"""Private Home Assistant client: fixed reads and revision-checked state saves."""
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

import websocket

ROOT = Path(__file__).resolve().parent
CONTRACT = json.loads((ROOT / 'app_contract.json').read_text())
DEFAULT_CONFIG = '/data/connection.json'


class FinanceError(Exception):
    """A safe, user-facing error; never include upstream bodies or credentials."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Connection:
    READ_PATHS = frozenset(['/state', '/status', '/app.js'])

    def __init__(self, config_path=None):
        self.config_path = str(Path(config_path or os.environ.get('FINANCE_CONFIG', DEFAULT_CONFIG)).resolve())
        self.config = json.loads(Path(self.config_path).read_text())
        self._session = None
        # Direct mode (Home Assistant add-on): talk to the Finance add-on over the
        # Supervisor's private network. No Home Assistant credential is involved.
        self.direct = bool(self.config.get('finance_url'))
        if self.direct:
            self.base = self.config['finance_url'].rstrip('/')
            url = urlparse(self.base)
            if url.scheme != 'http' or not url.hostname or url.username or url.password or url.path or url.query or url.fragment:
                raise FinanceError('The Finance add-on address must be an internal http://host:port origin.')
            return
        self.base = self.config['home_assistant_url'].rstrip('/')
        url = urlparse(self.base)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.path or url.query or url.fragment:
            raise FinanceError('The Home Assistant address must be an HTTPS origin.')
        self.slug = self.config['addon_slug']
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', self.slug):
            raise FinanceError('Invalid finance add-on identifier.')

    def _headers(self, session, extra):
        headers = dict(extra)
        if session.get('cookie'):
            headers['Cookie'] = 'ingress_session=' + session['cookie']
        return headers

    def session(self, renew=False):
        if self.direct:
            return {'url': self.base, 'cookie': None, 'expires': float('inf')}
        if self._session and not renew and time.monotonic() < self._session['expires']:
            return dict(self._session)
        ws = None
        try:
            token = Path(self.config['token_file']).read_text().strip()
            if not token:
                raise FinanceError('The saved Home Assistant credential is empty.')
            ws = websocket.create_connection(self.base.replace('https:', 'wss:', 1) + '/api/websocket', timeout=15)
            if json.loads(ws.recv()).get('type') != 'auth_required':
                raise FinanceError('Home Assistant did not offer the expected sign-in handshake.')
            ws.send(json.dumps({'type': 'auth', 'access_token': token}))
            if json.loads(ws.recv()).get('type') != 'auth_ok':
                raise FinanceError('Home Assistant rejected the saved credential.')
            results = []
            # These are the only supervisor operations this client can issue.
            for ident, endpoint, method in [(1, '/addons/' + self.slug + '/info', 'get'), (2, '/ingress/session', 'post')]:
                ws.send(json.dumps({'id': ident, 'type': 'supervisor/api', 'endpoint': endpoint, 'method': method, 'data': {}}))
                reply = json.loads(ws.recv())
                if reply.get('id') != ident or not reply.get('success'):
                    raise FinanceError('Home Assistant could not open a private Finance session.')
                results.append(reply['result'])
            ingress = results[0]['ingress_url']
            if not re.fullmatch(r'/api/hassio_ingress/[A-Za-z0-9_-]+/?', ingress):
                raise FinanceError('Home Assistant returned an unexpected Finance address.')
            self._session = {'url': self.base + ingress.rstrip('/'), 'cookie': results[1]['session'], 'expires': time.monotonic() + 1200}
            return dict(self._session)
        except FinanceError:
            raise
        except Exception:
            raise FinanceError('Could not connect to Home Assistant. Check its availability and saved login.') from None
        finally:
            if ws is not None:
                ws.close()

    def read(self, path):
        if path not in self.READ_PATHS:
            raise FinanceError('That endpoint is outside the connector’s read-only surface.')
        for attempt in range(2):
            session = self.session(renew=attempt == 1)
            try:
                req = Request(session['url'] + path, headers=self._headers(session, {'Accept': 'application/json', 'Cache-Control': 'no-cache'}))
                with build_opener(NoRedirect()).open(req, timeout=20) as response:
                    data = response.read(20 * 1024 * 1024 + 1)
                if len(data) > 20 * 1024 * 1024:
                    raise FinanceError('Finance returned more data than the connector can safely read.')
                return data.decode('utf-8') if path == '/app.js' else json.loads(data)
            except HTTPError as error:
                if error.code in (401, 403) and attempt == 0 and not self.direct:
                    continue
                raise FinanceError('Finance could not be read (HTTP %s). No records were changed.' % error.code) from None
            except FinanceError:
                raise
            except Exception:
                raise FinanceError('Finance is unavailable or returned an invalid response. No records were changed.') from None

    def dataset(self):
        result = self.read('/state')
        state = result.get('state')
        if result.get('schemaVersion') != 1 or type(result.get('revision')) is not int or not isinstance(state, dict):
            raise FinanceError('Finance has no readable supported dataset. No default data will be substituted.')
        if not all(isinstance(state.get(k), list) for k in ('transactions', 'snapshots')) or not isinstance(state.get('payeeOverrides'), dict):
            raise FinanceError('Finance is missing expected saved records.')
        return result

    def check_contract(self):
        """Fail closed if app semantics change; raw records remain readable."""
        code = self.read('/app.js')
        if hashlib.sha256(code.encode()).hexdigest() != CONTRACT['app_sha256']:
            raise FinanceError('The Finance app changed since this connector was verified. Update the connector’s app contract before using summaries or refresh; raw transactions and saved fields remain available.')

    def save_state(self, revision, state, mutation_id):
        """Internal transport; MCP edits are constrained and journalled in editing.py."""
        payload = json.dumps({'revision': revision, 'state': state, 'mutationId': mutation_id}, allow_nan=False).encode()
        if len(payload) > 20 * 1024 * 1024:
            raise FinanceError('The saved dataset would exceed the app’s size limit.')
        for attempt in range(2):
            session = self.session(renew=attempt == 1)
            request = Request(session['url'] + '/state', data=payload, method='POST', headers=self._headers(session, {'Content-Type': 'application/json', 'X-Finance-Client': CONTRACT['app_version']}))
            try:
                with build_opener(NoRedirect()).open(request, timeout=25) as response:
                    result = json.loads(response.read(20 * 1024 * 1024 + 1))
                if result.get('mutationId') != mutation_id or result.get('revision') != revision + 1:
                    raise FinanceError('The save response could not be verified. Retry with the same preview and request ID to check whether it saved.')
                return result
            except HTTPError as error:
                if error.code in (401, 403) and attempt == 0 and not self.direct:
                    continue
                if error.code == 409:
                    raise FinanceError('Finance changed on another device or its app version changed. This edit was not applied. Read the latest data and create a new preview.') from None
                raise FinanceError('Finance rejected the save (HTTP %s). No confirmed edit; check current records before retrying.' % error.code) from None
            except FinanceError:
                raise
            except Exception:
                raise FinanceError('The save outcome is uncertain. Retry the same preview and request ID; do not create another edit until the receipt has been checked.') from None


def atomic_json(path, value):
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
