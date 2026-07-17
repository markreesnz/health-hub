#!/usr/bin/env sh
set -e
mkdir -p /share/journal
export JOURNAL_HTML=/app/index.html

# Pull the latest page from GitHub on start, so edits need only an add-on RESTART
# (no version bump / store refresh). Falls back to the baked-in copy if unreachable.
if python3 - <<'PY'
import urllib.request, os, sys, json, base64
api = "https://api.github.com/repos/markreesnz/health-hub/contents/journal/index.html?ref=main"
try:
    req = urllib.request.Request(api, headers={"User-Agent": "journal-addon", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = base64.b64decode(json.load(r)["content"])
    assert len(data) > 500, "suspiciously small"
    os.makedirs("/share/journal", exist_ok=True)
    with open("/share/journal/index.html", "wb") as f:
        f.write(data)
    print("journal: pulled latest from GitHub API (%d bytes)" % len(data))
except Exception as e:
    print("journal: GitHub pull failed (%s) - serving baked-in copy" % e)
    sys.exit(1)
PY
then
  export JOURNAL_HTML=/share/journal/index.html
fi

exec python3 server.py
