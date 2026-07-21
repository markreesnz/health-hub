#!/usr/bin/env sh
set -e
mkdir -p /share/reader

# Pull the latest app from GitHub on start so HTML-only updates need just a
# RESTART (no version bump). Falls back to the baked-in copy if unreachable.
if python3 - <<'PY'
import urllib.request, os, sys, json, base64
api = "https://api.github.com/repos/markreesnz/health-hub/contents/reader/index.html?ref=main"
try:
    req = urllib.request.Request(api, headers={"User-Agent": "reader-addon", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = base64.b64decode(json.load(r)["content"])
    assert len(data) > 1000, "suspiciously small"
    with open("/share/reader/index.html", "wb") as f:
        f.write(data)
    print("app: pulled latest from GitHub API (%d bytes)" % len(data))
except Exception as e:
    print("app: GitHub pull failed (%s) - serving baked-in copy" % e)
    sys.exit(1)
PY
then
  export READER_HTML=/share/reader/index.html
fi

exec python3 reader-proxy.py
