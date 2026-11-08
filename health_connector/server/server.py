"""Health MCP: read Mark's Health Hub (food, Wahls cups, drinks, rules, weekly review)
and log a drink or a meal through preview → apply.

Runs in the Health Connector add-on on the Home Assistant Green. All data and rules
live in the Health Hub add-on, reached over the Supervisor's private network; see hub.py
and previews.py.
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

import hub
import previews
from hub import HealthError

INSTRUCTIONS = '''Access Mark's Health Hub (hosted on his always-on Home Assistant): today's food log with Wahls veg cups, drinks logged against his alcohol rules and 30-day reset, date-range summaries with weight/HRV/sleep, his targets, and the Sunday weekly review. Be plain and non-judgemental; report rules kept or broken as facts.
Call health_targets before advising: it is the authoritative list of his food targets (Wahls Paleo: 3 cups each of greens, sulfur and colour daily plus seaweed; protein each meal; no gluten, dairy, egg, added sugar or packaged/ultra-processed food) and alcohol rules (30-day reset, then dry Mon-Thu, max 2 NZ standard drinks, nothing after 8pm). Use health_food_sleep to answer how food and drink affect his sleep, and say how many nights a comparison rests on. Drink days are "drink", "dry" (confirmed), "unknown" (not logged — never assume dry) or "future". The drinking day rolls over at 05:00.
Writes are preview-then-apply, like the Finance connector: health_preview_log_drink or health_preview_log_meal computes exactly what would be saved and returns a preview_id; health_apply saves it. Only log what Mark asked you to log. Pass a request_id you choose (8-80 characters) and reuse the same preview_id and request_id if a response is uncertain — the write lands once. Check parsed meal portions with Mark when they look off and re-preview instead of applying. Never invent intake or numbers that are not in the data.'''

mcp = FastMCP('Health', instructions=INSTRUCTIONS, log_level='ERROR')

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
PREVIEW = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
Day = Annotated[str, Field(pattern=r'^\d{4}-\d{2}-\d{2}$', description='YYYY-MM-DD')]


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except HealthError as error:
        raise ValueError(str(error)) from None


@mcp.tool(annotations=READ)
def health_today() -> dict:
    """Today's food log (items with macros, Wahls cup group, on/off-plan), totals vs targets, Wahls cups by group, and the drinking day's drinks, rule flags, week and 30-day reset status."""
    return _wrap(hub.get, '/today')


@mcp.tool(annotations=READ)
def health_range(start: Day, end: Day) -> dict:
    """Day-by-day summary for start..end inclusive (max 92 days): food logged, macros, veg cups, seaweed, off-plan items, drink status and standard drinks, rules broken, weight, HRV, sleep, resting HR, sleep score, hours after bed that heart rate bottomed out, HRV recovery and time to fall asleep; plus averages."""
    return _wrap(hub.get, '/summary/range', start=start, end=end)


@mcp.tool(annotations=READ)
def health_targets() -> dict:
    """Current food targets, Wahls rules, alcohol rules (with the full guideline list), 30-day reset status and weight goal."""
    return _wrap(hub.get, '/targets')


@mcp.tool(annotations=READ)
def health_weekly_review(week_ending: Day | None = None) -> dict:
    """The Sunday review for the Mon–Sun week containing week_ending (default this week): each habit's day-by-day status and total, Zone 2 minutes vs target, the 7-day smoothed weight trend and weekly HRV / sleep score, the experiment (progress or result), the week's insight, and next week's proposed actions with their status (proposed / accepted / skipped). Mark accepts or edits proposals in the app."""
    return _wrap(hub.get, '/review', date=week_ending)


@mcp.tool(annotations=READ)
def health_experiments() -> dict:
    """His experiments: the active one (one at a time; day, adherence, daily status, and the running before/after against the 4 weeks before it), finished ones with adherence, measured effect on HRV, resting HR, deep sleep, sleep efficiency and weight trend, and a plain-English verdict, plus suggested next experiments. Report verdicts as measured; don't overstate low-adherence or low-confidence results."""
    return _wrap(hub.get, '/experiments')


@mcp.tool(annotations=READ)
def health_food_sleep(days: Annotated[int, Field(ge=7, le=365)] = 90) -> dict:
    """How each evening's food and drinks line up with the next night's Oura numbers over the last `days`: for drinks, late meals, carbs over target, calories over target, off-plan food and hitting all 9 veg cups, the average sleep score, HRV, lowest heart rate, how many hours after bed heart rate bottomed out, HRV recovery (second half of the night vs first), time to fall asleep and deep sleep, on nights with vs without. "enough" is false under 3 nights each way; treat small samples as hints, not findings. Also last night's heart-rate low and HRV recovery."""
    return _wrap(hub.get, '/insights/food-sleep', days=days)


@mcp.tool(annotations=READ)
def health_insights() -> dict:
    """Ranked insights from his own data: each evening's behaviour (drinks, late meal, heavy or high-carb day, off-plan food, low veg, Zone 2/HIIT/strength day, late bedtime) against the next night's HRV, lowest heart rate, deep sleep, sleep efficiency, sleep score and heart-rate-low timing. Only effects with 8+ nights on each side, a clear effect size, 90%+ confidence and a specific action are included. `headline` is this week's one insight (it isn't repeated in later weeks unless it materially changes); `others` are the rest, ranked. Lead with the action."""
    return _wrap(hub.get, '/insights/ranked')


@mcp.tool(annotations=READ)
def health_today_plan() -> dict:
    """What the app's Today screen says: readiness, the one thing that matters today, each active habit's status this week (Zone 2, Eat clean, No alcohol and any others), which habit is slipping most and its recovery action, Zone 2 pace (minutes, days left, minutes/day needed, 4-week adherence), and the evening questions still open."""
    return _wrap(hub.get, '/today/plan')


@mcp.tool(annotations=READ)
def health_training_week(date: Day | None = None) -> dict:
    """Training compliance for the Mon–Sun week containing `date` (default this week): strength sessions, Zone 2 minutes (time with heart rate in his Zone 2 range) and HIIT sessions against his weekly targets, each session with its source (Apple, Oura or logged by hand), plus the previous 8 weeks. Strength sessions are counted, not planned."""
    return _wrap(hub.get, '/training', date=date)


@mcp.tool(annotations=PREVIEW)
def health_preview_log_drink(
    time: Annotated[str | None, Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$', description='HH:MM, 24-hour, NZ time; default now')] = None,
    date: Annotated[Day | None, Field(description='Drinking day; default inferred from the time (before 05:00 = the night before)')] = None,
    standard_drinks: Annotated[float | None, Field(ge=0, le=10, description='NZ standard drinks; default by kind (wine 150 ml = 1.5)')] = None,
    kind: str = 'wine',
    note: Annotated[str | None, Field(max_length=200)] = None,
) -> dict:
    """Preview logging one drink: shows the day it lands on, the day's and week's totals after it, and any rules it breaks. Nothing is saved until health_apply."""
    return _wrap(previews.preview_drink, time, date, standard_drinks, kind, note)


@mcp.tool(annotations=PREVIEW)
def health_preview_log_meal(
    text: Annotated[str, Field(min_length=1, max_length=1000, description='What was eaten, comma-separated foods with portions, e.g. "200g salmon, 2 cups kale, 1 cup roast beetroot"')],
    meal: Literal['breakfast', 'lunch', 'dinner', 'snack'],
    date: Day | None = None,
    time_eaten: Annotated[str | None, Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$', description='HH:MM NZ time; default now when logging for today. Ask if Mark is logging an earlier meal.')] = None,
) -> dict:
    """Preview logging a meal: parses the text into items with macros, Wahls cup group and on/off-plan flags. Nothing is saved until health_apply. The time eaten feeds the last-meal-before-bed gap."""
    return _wrap(previews.preview_meal, text, meal, date, time_eaten)


@mcp.tool(annotations=WRITE)
def health_apply(preview_id: str, request_id: Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')]) -> dict:
    """Save a previewed drink or meal. Choose a request_id and reuse the same preview_id and request_id when retrying; the write lands once. A drink preview is refused if that day's drinks changed since — preview again."""
    return _wrap(previews.apply, preview_id, request_id)


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

    Only the companion health_mcp integration (Home Assistant sign-in, admin
    only) can reach it: every request must carry the shared secret the add-on
    generated at first start. Routes: /mcp (MCP), /healthz. Each request is
    logged with the Home Assistant user it came from.
    """
    secret = Path(os.environ.get('HEALTH_PROXY_SECRET_FILE', '/data/proxy_secret')).read_text().strip()
    if len(secret) < 32:
        raise SystemExit('Proxy secret missing or too short.')
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
        user = headers.get(b'x-health-user', b'').decode('latin-1')[:64]
        started = time.monotonic()
        if not hmac.compare_digest(headers.get(b'x-health-proxy-secret', b'').decode('latin-1'), secret):
            _log(event='refused', path=path, reason='missing or wrong proxy secret')
            return await respond(send, 403, b'{"error":"forbidden"}')

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
        uvicorn.run(http_app(), host='0.0.0.0', port=int(os.environ.get('HEALTH_MCP_PORT', '8099')),
                    log_level='warning', proxy_headers=False, server_header=False)
    else:
        mcp.run()
