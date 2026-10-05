"""Preview -> apply writes, mirroring the Finance connector's edit flow but
against a content hash instead of a numeric revision (these are markdown
files with no revision counter, hand-edited by more than one tool).

A preview never touches the workspace. Apply re-reads the target file right
before writing and refuses if its hash has moved since the preview was
taken, so a concurrent Codex/Claude edit can't be silently clobbered.
"""
import json
import os
import time
import uuid
from pathlib import Path

import repo
from storage import ROOT, WsetError, atomic_write, resolve_write_path, sha256

RUNTIME_DIR = Path(os.environ.get(
    'WSET_CONNECTOR_RUNTIME',
    '/Users/mark/Documents/Codex/Connectors/wset-runtime',
))
PREVIEWS_DIR = RUNTIME_DIR / 'previews'
RECEIPTS_PATH = RUNTIME_DIR / 'receipts' / 'receipts.jsonl'

MAX_CONTENT_BYTES = 2 * 1024 * 1024  # generous for a markdown session log


def _preview_file(preview_id: str) -> Path:
    if not preview_id or '/' in preview_id or '\\' in preview_id:
        raise WsetError('Invalid preview_id.')
    return PREVIEWS_DIR / f'{preview_id}.json'


def build_new_content(current: str, addition: str, mode: str) -> str:
    if mode == 'replace':
        return addition
    if mode == 'append':
        if not current:
            return addition
        return current.rstrip('\n') + '\n\n' + addition
    raise WsetError('mode must be "append" or "replace".')


def preview_write(target: str, content: str, mode: str = 'append', session_slug: str | None = None) -> dict:
    if len(content.encode('utf-8')) > MAX_CONTENT_BYTES:
        raise WsetError('That content is larger than this connector writes in one go.')
    path = resolve_write_path(target, session_slug)
    current = path.read_text(encoding='utf-8') if path.exists() else ''
    base_sha256 = sha256(current)
    new_content = build_new_content(current, content, mode)

    preview_id = uuid.uuid4().hex
    PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        'preview_id': preview_id,
        'target': target,
        'session_slug': session_slug,
        'path': str(path),
        'mode': mode,
        'base_sha256': base_sha256,
        'new_content': new_content,
        'created': time.time(),
    }
    (PREVIEWS_DIR / f'{preview_id}.json').write_text(json.dumps(record), encoding='utf-8')

    return {
        'preview_id': preview_id,
        'path': str(path),
        'existed': bool(current),
        'current_excerpt': current[-600:],
        'proposed_addition': content,
        'mode': mode,
    }


def apply_write(preview_id: str) -> dict:
    record_path = _preview_file(preview_id)
    if not record_path.exists():
        raise WsetError('No pending preview with that ID. Previews are single-use and do not survive a connector restart; create a new one.')
    record = json.loads(record_path.read_text(encoding='utf-8'))
    path = Path(record['path'])
    # Hash check, write and commit happen under the repo lock, so a git push
    # from the Mac cannot land in between.
    with repo.locked():
        existed = path.exists()
        current = path.read_text(encoding='utf-8') if existed else ''
        if sha256(current) != record['base_sha256']:
            raise WsetError(
                f'{path.name} changed since this preview was taken (edited elsewhere, e.g. by a Codex session synced from the Mac). '
                'Re-read it and create a new preview rather than overwriting that change.'
            )
        atomic_write(path, record['new_content'])
        slug = record.get('session_slug')
        label = f"session {slug}" if record['target'] == 'session_log' else record['target']
        try:
            commit = repo.commit_file(ROOT, path, f"Coach: {record['mode']} {label} ({path.name})\n\npreview {preview_id}")
        except Exception:
            # Never leave an uncommitted write behind: it would block Mac pushes.
            if existed:
                atomic_write(path, current)
            else:
                path.unlink(missing_ok=True)
            raise WsetError('The write could not be committed to the workspace history, so it was rolled back. Nothing was saved; try again.')
    record_path.unlink(missing_ok=True)

    receipt = {
        'preview_id': preview_id,
        'target': record['target'],
        'session_slug': record.get('session_slug'),
        'path': str(path),
        'mode': record['mode'],
        'new_sha256': sha256(record['new_content']),
        'commit': commit,
        'applied_at': time.time(),
    }
    RECEIPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RECEIPTS_PATH.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(receipt) + '\n')
    return receipt
