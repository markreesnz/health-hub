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
exec python3 server.py serve
