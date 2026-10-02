"""Run the existing Finance UI sync in an isolated browser, with durable job status."""
import asyncio
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from connection import Connection, FinanceError, atomic_json
from dataset import metadata


def now():
    return datetime.now(timezone.utc).isoformat()


def reconcile_bucket1_reserve(state):
    """Keep the app's B1 total aligned with its cash vehicles and confirmed transit."""
    transfers = state.get('financeConnector', {}).get('reserve_transfers', {})
    active = [row for row in transfers.values() if row.get('destination') == 'Simplicity Cash Fund' and row.get('purpose') == 'Bucket 1 retirement reserve' and row.get('included_in_b1_float')]
    if not active:
        return False
    accounts = state.get('cachedAkahuAccounts', [])
    bnz = next((a for a in accounts if (a.get('connection') or {}).get('name') == 'BNZ' and a.get('name') == 'Bucket 1'), None)
    cash = next((a for a in accounts if (a.get('connection') or {}).get('name') == 'Simplicity' and a.get('name') == 'Cash Fund'), None)
    if not bnz or not cash:
        return False
    bnz_value = (bnz.get('balance') or {}).get('current')
    cash_value = (cash.get('balance') or {}).get('current')
    if not isinstance(bnz_value, (int, float)) or not isinstance(cash_value, (int, float)):
        return False
    transfer_total = sum(float(row.get('amount') or 0) for row in active)
    destination_baseline = sum(float(row.get('destination_baseline') or 0) for row in active)
    arrived = min(transfer_total, max(0, float(cash_value) - destination_baseline))
    remaining = max(0, transfer_total - arrived)
    aggregate = round(float(bnz_value) + float(cash_value) + remaining, 2)
    changed = round(float(state.get('b1_float') or 0), 2) != aggregate
    state['b1_float'] = aggregate
    connector = state.setdefault('financeConnector', {})
    breakdown = {
        'bnz_bucket_1_confirmed': round(float(bnz_value), 2),
        'simplicity_cash_fund_confirmed': round(float(cash_value), 2),
        'simplicity_cash_fund_in_transit': round(remaining, 2),
        'bucket_1_liquid_total': aggregate,
    }
    if connector.get('reserve_vehicle_breakdown') != breakdown:
        connector['reserve_vehicle_breakdown'] = breakdown
        changed = True
    for row in active:
        new_status = 'confirmed' if arrived >= transfer_total * .98 else 'in_transit'
        if row.get('status') != new_status:
            row['status'] = new_status
            changed = True
    return changed


def jobs(connection):
    path = Path(connection.config['jobs_dir'])
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def status(connection, job_id=None):
    directory = jobs(connection)
    if job_id is None:
        if not (directory / 'latest.json').exists():
            return {'status': 'not_started', 'message': 'No connector refresh has been requested.'}
        job_id = json.loads((directory / 'latest.json').read_text())['job_id']
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise FinanceError('Invalid refresh job identifier.')
    path = directory / (job_id + '.json')
    if not path.exists():
        raise FinanceError('That refresh job was not found.')
    result = json.loads(path.read_text())
    if result['status'] in ('starting', 'running') and time.time() - result['started_epoch'] > 360:
        result.update(status='interrupted', message='The refresh did not report completion within six minutes. Some data may have saved; read the current dataset before retrying.')
    return result


def start(connection, request_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_id):
        raise FinanceError('Use an 8–80 character request ID containing letters, digits, underscores or hyphens. Reuse it when retrying the same request.')
    directory = jobs(connection)
    with (directory / 'launch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        request_path = directory / ('request-' + request_id + '.json')
        if request_path.exists():
            return status(connection, json.loads(request_path.read_text())['job_id'])
        latest = status(connection)
        if latest['status'] in ('starting', 'running'):
            atomic_json(request_path, {'job_id': latest['job_id']})
            return latest
        connection.check_contract()
        job_id = uuid.uuid4().hex
        result = {'job_id': job_id, 'status': 'starting', 'started_at': now(), 'started_epoch': time.time(), 'message': 'Opening the existing Finance sync workflow. Bank responses can take a few minutes.'}
        path = directory / (job_id + '.json')
        atomic_json(path, result)
        atomic_json(directory / 'latest.json', {'job_id': job_id})
        atomic_json(request_path, {'job_id': job_id})
        try:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), connection.config_path, job_id], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            # The worker owns status writes after launch, avoiding a starter/worker race.
            return {**result, 'worker_started': process.pid is not None}
        except Exception:
            result.update(status='failed', message='Could not start the refresh worker. No refresh was requested.')
            atomic_json(path, result)
            return result


async def run_worker(connection, job_id):
    from playwright.async_api import async_playwright
    directory = jobs(connection)
    path = directory / (job_id + '.json')
    job = json.loads(path.read_text())

    def update(**kwargs):
        job.update(kwargs, updated_at=now())
        atomic_json(path, job)

    with (directory / 'worker.lock').open('a') as worker_lock:
        try:
            fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            update(status='failed', message='A previous refresh worker is still active. Wait for it to finish.')
            return
        browser = None
        try:
            connection.check_contract()
            before = connection.dataset()
            session = connection.session()
            origin = connection.base
            prefix = urlparse(session['url']).path
            if connection.config.get('browser_path'):
                os.environ['PLAYWRIGHT_BROWSERS_PATH'] = connection.config['browser_path']
            update(status='running', stage='opening_app', before=metadata(before), message='Opening a private browser session using the app’s existing sync logic.')
            refresh_results = []
            failed_reads = []
            capture_tasks = set()
            async with async_playwright() as playwright:
                launch = {'headless': True}
                if connection.direct:
                    launch['args'] = ['--no-sandbox', '--disable-dev-shm-usage']
                if connection.config.get('chromium_executable'):
                    launch['executable_path'] = connection.config['chromium_executable']
                browser = await playwright.chromium.launch(**launch)
                context = await browser.new_context(timezone_id='Pacific/Auckland', locale='en-NZ', service_workers='block', accept_downloads=False)
                if session.get('cookie'):
                    await context.add_cookies([{'name': 'ingress_session', 'value': session['cookie'], 'domain': urlparse(origin).hostname, 'path': prefix, 'secure': True, 'httpOnly': True, 'sameSite': 'Strict'}])
                allowed_gets = {'/', '/financial-plan-dashboard.html', '/app.js', '/sync.js', '/migrations.js', '/calculations.js', '/status', '/state', '/auto-snapshots', '/accounts', '/transactions', '/share-price'}

                async def route_request(route):
                    request = route.request
                    parsed = urlparse(request.url)
                    if request.url == 'https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js' and request.method == 'GET':
                        await route.continue_()
                        return
                    if parsed.scheme + '://' + parsed.netloc == origin and parsed.path.startswith(prefix + '/'):
                        endpoint = parsed.path[len(prefix):]
                        if request.method == 'GET' and endpoint in allowed_gets or request.method == 'POST' and endpoint in ('/state', '/refresh'):
                            await route.continue_()
                            return
                    await route.abort()

                await context.route('**/*', route_request)
                page = await context.new_page()

                async def capture(response):
                    endpoint = urlparse(response.url).path[len(prefix):]
                    if endpoint == '/refresh':
                        payload = await response.json()
                        outcomes = []
                        for item in payload.get('results', []):
                            # Never store upstream error bodies or connection credentials.
                            error_text = str(item.get('error', ''))
                            code = re.search(r'HTTP Error (\d{3})', error_text)
                            outcomes.append({'accepted': not bool(error_text) and item.get('success', True) is not False, 'http_status': int(code.group(1)) if code else None})
                        refresh_results.append({'http_status': response.status, 'accepted': bool(payload.get('success')) and response.ok, 'connections': outcomes})
                    elif endpoint in ('/accounts', '/transactions') and not response.ok:
                        failed_reads.append({'endpoint': endpoint, 'http_status': response.status})

                def on_response(response):
                    task = asyncio.create_task(capture(response))
                    capture_tasks.add(task)
                    task.add_done_callback(capture_tasks.discard)

                page.on('response', on_response)
                await page.goto(session['url'] + '/', wait_until='domcontentloaded', timeout=45000)
                await page.wait_for_function("typeof _syncing !== 'undefined' && _syncing === true", timeout=45000)
                update(stage='waiting_for_banks', message='The Finance app is importing balances and transactions and waiting for bank responses.')
                await page.wait_for_function("typeof _syncing !== 'undefined' && _syncing === false", timeout=240000)
                result = await page.evaluate("""async () => {
                    if (!syncClient || syncClient.conflict) return {saved:false, conflict:true};
                    const saved = await syncClient.sync();
                    return {saved:!!saved, conflict:!!syncClient.conflict, revision:syncClient.revision,
                        lastFetch:state.akahuLastFetch, bankWarning:!!document.getElementById('lastSyncPill').dataset.failed};
                }""")
                if capture_tasks:
                    await asyncio.gather(*capture_tasks, return_exceptions=True)
                after = connection.dataset()
                if not result['saved'] or result['conflict']:
                    update(status='needs_attention', message='The app could not confirm a clean save. Existing conflict safeguards were preserved; open Finance to review.', after=metadata(after))
                    return
                if result.get('lastFetch') == before['state'].get('akahuLastFetch'):
                    update(status='failed', message='No successful new Akahu fetch was confirmed. Existing data remains available.', after=metadata(after))
                    return
                if reconcile_bucket1_reserve(after['state']):
                    after = connection.save_state(after['revision'], after['state'], 'finance-reserve-reconcile-' + uuid.uuid4().hex)
                before_accounts = {a.get('_id'): a.get('refreshed', {}) for a in before['state'].get('cachedAkahuAccounts', [])}
                advanced = []
                for account in after['state'].get('cachedAkahuAccounts', []):
                    earlier = before_accounts.get(account.get('_id'), {})
                    refreshed = account.get('refreshed') or {}
                    fields = [key for key in ('balance', 'transactions', 'meta') if refreshed.get(key) and refreshed[key] > earlier.get(key, '')]
                    if fields:
                        advanced.append({'provider': (account.get('connection') or {}).get('name'), 'account': account.get('name'), 'updated_fields': fields, 'refreshed': refreshed})
                limited = result.get('bankWarning') or failed_reads or not refresh_results or not all(r['accepted'] for r in refresh_results)
                update(status='completed_with_limits' if limited else 'completed', stage='saved', completed_at=now(), message='The app’s sync finished and saved its dataset.' + (' Some bank refresh requests or fetches were unavailable; an hourly Akahu limit may apply. See connection outcomes and timestamps.' if limited else ' Bank timestamps below show which accounts actually advanced.'), after=metadata(after), new_transaction_count=len({t['id'] for t in after['state']['transactions']} - {t['id'] for t in before['state']['transactions']}), bank_refresh_outcomes=refresh_results, fetch_failures=failed_reads, accounts_with_newer_bank_timestamps=advanced)
                await context.close()
        except FinanceError as error:
            update(status='failed', message=str(error))
        except Exception as error:
            # Browser exceptions can contain URLs/cookies, so keep details out of tool output.
            update(status='failed', error_type=type(error).__name__, message='The background refresh could not finish. Some passes may have saved; check current data before retrying. The Finance UI remains available.')
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass


if __name__ == '__main__':
    asyncio.run(run_worker(Connection(sys.argv[1]), sys.argv[2]))
