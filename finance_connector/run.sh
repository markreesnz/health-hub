#!/bin/sh
# Finance Connector add-on entrypoint.
set -eu

FINANCE_URL=$(python3 -c "import json;print(json.load(open('/data/options.json')).get('finance_url','').rstrip('/'))")

# Shared secret between this add-on and the finance_mcp integration (never leaves the Green).
if [ ! -s /data/proxy_secret ]; then
  python3 -c "import secrets;print(secrets.token_urlsafe(48))" > /data/proxy_secret
fi
chmod 600 /data/proxy_secret

mkdir -p /data/jobs /share/finance_connector/planning
chmod 700 /share/finance_connector /share/finance_connector/planning
python3 - "$FINANCE_URL" <<'PY'
import json, sys
json.dump({'finance_url': sys.argv[1], 'jobs_dir': '/data/jobs'}, open('/data/connection.json', 'w'))
PY

# Install / update the companion Home Assistant integration.
DEST=/homeassistant/custom_components/finance_mcp
mkdir -p "$DEST"
cp -R /integration/finance_mcp/. "$DEST/"
python3 -c "import socket;print('http://%s:8099' % socket.gethostname())" > "$DEST/.upstream"
cp /data/proxy_secret "$DEST/.proxy_secret"
chmod 600 "$DEST/.proxy_secret"
echo "[finance-connector] integration files installed in $DEST (restart Home Assistant after first install or update)"

# Check that the Finance app is reachable; keep running either way so the logs explain it.
python3 - "$FINANCE_URL" <<'PY' || true
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1] + '/status', timeout=10) as r:
        s = json.load(r)
    print('[finance-connector] Finance app reachable: version %s, revision %s' % (s.get('version'), s.get('revision')))
except Exception as e:
    print('[finance-connector] WARNING: cannot reach the Finance app at %s (%s). Check the finance_url option.' % (sys.argv[1], type(e).__name__))
PY

export FINANCE_CONFIG=/data/connection.json
export FINANCE_PROXY_SECRET_FILE=/data/proxy_secret
export FINANCE_PLANNING_DIR=/share/finance_connector/planning
echo "[finance-connector] MCP server listening on :8099 (private network only)"
exec python3 /app/server.py http
