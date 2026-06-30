#!/usr/bin/env sh
set -e
# Persist snapshots, backups and the AI-insights cache on /share (survive updates/rebuilds).
mkdir -p /share/financial/backups
export FIN_DATA_DIR=/share/financial
export ADDON=1          # suppress the Mac-only browser-open on startup
# Inject secrets from the add-on Configuration tab (never committed to the repo).
if [ -f /data/options.json ]; then
  export AKAHU_APP_TOKEN="$(python3 -c "import json;print(json.load(open('/data/options.json')).get('akahu_app_token',''))" 2>/dev/null)"
  export AKAHU_USER_TOKEN="$(python3 -c "import json;print(json.load(open('/data/options.json')).get('akahu_user_token',''))" 2>/dev/null)"
  export ANTHROPIC_API_KEY="$(python3 -c "import json;print(json.load(open('/data/options.json')).get('anthropic_api_key',''))" 2>/dev/null)"
fi
# Pull the latest dashboard from GitHub on start, so app updates need only an add-on RESTART
# (no version bump / store refresh / HA update). Falls back to the baked-in copy if unreachable.
if python3 - <<'PY'
import urllib.request, os, sys
url = "https://raw.githubusercontent.com/markreesnz/health-hub/main/financial/financial-plan-dashboard.html"
try:
    with urllib.request.urlopen(url, timeout=15) as r:
        data = r.read()
    assert len(data) > 1000, "suspiciously small"
    os.makedirs("/share/financial", exist_ok=True)
    with open("/share/financial/financial-plan-dashboard.html", "wb") as f:
        f.write(data)
    print("dashboard: pulled latest from GitHub (%d bytes)" % len(data))
except Exception as e:
    print("dashboard: GitHub pull failed (%s) - serving baked-in copy" % e)
    sys.exit(1)
PY
then
  export FIN_HTML_OVERRIDE=1
fi
exec python3 akahu-proxy.py
