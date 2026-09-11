#!/usr/bin/env sh
set -e
# Persist current state and recovery backups outside the container.
mkdir -p /share/financial/backups
export FIN_DATA_DIR=/share/financial
export ADDON=1          # suppress the Mac-only browser-open on startup
# Inject secrets from the add-on Configuration tab (never committed to the repo).
if [ -f /data/options.json ]; then
  export AKAHU_APP_TOKEN="$(python3 -c "import json;print(json.load(open('/data/options.json')).get('akahu_app_token',''))" 2>/dev/null)"
  export AKAHU_USER_TOKEN="$(python3 -c "import json;print(json.load(open('/data/options.json')).get('akahu_user_token',''))" 2>/dev/null)"
fi
export PYTHONUNBUFFERED=1
export TZ=Pacific/Auckland
# Frontend and backend are released together in the image.
exec python3 akahu-proxy.py
