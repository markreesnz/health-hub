"""Client for the Health Hub add-on on the Supervisor's private network.

The hub owns all health data and every rule; this connector only reads its
fixed JSON endpoints and calls its idempotent write endpoints.
"""
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

DEFAULT_CONFIG = '/data/connection.json'
READ_PATHS = frozenset(['/today', '/targets', '/summary/range', '/review/weekly', '/alcohol'])
WRITE_PATHS = frozenset(['/alcohol/drink', '/food/parse', '/food/items'])


class HealthError(Exception):
    """A safe, user-facing error; never includes credentials."""


def base_url() -> str:
    config = json.loads(Path(os.environ.get('HEALTH_CONFIG', DEFAULT_CONFIG)).read_text())
    base = config['health_url'].rstrip('/')
    url = urlparse(base)
    if url.scheme != 'http' or not url.hostname or url.username or url.password or url.path or url.query:
        raise HealthError('The Health Hub address must be an internal http://host:port origin.')
    return base


def _call(method: str, path: str, query: dict | None = None, body: dict | None = None, timeout: int = 90):
    url = base_url() + path + ('?' + urlencode({k: v for k, v in (query or {}).items() if v is not None}) if query else '')
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, method=method, headers={'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            message = json.load(error).get('error') or ''
        except ValueError:
            message = ''
        raise HealthError(f'Health Hub refused the request ({error.code}): {message[:300]}') from None
    except (URLError, TimeoutError, OSError):
        raise HealthError('The Health Hub add-on did not answer. Check it is running.') from None


def get(path: str, **query):
    if path not in READ_PATHS:
        raise HealthError('Not an allowed Health Hub read.')
    return _call('GET', path, query)


def post(path: str, body: dict):
    if path not in WRITE_PATHS:
        raise HealthError('Not an allowed Health Hub write.')
    return _call('POST', path, body=body)
