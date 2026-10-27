"""Constants for the health connector integration."""

DOMAIN = "health_mcp"
API_PATH = "/api/health_mcp"
TIMEOUT_SECONDS = 120
MAX_BODY_BYTES = 1024 * 1024
# Per Home Assistant user: a check-in or meal log makes a handful of MCP calls.
RATE_LIMIT_REQUESTS = 60
RATE_LIMIT_WINDOW_SECONDS = 60
