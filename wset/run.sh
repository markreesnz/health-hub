#!/usr/bin/env sh
set -e
mkdir -p /share/wset /share/wset/maps
python3 fetch_maps.py

# Pull the latest app from GitHub on start, so content-only updates need just a
# RESTART rather than a version bump. Falls back to the baked-in copy.
if python3 - <<'PY'
import urllib.request, os, sys, json, base64
api = "https://api.github.com/repos/markreesnz/health-hub/contents/wset/index.html?ref=main"
try:
    req = urllib.request.Request(api, headers={"User-Agent": "wset-addon",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = base64.b64decode(json.load(r)["content"])
    assert len(data) > 100_000, "suspiciously small"
    with open("/share/wset/index.html", "wb") as f:
        f.write(data)
    print("app: pulled latest from GitHub (%d bytes)" % len(data))
except Exception as e:
    print("app: GitHub pull failed (%s) - serving baked-in copy" % e)
    sys.exit(1)
PY
then
  export WSET_HTML=/share/wset/index.html
fi

exec python3 server.py
