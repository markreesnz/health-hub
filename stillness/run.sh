#!/usr/bin/env sh
set -e
mkdir -p /share/stillness
export STILL_HTML=/app/index.html

# Pull the latest page from GitHub on start, so edits need only an add-on RESTART
# (no version bump / store refresh). Falls back to the baked-in copy if unreachable.
if python3 - <<'PY'
import urllib.request, os, sys, json, base64
api = "https://api.github.com/repos/markreesnz/health-hub/contents/stillness/index.html?ref=main"
try:
    req = urllib.request.Request(api, headers={"User-Agent": "stillness-addon", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = base64.b64decode(json.load(r)["content"])
    assert len(data) > 500, "suspiciously small"
    os.makedirs("/share/stillness", exist_ok=True)
    with open("/share/stillness/index.html", "wb") as f:
        f.write(data)
    print("stillness: pulled latest from GitHub API (%d bytes)" % len(data))
except Exception as e:
    print("stillness: GitHub pull failed (%s) - serving baked-in copy" % e)
    sys.exit(1)
PY
then
  export STILL_HTML=/share/stillness/index.html
fi

exec python3 server.py
