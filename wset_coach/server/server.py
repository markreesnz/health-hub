"""WSET study coach MCP: read Mark's WSET Level 3 study state and textbook,
and log coaching sessions back to the workspace.

On the Home Assistant Green (the WSET Coach Connector add-on) the workspace is
a git repository in /share/wset_coach/repo: every applied write is a commit,
and the Mac working copy syncs with it through the /git endpoint below, which
the wset_mcp integration exposes behind Home Assistant sign-in. Run with no
arguments for the original local stdio server. See storage.py / previews.py /
repo.py.
"""
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

import previews
import repo
import storage
import textbook
from storage import WsetError

INSTRUCTIONS = '''Access Mark's WSET Level 3 study workspace (hosted on his always-on Home Assistant; the ~/wset copy on his Mac syncs with it): saved progress, weak areas, study plan, tasting history, session logs, and the extracted textbook text. This is a coaching tool, not a scoring service — start a session by calling wset_coaching_contract and following it; it is the authoritative, up-to-date rulebook (active recall, one question at a time, positive marking, source-checked questions, delayed-retention tracking) and supersedes any summary of it written elsewhere. Also read wset_progress for the exact resume point and wset_weak_areas before choosing questions. Use wset_search_textbook to ground questions and feedback in the actual 2016 Issue 1 textbook and cite the book page it returns.
Writes are preview-then-apply, like the Finance connector: wset_preview_write computes the exact new file content and returns a preview_id; wset_apply_write commits it only if the file has not changed since (it may have been edited by a Codex session in the meantime, in which case re-read and re-preview rather than overwriting); every applied write is kept in the workspace's git history. Session transcripts go to target="session_log" with a short slug (e.g. "loire-quickfire"), which is created at quizzes/session-<today>-<slug>.md; save raw answers there as the session goes, not just at the end. Update target="progress" with the exact resume point and target="weak_areas" with repeated errors before ending a session. Never invent a score or mark a question correct that was not actually answered.'''

mcp = FastMCP('WSET', instructions=INSTRUCTIONS, log_level='ERROR')

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
PREVIEW = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except WsetError as error:
        raise ValueError(str(error)) from None


@mcp.tool(annotations=READ)
def wset_coaching_contract() -> dict:
    """Read AGENTS.md: the current, authoritative WSET coaching contract (marking rules, question style, retention policy). Call this first in a new coaching session."""
    return _wrap(storage.read_target, 'coaching_contract')


@mcp.tool(annotations=READ)
def wset_progress() -> dict:
    """Read state/progress.md: the exact resume point and recent session history."""
    return _wrap(storage.read_target, 'progress')


@mcp.tool(annotations=READ)
def wset_weak_areas() -> dict:
    """Read state/weak-areas.md: repeated errors and topics due for a check."""
    return _wrap(storage.read_target, 'weak_areas')


@mcp.tool(annotations=READ)
def wset_study_plan() -> dict:
    """Read state/study-plan.md: saved preferences for how Mark wants sessions run."""
    return _wrap(storage.read_target, 'study_plan')


@mcp.tool(annotations=READ)
def wset_tasting_history() -> dict:
    """Read state/tasting-history.md: longitudinal tasting findings and calibration biases."""
    return _wrap(storage.read_target, 'tasting_history')


@mcp.tool(annotations=READ)
def wset_list_sessions(limit: Annotated[int, Field(ge=1, le=50)] = 10) -> list[dict]:
    """List the most recent dated session-log files under quizzes/, newest first."""
    return _wrap(storage.list_sessions, limit)


@mcp.tool(annotations=READ)
def wset_session_log(session_slug: str) -> dict:
    """Read today's session log for a given slug (quizzes/session-<today>-<slug>.md). Returns empty content if it doesn't exist yet."""
    return _wrap(storage.read_session, session_slug)


@mcp.tool(annotations=READ)
def wset_search_textbook(
    query: str,
    max_results: Annotated[int, Field(ge=1, le=30)] = 8,
    context_chars: Annotated[int, Field(ge=100, le=2000)] = 500,
) -> list[dict]:
    """Search the extracted 2016 Issue 1 textbook text for a term or phrase. Each hit includes the surrounding excerpt and the book page it came from, for citation."""
    return _wrap(textbook.search_textbook, query, max_results, context_chars)


@mcp.tool(annotations=PREVIEW)
def wset_preview_write(
    target: Literal['progress', 'weak_areas', 'study_plan', 'tasting_history', 'session_log'],
    content: str,
    mode: Literal['append', 'replace'] = 'append',
    session_slug: str | None = None,
) -> dict:
    """Preview a write before saving it. target picks the file (session_log needs session_slug, e.g. "loire-quickfire", written to quizzes/session-<today>-<slug>.md). mode="append" adds content as a new paragraph; "replace" overwrites the whole file — use replace only for progress.md's resume point, never for session logs. Returns a preview_id to pass to wset_apply_write."""
    return _wrap(previews.preview_write, target, content, mode, session_slug)


@mcp.tool(annotations=WRITE)
def wset_apply_write(preview_id: str) -> dict:
    """Commit a previously previewed write. Fails if the target file changed since the preview (e.g. edited by a Codex session) — re-read and re-preview in that case rather than forcing it."""
    return _wrap(previews.apply_write, preview_id)


def _log(**fields) -> None:
    print(json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), **fields}), flush=True)


def _rpc_summary(body: bytes) -> str:
    try:
        message = json.loads(body)
    except ValueError:
        return ''
    items = message if isinstance(message, list) else [message]
    names = []
    for item in items:
        if not isinstance(item, dict):
            continue
        method = item.get('method', '')
        if method == 'tools/call':
            method += ':' + str((item.get('params') or {}).get('name', ''))
        names.append(method)
    return ','.join(names)


def http_app():
    """Streamable HTTP app for the Home Assistant add-on.

    Only the companion wset_mcp integration (Home Assistant sign-in, admin
    only) can reach it: every request must carry the shared secret the add-on
    generated at first start. Routes: /mcp (MCP), /git/... (workspace sync),
    /healthz. Each request is logged with the Home Assistant user it came from.
    """
    secret = Path(os.environ.get('WSET_PROXY_SECRET_FILE', '/data/proxy_secret')).read_text().strip()
    if len(secret) < 32:
        raise SystemExit('Proxy secret missing or too short.')
    project_root = storage.ROOT.parent
    inner = mcp.streamable_http_app()

    async def respond(send, status, body):
        await send({'type': 'http.response.start', 'status': status, 'headers': [(b'content-type', b'application/json')]})
        await send({'type': 'http.response.body', 'body': body})

    async def app(scope, receive, send):
        if scope['type'] != 'http':
            return await inner(scope, receive, send)
        path = scope.get('path', '')
        if path == '/healthz':
            return await respond(send, 200, b'{"ok":true}')
        headers = dict(scope.get('headers') or [])
        user = headers.get(b'x-wset-user', b'').decode('latin-1')[:64]
        started = time.monotonic()
        if not hmac.compare_digest(headers.get(b'x-wset-proxy-secret', b'').decode('latin-1'), secret):
            _log(event='refused', path=path, reason='missing or wrong proxy secret')
            return await respond(send, 403, b'{"error":"forbidden"}')

        if path == '/git' or path.startswith('/git/'):
            status = await repo.git_http(scope, receive, send, project_root, user)
            _log(event='git', user=user, method=scope['method'], path=path, status=status,
                 ms=round((time.monotonic() - started) * 1000))
            return

        # Buffer the body once so the JSON-RPC method can be logged, then replay it.
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get('body', b''))
            if not message.get('more_body'):
                break
        body = b''.join(chunks)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await receive()

        status_box = {}

        async def capture(message):
            if message['type'] == 'http.response.start':
                status_box['status'] = message['status']
            await send(message)

        await inner(scope, replay, capture)
        _log(event='mcp', user=user, method=scope['method'], rpc=_rpc_summary(body),
             status=status_box.get('status'), ms=round((time.monotonic() - started) * 1000))
    return app


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'http':
        import uvicorn
        from mcp.server.transport_security import TransportSecuritySettings
        mcp.settings.stateless_http = True
        mcp.settings.json_response = True
        mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        uvicorn.run(http_app(), host='0.0.0.0', port=int(os.environ.get('WSET_MCP_PORT', '8099')),
                    log_level='warning', proxy_headers=False, server_header=False)
    else:
        mcp.run()
