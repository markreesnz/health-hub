"""File access for the WSET connector: a fixed whitelist of readable/writable
paths under the local study workspace, plus atomic, hash-checked writes.

Nothing here talks to Home Assistant or any network. ~/wset is a plain git
workspace on this Mac, edited directly by Claude and by Codex sessions, so
writes are guarded by a content-hash check (see previews.py) rather than a
numeric revision like the Finance connector uses against its shared JSON
blob.
"""
import hashlib
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path

CONFIG_PATH = Path(os.environ.get(
    'WSET_CONNECTOR_CONFIG',
    '/Users/mark/Documents/Codex/Connectors/wset-runtime/connection.json',
))


class WsetError(Exception):
    """A safe, user-facing error."""


def _load_root() -> Path:
    # WSET_ROOT always wins, deliberately — this is what tests set to point
    # at a throwaway directory, and it must never fall through to the real
    # connection.json (a prior version of this function got that backwards
    # and let a test suite write into the live ~/wset workspace).
    env_root = os.environ.get('WSET_ROOT')
    if env_root:
        return Path(env_root).resolve()
    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text())
        root = config.get('wset_root')
        if root:
            return Path(root).resolve()
    return Path('/Users/mark/wset').resolve()


ROOT = _load_root()

# State files the coach reads and updates each session.
STATE_TARGETS = {
    'progress': ROOT / 'state' / 'progress.md',
    'weak_areas': ROOT / 'state' / 'weak-areas.md',
    'study_plan': ROOT / 'state' / 'study-plan.md',
    'tasting_history': ROOT / 'state' / 'tasting-history.md',
}

# Only the state files above are writable.
WRITE_TARGETS = dict(STATE_TARGETS)

# coaching_contract is read-only: it's the living AGENTS.md coaching rules,
# read fresh each session rather than frozen into this connector's code.
READ_TARGETS = dict(STATE_TARGETS, coaching_contract=ROOT / 'AGENTS.md')

SESSIONS_DIR = ROOT / 'quizzes'
SLUG_RE = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def read_target(name: str) -> dict:
    if name not in READ_TARGETS:
        raise WsetError(f'Unknown target {name!r}. Valid targets: {sorted(READ_TARGETS)}.')
    path = READ_TARGETS[name]
    content = path.read_text(encoding='utf-8') if path.exists() else ''
    stat = path.stat() if path.exists() else None
    return {
        'target': name,
        'path': str(path),
        'content': content,
        'sha256': sha256(content),
        'mtime': stat.st_mtime if stat else None,
    }


def session_path(slug: str) -> Path:
    if not SLUG_RE.fullmatch(slug):
        raise WsetError('session_slug must be lowercase letters, digits and hyphens only, e.g. "loire-quickfire".')
    return SESSIONS_DIR / f'session-{date.today().isoformat()}-{slug}.md'


def read_session(slug: str) -> dict:
    path = session_path(slug)
    content = path.read_text(encoding='utf-8') if path.exists() else ''
    stat = path.stat() if path.exists() else None
    return {
        'target': f'session:{slug}',
        'path': str(path),
        'content': content,
        'sha256': sha256(content),
        'mtime': stat.st_mtime if stat else None,
    }


def list_sessions(limit: int = 10) -> list[dict]:
    if not SESSIONS_DIR.exists():
        return []
    # Newest first by the date in the file name, then by mtime: a git checkout
    # gives every file the same mtime, so mtime alone is not an order.
    files = sorted(
        (p for p in SESSIONS_DIR.glob('session-*.md') if p.is_file()),
        key=lambda p: (p.name[len('session-'):len('session-') + 10], p.stat().st_mtime),
        reverse=True,
    )
    return [
        {'path': str(p), 'name': p.name, 'mtime': p.stat().st_mtime}
        for p in files[:limit]
    ]


def resolve_write_path(target: str, session_slug: str | None) -> Path:
    if target == 'session_log':
        if not session_slug:
            raise WsetError('session_slug is required when target is "session_log".')
        return session_path(session_slug)
    if target not in WRITE_TARGETS:
        raise WsetError(f'Unknown write target {target!r}. Valid targets: {sorted(WRITE_TARGETS) + ["session_log"]}.')
    return WRITE_TARGETS[target]


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
