#!/usr/bin/env sh
set -e
mkdir -p /share/wine

# Pull latest index.html from GitHub on start so HTML-only changes need only a restart.
if python3 - <<'PY'
import urllib.request, os, sys, json, base64
api = "https://api.github.com/repos/markreesnz/health-hub/contents/wine/index.html?ref=main"
try:
    req = urllib.request.Request(api, headers={"User-Agent": "wine-addon", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = base64.b64decode(json.load(r)["content"])
    assert len(data) > 1000, "suspiciously small"
    with open("/share/wine/index.html", "wb") as f:
        f.write(data)
    print("app: pulled latest from GitHub (%d bytes)" % len(data))
except Exception as e:
    print("app: GitHub pull failed (%s) — serving baked-in copy" % e)
    sys.exit(1)
PY
then
  export WINE_HTML=/share/wine/index.html
fi

exec python3 server.py
