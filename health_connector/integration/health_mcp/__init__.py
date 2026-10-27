"""Health connector: exposes the Health Connector add-on at /api/health_mcp.

The endpoint sits behind Home Assistant's own authentication (the OAuth flow
Claude uses through Nabu Casa). Only admin users may call it, each user is
rate limited, and the request is forwarded to the add-on on the Supervisor's
private network with a shared secret that the add-on writes next to this file.
The add-on logs every request with the user name passed here.
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
    MAX_BODY_BYTES,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    TIMEOUT_SECONDS,
)

_LOGGER = logging.getLogger(__name__)
HERE = Path(__file__).parent
_VIEW_KEY = f"{DOMAIN}_views_registered"

MCP_REQUEST_HEADERS = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
MCP_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "cache-control")

_hits: dict[str, deque[float]] = defaultdict(deque)


def _read_link() -> tuple[str, str]:
    upstream = (HERE / ".upstream").read_text().strip().rstrip("/")
    secret = (HERE / ".proxy_secret").read_text().strip()
    if not upstream.startswith("http://") or len(secret) < 32:
        raise ValueError("Health Connector add-on link files are invalid")
    return upstream, secret


def _rate_limited(user_id: str) -> bool:
    now = time.monotonic()
    hits = _hits[user_id]
    while hits and now - hits[0] > RATE_LIMIT_WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= RATE_LIMIT_REQUESTS:
        return True
    hits.append(now)
    return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register the proxy endpoint."""
    if not hass.data.get(_VIEW_KEY):
        hass.http.register_view(HealthMCPView())
        hass.data[_VIEW_KEY] = True
    hass.data[DOMAIN] = entry.entry_id
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Views cannot be unregistered; the view refuses requests while unloaded."""
    hass.data.pop(DOMAIN, None)
    return True


class HealthMCPView(HomeAssistantView):
    """Authenticated Streamable-HTTP proxy to the Health MCP server."""

    url = API_PATH
    name = "api:health_mcp"
    requires_auth = True

    async def _proxy(self, request: web.Request) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        if DOMAIN not in hass.data:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="Health connector is not set up")
        user = request["hass_user"]
        if user is None or not user.is_admin or user.system_generated:
            raise Unauthorized()
        if _rate_limited(user.id):
            _LOGGER.warning("Health connector: rate limit hit for %s", user.name)
            return web.Response(status=HTTPStatus.TOO_MANY_REQUESTS, headers={"Retry-After": "30"}, text="Too many requests")

        try:
            upstream, secret = await hass.async_add_executor_job(_read_link)
        except (OSError, ValueError):
            _LOGGER.error("Health Connector add-on link files missing; start the add-on")
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE, text="Health Connector add-on is not running")

        body = b""
        if request.method == "POST":
            if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
                return web.Response(status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            # Read the whole body: StreamReader.read(n) may return only the first chunk.
            chunks: list[bytes] = []
            total = 0
            async for chunk in request.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > MAX_BODY_BYTES:
                    return web.Response(status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                chunks.append(chunk)
            body = b"".join(chunks)

        headers = {k: v for k, v in request.headers.items() if k.lower() in MCP_REQUEST_HEADERS}
        headers["X-Health-Proxy-Secret"] = secret
        headers["X-Health-User"] = (user.name or user.id)[:64]
        session = async_get_clientsession(hass)
        try:
            async with session.request(
                request.method,
                upstream + "/mcp",
                data=body if request.method == "POST" else None,
                headers=headers,
                timeout=ClientTimeout(total=TIMEOUT_SECONDS),
                allow_redirects=False,
                auto_decompress=False,
            ) as upstream_response:
                response = web.StreamResponse(status=upstream_response.status)
                for key in MCP_RESPONSE_HEADERS:
                    if key in upstream_response.headers:
                        response.headers[key] = upstream_response.headers[key]
                await response.prepare(request)
                async for chunk in upstream_response.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (ClientError, TimeoutError):
            _LOGGER.warning("Health Connector add-on did not answer")
            return web.Response(status=HTTPStatus.BAD_GATEWAY, text="Health Connector add-on did not answer")

    async def post(self, request: web.Request) -> web.StreamResponse:
        return await self._proxy(request)

    async def get(self, request: web.Request) -> web.StreamResponse:
        return await self._proxy(request)

    async def delete(self, request: web.Request) -> web.StreamResponse:
        return await self._proxy(request)
