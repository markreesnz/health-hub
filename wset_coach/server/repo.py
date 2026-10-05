"""The coaching workspace as a git repository on the Green.

Every applied write becomes one commit, so the state has history and the
Mac working copy syncs with plain `git pull` / `git push` through the
`wset_mcp` integration's git endpoint (see git_http below).

One exclusive lock serialises the two writers: wset_apply_write (hash check
-> write -> commit) and a push from the Mac (receive-pack, which updates the
checked-out files via receive.denyCurrentBranch=updateInstead). Without it a
push could land between an apply's hash check and its write.
"""
import contextlib
import fcntl
import os
import subprocess
from pathlib import Path

def _lock_path() -> Path:
    if os.environ.get('WSET_REPO_LOCK'):
        return Path(os.environ['WSET_REPO_LOCK'])
    return Path(os.environ.get('WSET_CONNECTOR_RUNTIME', '/data')) / 'repo.lock'

GIT_ENV = {
    'GIT_AUTHOR_NAME': 'WSET Study Coach',
    'GIT_AUTHOR_EMAIL': 'wset-coach@homeassistant.local',
    'GIT_COMMITTER_NAME': 'WSET Study Coach',
    'GIT_COMMITTER_EMAIL': 'wset-coach@homeassistant.local',
}


@contextlib.contextmanager
def locked():
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def is_repo(root: Path) -> bool:
    return (root / '.git').is_dir()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ['git', '-C', str(root), *args],
        env={**os.environ, **GIT_ENV}, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f'git {args[0]} failed: {result.stderr.strip()[:300]}')
    return result.stdout.strip()


def commit_file(root: Path, path: Path, message: str) -> str | None:
    """Commit one file. Caller holds locked(). Returns the commit id, or None
    when the workspace is not a git repo (tests, or a plain local folder)."""
    if not is_repo(root):
        return None
    rel = str(path.resolve().relative_to(root.resolve()))
    _git(root, 'add', '--', rel)
    unchanged = subprocess.run(['git', '-C', str(root), 'diff', '--cached', '--quiet', '--', rel]).returncode == 0
    if not unchanged:
        _git(root, 'commit', '-q', '-m', message, '--', rel)
    return _git(root, 'rev-parse', 'HEAD')


async def git_http(scope, receive, send, project_root: Path, user: str):
    """Serve git smart HTTP for the workspace via `git http-backend` (CGI).

    Path layout below /git: /git/repo/info/refs, /git/repo/git-upload-pack,
    /git/repo/git-receive-pack. Pushes take the repo lock for their whole run.
    """
    import asyncio

    path = scope['path'][len('/git'):] or '/'
    headers = {k.decode('latin-1').lower(): v.decode('latin-1') for k, v in scope.get('headers') or []}
    body = bytearray()
    while True:
        message = await receive()
        body.extend(message.get('body', b''))
        if not message.get('more_body'):
            break

    env = {
        'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
        'GIT_PROJECT_ROOT': str(project_root),
        'GIT_HTTP_EXPORT_ALL': '1',
        'REQUEST_METHOD': scope['method'],
        'PATH_INFO': path,
        'QUERY_STRING': scope.get('query_string', b'').decode('latin-1'),
        'CONTENT_TYPE': headers.get('content-type', ''),
        'CONTENT_LENGTH': str(len(body)),
        'REMOTE_USER': user or 'mac',
        'REMOTE_ADDR': '127.0.0.1',
        'GIT_CONFIG_NOSYSTEM': '1',
        **GIT_ENV,
    }
    if headers.get('content-encoding'):
        env['HTTP_CONTENT_ENCODING'] = headers['content-encoding']
    if headers.get('git-protocol'):
        env['GIT_PROTOCOL'] = headers['git-protocol']

    def run() -> bytes:
        cm = locked() if path.endswith('git-receive-pack') else contextlib.nullcontext()
        with cm:
            return subprocess.run(['git', 'http-backend'], input=bytes(body), env=env,
                                  capture_output=True, timeout=120).stdout

    output = await asyncio.to_thread(run)
    head, _, payload = output.partition(b'\r\n\r\n')
    if not _:
        head, _, payload = output.partition(b'\n\n')
    status = 200
    out_headers = []
    for line in head.decode('latin-1').splitlines():
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        if key.lower() == 'status':
            status = int(value.strip().split()[0])
        else:
            out_headers.append((key.strip().lower().encode(), value.strip().encode('latin-1')))
    await send({'type': 'http.response.start', 'status': status, 'headers': out_headers})
    await send({'type': 'http.response.body', 'body': payload})
    return status
