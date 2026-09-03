#!/usr/bin/env python3
"""WSET L3 add-on server.

Serves the study app and persists card progress + the daily timer to
/share/wset/state.json so it follows between the Mac and the phone.

  GET  /              the app
  GET  /api/state     the saved state blob
  POST /api/state     replace it
  GET  /api/health    liveness
"""
import json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT  = int(os.environ.get("WSET_PORT", "8776"))
HTML  = os.environ.get("WSET_HTML", "/app/index.html")
STORE = os.environ.get("WSET_STATE", "/share/wset/state.json")
MAPS  = os.environ.get("WSET_MAPS", "/app/maps")
LOCK  = threading.Lock()

os.makedirs(os.path.dirname(STORE), exist_ok=True)


def load():
    try:
        with open(STORE, encoding="utf8") as f:
            return json.load(f)
    except Exception:
        return {}


def save(d):
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(d, f)
    os.replace(tmp, STORE)          # atomic, so a crash can't truncate progress


# ---------------------------------------------------------------------------
# Merging state from two devices.
#
# The old code did cur.update(incoming), which merges only the TOP-LEVEL keys.
# But all the card progress lives inside ONE of those keys as a JSON string, so
# a stale client replaced the entire deck state wholesale — last-write-wins
# dressed up as a merge. That is how the phone could run ahead and then be
# overwritten by a Mac tab that had been open since before the phone's session.
#
# Progress is monotonic, so it can be merged semantically: for each card keep
# whichever record is FURTHER ALONG. Nothing a device has earned can be lost by
# syncing, in either direction.

PROGRESS_KEY = "wset-cards-v2"
UNION_KEYS   = ("wset-retired-v1", "wset-flags-v1")
COUNTER_KEY  = "wset-drills-v1"
ITEM_KEY     = "wset-drillitems-v1"


def _as_obj(v):
    """Values arrive as JSON strings. Return (dict, was_string) or (None, _)."""
    if isinstance(v, dict):
        return v, False
    if isinstance(v, str):
        try:
            d = json.loads(v or "{}")
            return (d, True) if isinstance(d, dict) else (None, True)
        except Exception:
            return None, True
    return None, False


def _ahead(a, b):
    """Is card record a further along than b? Ordered by next-review date, then
    by the streak counter. A card scheduled further out has survived more
    correct recalls, so it is the later state."""
    if not isinstance(b, dict):
        return True
    if not isinstance(a, dict):
        return False
    return (a.get("due", 0), a.get("k", 0)) >= (b.get("due", 0), b.get("k", 0))


def deep_merge(cur, incoming):
    out, kept = dict(cur), 0
    for k, v in incoming.items():
        if k == PROGRESS_KEY:
            new, was_str = _as_obj(v)
            old, _ = _as_obj(cur.get(k))
            if new is None:                       # unparseable: leave what we have
                continue
            if old is None:
                out[k] = v
                continue
            merged = dict(old)
            for ck, rec in new.items():
                if ck not in merged or _ahead(rec, merged[ck]):
                    merged[ck] = rec
                else:
                    kept += 1                     # server copy was further along
            out[k] = json.dumps(merged) if was_str else merged
        elif k == ITEM_KEY:
            # per-item drill history: counters take the higher, and the rolling
            # window takes whichever device recorded more recently
            new, was_str = _as_obj(v)
            old, _ = _as_obj(cur.get(k))
            if new is None:
                continue
            merged = dict(old or {})
            for rk, rec in new.items():
                have = merged.get(rk)
                if not isinstance(have, dict) or not isinstance(rec, dict):
                    merged[rk] = rec
                    continue
                newer = rec if rec.get("last", 0) >= have.get("last", 0) else have
                merged[rk] = dict(newer)
                merged[rk]["right"] = max(have.get("right", 0), rec.get("right", 0))
                merged[rk]["wrong"] = max(have.get("wrong", 0), rec.get("wrong", 0))
                merged[rk]["last"]  = max(have.get("last", 0),  rec.get("last", 0))
            out[k] = json.dumps(merged) if was_str else merged
        elif k == COUNTER_KEY:
            # drill scores are cumulative counters; two devices both add to them,
            # so take the higher of each rather than letting one device's total win
            new, was_str = _as_obj(v)
            old, _ = _as_obj(cur.get(k))
            if new is None:
                continue
            merged = dict(old or {})
            for rk, rec in new.items():
                have = merged.get(rk)
                if not isinstance(have, dict) or not isinstance(rec, dict):
                    merged[rk] = rec
                    continue
                merged[rk] = {
                    "runs":  max(have.get("runs", 0),  rec.get("runs", 0)),
                    "right": max(have.get("right", 0), rec.get("right", 0)),
                    "wrong": max(have.get("wrong", 0), rec.get("wrong", 0)),
                    "last":  max(have.get("last", 0),  rec.get("last", 0)),
                    "best":  min([x for x in (have.get("best"), rec.get("best")) if x is not None] or [None]),
                }
            out[k] = json.dumps(merged) if was_str else merged
        elif k in UNION_KEYS:
            new, was_str = _as_obj(v)
            old, _ = _as_obj(cur.get(k))
            if new is None:
                continue
            merged = dict(old or {})
            merged.update(new)                    # retirement is a tombstone: never undone by a sync
            out[k] = json.dumps(merged) if was_str else merged
        else:
            out[k] = v                            # timer and the like: last write wins
    return out, kept


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if "/maps/" in path and path.endswith((".jpg", ".png")):
            name = os.path.basename(path)
            if name != os.path.basename(os.path.normpath(name)):
                return self._send(400, json.dumps({"error": "bad name"}))
            f = os.path.join(MAPS, name)
            if not os.path.isfile(f):
                return self._send(404, json.dumps({"error": "no such map"}))
            with open(f, "rb") as fh:
                body = fh.read()
            ctype = "image/jpeg" if name.endswith(".jpg") else "image/png"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            return self.wfile.write(body)
        if path.endswith("/api/maps"):
            try: names = sorted(n for n in os.listdir(MAPS) if n.endswith((".jpg", ".png")))
            except FileNotFoundError: names = []
            return self._send(200, json.dumps(names))
        if path.endswith("/api/state"):
            with LOCK:
                return self._send(200, json.dumps(load()))
        if path.endswith("/api/health"):
            return self._send(200, json.dumps({"ok": True, "entries": len(load())}))
        try:
            with open(HTML, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        except FileNotFoundError:
            return self._send(404, json.dumps({"error": "app not found"}))

    def do_POST(self):
        if not self.path.split("?")[0].rstrip("/").endswith("/api/state"):
            return self._send(404, json.dumps({"error": "not found"}))
        n = int(self.headers.get("Content-Length") or 0)
        if n > 8_000_000:
            return self._send(413, json.dumps({"error": "too large"}))
        try:
            incoming = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(incoming, dict):
                raise ValueError("expected an object")
        except Exception as e:
            return self._send(400, json.dumps({"error": str(e)}))
        with LOCK:
            cur = load()
            merged, kept = deep_merge(cur, incoming)
            save(merged)
        return self._send(200, json.dumps({"ok": True, "entries": len(merged), "kept": kept}))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"WSET study app on :{PORT} · state {STORE}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
