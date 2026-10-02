"""Finance connector: exposes the Finance Connector add-on's MCP server at /api/finance_mcp.

Authentication is Home Assistant's own (the same OAuth flow Claude uses for the
built-in MCP server via Nabu Casa). Only admin users may call it. The request is
forwarded to the add-on on the Supervisor's private network with a shared secret
that the add-on writes next to this file.
"""

from __future__ import annotations

from http import HTTPStatus
import logging
from pathlib import Path

from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import API_PATH, DOMAIN, MAX_BODY_BYTES, TIMEOUT_SECONDS

_LOGGER = logging.getLogger(__name__)
HERE = Path(__file__).parent
_VIEW_KEY = f"{DOMAIN}_view_registered"

FORWARD_REQUEST_HEADERS = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
FORWARD_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "cache-control")


def _read_link() -> tuple[str, str]:
    upstream = (HERE / ".upstream").read_text().strip().rstrip("/")
    secret = (HERE / ".proxy_secret").read_text().strip()
    if not upstream.startswith("http://") or len(secret) < 32:
        raise ValueError("Finance Connector add-on link files are invalid")
    return upstream, secret


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register the proxy endpoint."""
    if not hass.data.get(_VIEW_KEY):
        hass.http.register_view(FinanceMCPView())
        hass.data[_VIEW_KEY] = True
    hass.data[DOMAIN] = entry.entry_id
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Views cannot be unregistered; the view refuses requests while unloaded."""
    hass.data.pop(DOMAIN, None)
    return True


class FinanceMCPView(HomeAssistantView):
    """Authenticated Streamable-HTTP proxy to the Finance Connector add-on."""

    url = API_PATH
    name = "api:finance_mcp"
    requires_auth = True

    async def post(self, request: web.Request) -> web.StreamResponse:
        return await self._proxy(request)

    async def get(self, request: web.Request) -> web.StreamResponse:
        return await self._proxy(request)

    async def delete(self, request: web.Request) -> web.StreamResponse:
        return await self._proxy(request)

    async def _proxy(self, request: web.Request) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        if DOMAIN not in hass.data:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="Finance connector is not set up")
        user = request["hass_user"]
        if user is None or not user.is_admin or user.system_generated:
            raise Unauthorized()

        try:
            upstream, secret = await hass.async_add_executor_job(_read_link)
        except (OSError, ValueError):
            _LOGGER.error("Finance Connector add-on link files missing; start the add-on")
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE, text="Finance Connector add-on is not running")

        body = b""
        if request.method == "POST":
            if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
                return web.Response(status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            body = await request.content.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                return web.Response(status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_REQUEST_HEADERS}
        headers["X-Finance-Proxy-Secret"] = secret
        session = async_get_clientsession(hass)
        try:
            async with session.request(
                request.method,
                upstream + "/mcp",
                data=body if request.method == "POST" else None,
                headers=headers,
                timeout=ClientTimeout(total=TIMEOUT_SECONDS),
                allow_redirects=False,
            ) as upstream_response:
                response = web.StreamResponse(status=upstream_response.status)
                for key in FORWARD_RESPONSE_HEADERS:
                    if key in upstream_response.headers:
                        response.headers[key] = upstream_response.headers[key]
                await response.prepare(request)
                async for chunk in upstream_response.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (ClientError, TimeoutError):
            _LOGGER.warning("Finance Connector add-on did not answer")
            return web.Response(status=HTTPStatus.BAD_GATEWAY, text="Finance Connector add-on did not answer")
