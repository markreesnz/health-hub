#!/usr/bin/env python3
"""Pull the map images into /share on start.

There is no addon_update service reachable over Nabu Casa, so anything baked
into the image would need a manual UI update. Fetching at runtime means a
restart is enough to ship new or changed maps.
"""
import json, os, sys, urllib.request

RAW = "https://raw.githubusercontent.com/markreesnz/health-hub/main/wset/maps/"
API = "https://api.github.com/repos/markreesnz/health-hub/contents/wset/maps?ref=main"
DEST = os.environ.get("WSET_MAPS", "/share/wset/maps")

try:
    os.makedirs(DEST, exist_ok=True)
    req = urllib.request.Request(API, headers={"User-Agent": "wset-addon",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        listing = json.load(r)
    want = {f["name"]: f["size"] for f in listing if f["name"].endswith((".jpg", ".png"))}
    fetched = 0
    for name, size in want.items():
        path = os.path.join(DEST, name)
        if os.path.exists(path) and abs(os.path.getsize(path) - size) < 64:
            continue
        with urllib.request.urlopen(RAW + name, timeout=60) as r, open(path, "wb") as f:
            f.write(r.read())
        fetched += 1
    print("maps: %d fetched, %d already current" % (fetched, len(want) - fetched), flush=True)
except Exception as e:
    print("maps: pull failed (%s) - serving whatever is present" % e, flush=True)
    sys.exit(0)
