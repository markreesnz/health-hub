#!/bin/sh
# Health Connector add-on entrypoint.
set -eu

HEALTH_URL=$(python3 -c "import json;print(json.load(open('/data/options.json')).get('health_url','').rstrip('/'))")

# Shared secret between this add-on and the health_mcp integration (never leaves the Green).
if [ ! -s /data/proxy_secret ]; then
  python3 -c "import secrets;print(secrets.token_urlsafe(48))" > /data/proxy_secret
fi
chmod 600 /data/proxy_secret

mkdir -p /data/previews /data/receipts
chmod 700 /data/previews /data/receipts
python3 - "$HEALTH_URL" <<'PY'
import json, sys
json.dump({'health_url': sys.argv[1]}, open('/data/connection.json', 'w'))
PY

# Install / update the companion Home Assistant integration.
DEST=/homeassistant/custom_components/health_mcp
mkdir -p "$DEST"
cp -R /integration/health_mcp/. "$DEST/"
python3 -c "import socket;print('http://%s:8099' % socket.gethostname())" > "$DEST/.upstream"
cp /data/proxy_secret "$DEST/.proxy_secret"
chmod 600 "$DEST/.proxy_secret"
echo "[health-connector] integration files installed in $DEST (restart Home Assistant after first install or update)"

# Check that the Health Hub is reachable; keep running either way so the logs explain it.
python3 - "$HEALTH_URL" <<'PY' || true
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1] + '/targets', timeout=10) as r:
        t = json.load(r)
    reset = (t.get('alcohol') or {}).get('reset') or {}
    print('[health-connector] Health Hub reachable; reset day %s of %s' % (reset.get('day'), reset.get('days')))
except Exception as e:
    print('[health-connector] WARNING: cannot reach the Health Hub at %s (%s). Check the health_url option and that Health Hub is 1.11.0 or later.' % (sys.argv[1], type(e).__name__))
PY

export HEALTH_CONFIG=/data/connection.json
export HEALTH_RUNTIME=/data
export HEALTH_PROXY_SECRET_FILE=/data/proxy_secret
echo "[health-connector] MCP server listening on :8099 (private network only); one JSON log line per request"
exec python3 /app/server.py http
