"""Constants for the WSET coach connector integration."""

DOMAIN = "wset_mcp"
API_PATH = "/api/wset_mcp"
GIT_PATH = "/api/wset_mcp/git/{path:.*}"
TIMEOUT_SECONDS = 120
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_GIT_BODY_BYTES = 32 * 1024 * 1024
# Per Home Assistant user, per endpoint: a coaching turn makes a handful of
# MCP calls; a git pull/push makes two or three requests.
RATE_LIMIT_REQUESTS = 60
RATE_LIMIT_WINDOW_SECONDS = 60
