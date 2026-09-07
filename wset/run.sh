#!/usr/bin/env sh
set -e
mkdir -p /share/wset /share/wset/maps
python3 fetch_maps.py

# Pull the latest app from GitHub on start, so content-only updates need just a
# RESTART rather than a version bump.
#
# 7 Sep 2026: this was falling back to the BAKED-IN copy whenever the pull failed,
# which silently served an image-age app — weeks old — while every deploy reported
# success. Two changes: try the API then raw.githubusercontent (the API is 60/hour
# per IP and a day of deploys can exhaust it), and on failure keep the LAST GOOD
# PULL in /share, which persists across restarts, rather than the stale image.
if python3 - <<'PY'
import urllib.request, json, base64, os, sys

DEST = "/share/wset/index.html"
RAW  = "https://raw.githubusercontent.com/markreesnz/health-hub/main/wset/index.html?cb="
API  = "https://api.github.com/repos/markreesnz/health-hub/contents/wset/index.html?ref=main"
HDR  = {"User-Agent": "wset-addon", "Cache-Control": "no-cache", "Pragma": "no-cache"}


def fetch(url, decode_json=False):
    req = urllib.request.Request(url, headers=dict(HDR, **(
        {"Accept": "application/vnd.github+json"} if decode_json else {})))
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return base64.b64decode(json.loads(raw)["content"]) if decode_json else raw


data, how = None, ""
# The API is authoritative and immediate. raw.githubusercontent sits behind a CDN
# that was measured serving a build hours old on 7 Sep, so it is the fallback, not
# the primary — it only matters when the API quota (60/hour per IP) is exhausted.
for url, is_api in ((API, True), (RAW, False)):
    try:
        d = fetch(url + (str(int(__import__("time").time())) if url.endswith("cb=") else ""), is_api)
        if len(d) < 100_000:
            raise ValueError("suspiciously small: %d bytes" % len(d))
        data, how = d, ("API" if is_api else "raw")
        break
    except Exception as e:
        print("app: %s fetch failed (%s)" % ("API" if is_api else "raw", e))

if data:
    with open(DEST, "wb") as f:
        f.write(data)
    print("app: pulled from GitHub via %s (%d bytes)" % (how, len(data)))
    sys.exit(0)

# Both fetches failed. The last good pull is far newer than the image, so prefer it.
if os.path.isfile(DEST) and os.path.getsize(DEST) > 100_000:
    print("app: GitHub unreachable - serving the LAST GOOD PULL from /share")
    sys.exit(0)

print("app: GitHub unreachable and no cached copy - serving the baked-in image")
sys.exit(1)
PY
then
  export WSET_HTML=/share/wset/index.html
fi

# Report what is actually being served, so a stale app is visible in the log.
if [ -n "$WSET_HTML" ] && [ -f "$WSET_HTML" ]; then
  echo "app: serving $WSET_HTML  [$(grep -o 'build [0-9]* [A-Za-z]* [0-9:]* . [a-f0-9]*' "$WSET_HTML" | tail -1)]"
else
  echo "app: serving the baked-in /app/index.html  [$(grep -o 'build [0-9]* [A-Za-z]* [0-9:]* . [a-f0-9]*' /app/index.html | tail -1)]"
fi

exec python3 server.py
