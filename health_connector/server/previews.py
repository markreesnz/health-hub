"""Preview → apply for the two writes (log a drink, log a meal), like the Finance connector.

A preview computes exactly what would be saved and stores it as a draft; apply saves that
draft only. Retrying apply with the same preview_id and request_id is safe: the hub write
is keyed by an id derived from the request_id, so it lands once.
"""
import hashlib
import json
import os
import re
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import hub
from hub import HealthError

PREVIEW_TTL_SECONDS = 24 * 3600
MEALS = ('breakfast', 'lunch', 'dinner', 'snack')
NZ = ZoneInfo('Pacific/Auckland')


def _dir(name: str) -> Path:
    path = Path(os.environ.get('HEALTH_RUNTIME', '/data')) / name
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _prune() -> None:
    cutoff = time.time() - 7 * 24 * 3600
    for path in _dir('previews').glob('*.json'):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def _fingerprint(drinks: list, exclude_id: str | None = None) -> str:
    kept = sorted((d for d in drinks if d.get('id') != exclude_id), key=lambda d: d.get('id', ''))
    return hashlib.sha256(json.dumps(kept, sort_keys=True).encode()).hexdigest()


def _hub_id(request_id: str) -> str:
    return hashlib.sha256(request_id.encode()).hexdigest()[:16]


def _save_draft(action: str, fields: dict, **extra) -> dict:
    _prune()
    draft = {'preview_id': uuid.uuid4().hex, 'action': action, 'fields': fields,
             'created_at': time.time(), **extra}
    _write(_dir('previews') / (draft['preview_id'] + '.json'), draft)
    return draft


def preview_drink(time_hhmm: str | None = None, day: str | None = None, std: float | None = None,
                  kind: str = 'wine', note: str | None = None) -> dict:
    body = {k: v for k, v in {'time': time_hhmm, 'date': day, 'std': std, 'kind': kind, 'note': note}.items()
            if v is not None}
    after = hub.post('/alcohol/drink', {**body, 'dry_run': True})
    drink, resolved = after['drink'], after['day']['date']
    before = hub.get('/alcohol', date=resolved)['day']['drinks']
    fields = {'date': resolved, 'time': drink['time'], 'kind': drink['kind'], 'std': drink['std']}
    if 'note' in drink:
        fields['note'] = drink['note']
    draft = _save_draft('log_drink', fields, fingerprint=_fingerprint(before))
    return {'status': 'preview', 'preview_id': draft['preview_id'], 'would_log': fields,
            'day_after': {k: after['day'][k] for k in ('date', 'weekday', 'std', 'broken')},
            'week_after': {k: after['week'][k] for k in ('std', 'drink_days', 'dry_days', 'unknown_days', 'broken')},
            'reset': after['reset'],
            'message': 'Nothing saved yet. Apply with health_apply only if Mark asked to log this drink. '
                       '"broken" lists rules this drink breaks; report them plainly, without judgement.'}


def preview_meal(text: str, meal: str, day: str | None = None) -> dict:
    if meal not in MEALS:
        raise HealthError(f'meal must be one of {", ".join(MEALS)}')
    day = day or datetime.now(NZ).date().isoformat()
    date.fromisoformat(day)
    if not text.strip() or len(text) > 1000:
        raise HealthError('Describe the meal in 1–1000 characters.')
    items = hub.post('/food/parse', {'text': text})['items']
    if not items:
        raise HealthError('The food parser returned nothing for that text.')
    draft = _save_draft('log_meal', {'day': day, 'meal': meal, 'items': items, 'text': text})
    totals = {k: round(sum(i.get(k) or 0 for i in items)) for k in ('cal', 'protein', 'carbs', 'fat')}
    return {'status': 'preview', 'preview_id': draft['preview_id'], 'day': day, 'meal': meal,
            'items': [{k: i.get(k) for k in ('name', 'cal', 'protein', 'carbs', 'fat', 'cups', 'group', 'tags', 'wahls_ok', 'why')}
                      for i in items],
            'totals': totals,
            'off_plan': [f"{i.get('name')} ({i.get('why') or 'off-plan'})" for i in items if i.get('wahls_ok') is False],
            'message': 'Nothing saved yet. Check the portions with Mark if any look wrong (re-preview with clearer '
                       'text), then apply with health_apply.'}


def apply(preview_id: str, request_id: str) -> dict:
    if not re.fullmatch(r'[a-f0-9]{32}', preview_id or '') or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_id or ''):
        raise HealthError('Use a valid preview_id and an 8–80 character request_id. Reuse both after an uncertain response.')
    path = _dir('previews') / (preview_id + '.json')
    if not path.exists():
        raise HealthError('That preview was not found. Create a fresh preview.')
    draft = json.loads(path.read_text())
    receipt_path = _dir('receipts') / (_hub_id(request_id) + '.json')
    if receipt_path.exists() and json.loads(receipt_path.read_text())['preview_id'] != preview_id:
        raise HealthError('That request_id belongs to a different write. Use a new request_id.')
    if time.time() - draft['created_at'] > PREVIEW_TTL_SECONDS:
        raise HealthError('That preview is more than a day old. Create a fresh preview.')
    hub_id, f = _hub_id(request_id), draft['fields']

    if draft['action'] == 'log_drink':
        current = hub.get('/alcohol', date=f['date'])['day']['drinks']
        if not any(d.get('id') == hub_id for d in current) and _fingerprint(current, hub_id) != draft['fingerprint']:
            raise HealthError("That day's drinks changed after the preview. Nothing saved; preview again.")
        result = hub.post('/alcohol/drink', {**f, 'id': hub_id})
        out = {'status': 'already_applied' if result.get('already_applied') else 'applied',
               'logged': f, 'day': {k: result['day'][k] for k in ('date', 'std', 'broken')},
               'reset': result['reset']}
    elif draft['action'] == 'log_meal':
        result = hub.post('/food/items', {'day': f['day'], 'meal': f['meal'], 'items': f['items'], 'entry_id': hub_id})
        out = {'status': 'already_applied' if result.get('already_applied') else 'applied',
               'day': f['day'], 'meal': f['meal'], 'day_totals': result.get('totals'),
               'wahls': {k: (result.get('wahls') or {}).get(k) for k in ('cups', 'cups_total', 'off_plan')}}
    else:
        raise HealthError('Unknown preview type.')
    _write(receipt_path, {'preview_id': preview_id, 'request_id': request_id, 'status': out['status'],
                          'applied_at': time.time()})
    out['message'] = 'Saved in the Health Hub; it shows in the app on the next refresh.'
    return out
