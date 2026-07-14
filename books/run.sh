#!/usr/bin/env sh
set -e
# Backups persist on /share (survive add-on updates/rebuilds).
mkdir -p /share/books/backups
export BOOKS_BACKUP_DIR=/share/books/backups

# Inject the API key from the add-on Configuration tab (never committed to the repo).
if [ -f /data/options.json ]; then
  export ANTHROPIC_API_KEY="$(python3 -c "import json;print(json.load(open('/data/options.json')).get('anthropic_api_key',''))" 2>/dev/null)"
  export WCL_CARD="$(python3 -c "import json;print(json.load(open('/data/options.json')).get('wcl_card',''))" 2>/dev/null)"
  export WCL_PIN="$(python3 -c "import json;print(json.load(open('/data/options.json')).get('wcl_pin',''))" 2>/dev/null)"
fi

# Pull the latest app from GitHub on start so HTML-only updates need just an add-on
# RESTART (no version bump). Falls back to the baked-in copy if unreachable.
if python3 - <<'PY'
import urllib.request, os, sys, json, base64
api = "https://api.github.com/repos/markreesnz/health-hub/contents/books/index.html?ref=main"
try:
    req = urllib.request.Request(api, headers={"User-Agent": "books-addon", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = base64.b64decode(json.load(r)["content"])
    assert len(data) > 1000, "suspiciously small"
    os.makedirs("/share/books", exist_ok=True)
    with open("/share/books/index.html", "wb") as f:
        f.write(data)
    print("app: pulled latest from GitHub API (%d bytes)" % len(data))
except Exception as e:
    print("app: GitHub pull failed (%s) - serving baked-in copy" % e)
    sys.exit(1)
PY
then
  export BOOKS_HTML=/share/books/index.html
fi

exec python3 books-proxy.py
