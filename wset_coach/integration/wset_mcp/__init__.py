"""WSET coach connector: exposes the WSET Coach Connector add-on at /api/wset_mcp.

Two endpoints, both behind Home Assistant's own authentication (the OAuth
flow Claude uses through Nabu Casa, or a long-lived admin token from the Mac):

- /api/wset_mcp          the Study Coach MCP server (streamable HTTP)
- /api/wset_mcp/git/...  git smart HTTP for the coaching workspace, so the Mac
                         working copy can pull and push

Only admin users may call them, each user is rate limited per endpoint, and
the request is forwarded to the add-on on the Supervisor's private network
with a shared secret that the add-on writes next to this file. The add-on
logs every request with the user name passed here.
"""

from __future__ import annotations

from collections import defaultdict, deque
from http import HTTPStatus
import logging
from pathlib import Path
import time

from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_PATH,
    DOMAIN,
    GIT_PATH,
    MAX_BODY_BYTES,
    MAX_GIT_BODY_BYTES,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    TIMEOUT_SECONDS,
)

_LOGGER = logging.getLogger(__name__)
HERE = Path(__file__).parent
_VIEW_KEY = f"{DOMAIN}_views_registered"

MCP_REQUEST_HEADERS = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
MCP_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "cache-control")
GIT_REQUEST_HEADERS = ("content-type", "accept", "content-encoding", "git-protocol")
GIT_RESPONSE_HEADERS = ("content-type", "cache-control", "expires", "pragma")

_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def _read_link() -> tuple[str, str]:
    upstream = (HERE / ".upstream").read_text().strip().rstrip("/")
    secret = (HERE / ".proxy_secret").read_text().strip()
    if not upstream.startswith("http://") or len(secret) < 32:
        raise ValueError("WSET Coach Connector add-on link files are invalid")
    return upstream, secret


def _rate_limited(user_id: str, endpoint: str) -> bool:
    now = time.monotonic()
    hits = _hits[(user_id, endpoint)]
    while hits and now - hits[0] > RATE_LIMIT_WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= RATE_LIMIT_REQUESTS:
        return True
    hits.append(now)
    return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register the proxy endpoints."""
    if not hass.data.get(_VIEW_KEY):
        hass.http.register_view(WsetMCPView())
        hass.http.register_view(WsetGitView())
        hass.data[_VIEW_KEY] = True
    hass.data[DOMAIN] = entry.entry_id
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Views cannot be unregistered; the views refuse requests while unloaded."""
    hass.data.pop(DOMAIN, None)
    return True


class _ProxyView(HomeAssistantView):
    requires_auth = True
    endpoint = ""
    upstream_path = ""
    max_body = MAX_BODY_BYTES
    request_headers: tuple[str, ...] = ()
    response_headers: tuple[str, ...] = ()

    def _target(self, request: web.Request, upstream: str) -> str:
        return upstream + self.upstream_path

    async def _proxy(self, request: web.Request) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        if DOMAIN not in hass.data:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="WSET coach connector is not set up")
        user = request["hass_user"]
        if user is None or not user.is_admin or user.system_generated:
            raise Unauthorized()
        if _rate_limited(user.id, self.endpoint):
            _LOGGER.warning("WSET coach connector: rate limit hit for %s on %s", user.name, self.endpoint)
            return web.Response(status=HTTPStatus.TOO_MANY_REQUESTS, headers={"Retry-After": "30"}, text="Too many requests")

        try:
            upstream, secret = await hass.async_add_executor_job(_read_link)
        except (OSError, ValueError):
            _LOGGER.error("WSET Coach Connector add-on link files missing; start the add-on")
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE, text="WSET Coach Connector add-on is not running")

        body = b""
        if request.method == "POST":
            if request.content_length is not None and request.content_length > self.max_body:
                return web.Response(status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            # Read the whole body: StreamReader.read(n) may return only the first chunk.
            chunks: list[bytes] = []
            total = 0
            async for chunk in request.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > self.max_body:
                    return web.Response(status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                chunks.append(chunk)
            body = b"".join(chunks)

        headers = {k: v for k, v in request.headers.items() if k.lower() in self.request_headers}
        headers["X-WSET-Proxy-Secret"] = secret
        headers["X-WSET-User"] = (user.name or user.id)[:64]
        session = async_get_clientsession(hass)
        try:
            async with session.request(
                request.method,
                self._target(request, upstream),
                data=body if request.method == "POST" else None,
                headers=headers,
                timeout=ClientTimeout(total=TIMEOUT_SECONDS),
                allow_redirects=False,
                auto_decompress=False,
            ) as upstream_response:
                response = web.StreamResponse(status=upstream_response.status)
                for key in self.response_headers:
                    if key in upstream_response.headers:
                        response.headers[key] = upstream_response.headers[key]
                await response.prepare(request)
                async for chunk in upstream_response.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (ClientError, TimeoutError):
            _LOGGER.warning("WSET Coach Connector add-on did not answer")
            return web.Response(status=HTTPStatus.BAD_GATEWAY, text="WSET Coach Connector add-on did not answer")


class WsetMCPView(_ProxyView):
    """Authenticated Streamable-HTTP proxy to the Study Coach MCP server."""

    url = API_PATH
    name = "api:wset_mcp"
    endpoint = "mcp"
    upstream_path = "/mcp"
    request_headers = MCP_REQUEST_HEADERS
    response_headers = MCP_RESPONSE_HEADERS

    async def post(self, request: web.Request) -> web.StreamResponse:
        return await self._proxy(request)

    async def get(self, request: web.Request) -> web.StreamResponse:
        return await self._proxy(request)

    async def delete(self, request: web.Request) -> web.StreamResponse:
        return await self._proxy(request)


class WsetGitView(_ProxyView):
    """Authenticated git smart-HTTP proxy for the coaching workspace (Mac sync)."""

    url = GIT_PATH
    name = "api:wset_mcp:git"
    endpoint = "git"
    max_body = MAX_GIT_BODY_BYTES
    request_headers = GIT_REQUEST_HEADERS
    response_headers = GIT_RESPONSE_HEADERS

    def _target(self, request: web.Request, upstream: str) -> str:
        path = request.match_info.get("path", "")
        target = f"{upstream}/git/{path}"
        if request.query_string:
            target += "?" + request.query_string
        return target

    async def get(self, request: web.Request, path: str = "") -> web.StreamResponse:
        return await self._proxy(request)

    async def post(self, request: web.Request, path: str = "") -> web.StreamResponse:
        return await self._proxy(request)
