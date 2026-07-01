#!/usr/bin/env sh
set -e
# Persist logs, schedule, cache and backups on /share (survive add-on updates/rebuilds).
mkdir -p /share/energy/backups
export ADDON=1
export ENERGY_DATA_DIR=/share/energy
export ENERGY_BACKUP_DIR=/share/energy/backups
export ENERGY_OCTO_CFG=/share/energy/octopus-config.json
# HA Green enforces the hot-water schedule from /config/scripts/ — mounted here as /homeassistant.
export HA_HOTWATER_SCHED_PATH=/homeassistant/scripts/hotwater_schedule.json

# Inject secrets from the add-on Configuration tab (never committed to the repo).
if [ -f /data/options.json ]; then
  export ANTHROPIC_API_KEY="$(python3 -c "import json;print(json.load(open('/data/options.json')).get('anthropic_api_key',''))" 2>/dev/null)"
  # Build the Octopus config the proxy expects from the individual options.
  python3 - <<'PY'
import json, os
o = json.load(open("/data/options.json"))
os.makedirs("/share/energy", exist_ok=True)
json.dump({
    "email":        o.get("octopus_email", ""),
    "password":     o.get("octopus_password", ""),
    "icp":          o.get("octopus_icp", ""),
    "account":      o.get("octopus_account", ""),
    "property_id":  o.get("octopus_property_id", "27574"),
    "supply_start": o.get("octopus_supply_start", "2026-06-03"),
}, open("/share/energy/octopus-config.json", "w"))
PY
fi

# Pull the latest dashboard from GitHub on start, so app updates need only an add-on RESTART
# (no version bump / store refresh / HA update). Falls back to the baked-in copy if unreachable.
if python3 - <<'PY'
import urllib.request, os, sys, json, base64
# GitHub contents API (not raw.githubusercontent.com — its CDN serves stale copies for minutes).
api = "https://api.github.com/repos/markreesnz/health-hub/contents/energy/index.html?ref=main"
try:
    req = urllib.request.Request(api, headers={"User-Agent": "energy-addon", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = base64.b64decode(json.load(r)["content"])
    assert len(data) > 1000, "suspiciously small"
    os.makedirs("/share/energy", exist_ok=True)
    with open("/share/energy/index.html", "wb") as f:
        f.write(data)
    print("dashboard: pulled latest from GitHub API (%d bytes)" % len(data))
except Exception as e:
    print("dashboard: GitHub pull failed (%s) - serving baked-in copy" % e)
    sys.exit(1)
PY
then
  export ENERGY_HTML=/share/energy/index.html
fi

exec python3 energy-proxy.py --no-browser
