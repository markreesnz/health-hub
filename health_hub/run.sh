#!/usr/bin/env sh
set -e
# Persist state + backups + the generated plan on /share so they survive updates/rebuilds.
mkdir -p /share/health/data /share/health/backups
ln -sfn /share/health/data /app/data
ln -sfn /share/health/backups /app/backups
# program.json is the live (generated) plan — seed it from the bundled copy once, then keep it
# on /share so a GitHub update never resets your block/macrocycle position.
if [ ! -f /share/health/program.json ]; then cp /app/program.json /share/health/program.json; fi
ln -sfn /share/health/program.json /app/program.json
# Inject the secrets from the add-on Configuration tab as env vars (never in the repo).
if [ -f /data/options.json ]; then
  export ANTHROPIC_API_KEY="$(python3 -c "import json;print(json.load(open('/data/options.json')).get('anthropic_api_key',''))" 2>/dev/null)"
  export NOTION_TOKEN="$(python3 -c "import json;print(json.load(open('/data/options.json')).get('notion_token',''))" 2>/dev/null)"
fi
# Pull the latest dashboard from GitHub on start, so HTML-only changes need just an add-on RESTART
# (no version bump / rebuild). The GET / handler serves /share/health/index.html when present, so we
# refresh it here every start — this also means a stale override can never shadow an update. Falls
# back to the freshly-rebuilt baked-in copy if GitHub is unreachable.
python3 - <<'PY' || cp /app/index.html /share/health/index.html 2>/dev/null || true
import urllib.request, base64, json
# GitHub contents API (not raw.githubusercontent.com — its CDN serves stale copies for minutes).
api = "https://api.github.com/repos/markreesnz/health-hub/contents/health_hub/index.html?ref=main"
req = urllib.request.Request(api, headers={"User-Agent": "health-addon", "Accept": "application/vnd.github+json"})
data = base64.b64decode(json.load(urllib.request.urlopen(req, timeout=20))["content"])
assert len(data) > 1000, "suspiciously small"
open("/share/health/index.html", "wb").write(data)
print("dashboard: pulled latest from GitHub API (%d bytes)" % len(data))
PY
exec python3 server.py serve
