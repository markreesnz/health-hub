#!/usr/bin/env python3
"""Records add-on server.

Serves the single-file vinyl-collection app and persists localStorage snapshots
to /share so the collection survives browser resets and migrates across devices.
Endpoints (relative — works through HA ingress and direct port):
  GET  /            the app
  GET  /config      {"anthropic_api_key": ...} from the add-on options
  POST /backup      store a full snapshot (one file per day)
  GET  /restore     latest stored snapshot
"""
import glob
import json
import os
import re
import time
import traceback
import urllib.parse
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8773"))
HTML = os.environ.get("RECORDS_HTML", os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))
BACKUP_DIR = os.environ.get("RECORDS_BACKUP_DIR", "/share/records/backups")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def push_backup_sensor():
    """Publish sensor.records_backup into HA. The Green's filesystem is unreachable
    from the Mac, so this sensor is the remote check that snapshots exist and how many
    albums each holds. Needs homeassistant_api: true in config.yaml."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return
    try:
        rows = []
        for path in sorted(glob.glob(os.path.join(BACKUP_DIR, "backup-*.json")))[-14:]:
            name = os.path.basename(path)
            try:
                with open(path) as f:
                    s = json.load(f)
                recs = s.get("records") or []
                rows.append({
                    "date": name[len("backup-"):-len(".json")],
                    "owned": len([r for r in recs if r.get("status") == "owned"]),
                    "want": len([r for r in recs if r.get("status") == "want"]),
                    "bytes": os.path.getsize(path),
                })
            except Exception as e:
                rows.append({"date": name, "error": str(e)})
        payload = {"state": str(rows[-1]["owned"]) if rows and "owned" in rows[-1] else "0",
                   "attributes": {"friendly_name": "Records backup",
                                  "unit_of_measurement": "albums",
                                  "files": rows}}
        req = urllib.request.Request(
            "http://supervisor/core/api/states/sensor.records_backup",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST")
        urllib.request.urlopen(req, timeout=10).read()
        print("backup sensor pushed", flush=True)
    except Exception:
        traceback.print_exc()


FN_CACHE = {}   # norm(artist|title) -> (ts, payload); Shopify is fast but be kind
FN_TTL = 1800


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def fn_lookup(artist, title):
    """Search the Flying Nun store (Shopify predictive search) for a vinyl pressing of
    the album and report stock. Products are titled "Artist - Album" and tagged Vinyl."""
    key = _norm(artist) + "|" + _norm(title)
    hit = FN_CACHE.get(key)
    if hit and time.time() - hit[0] < FN_TTL:
        return hit[1]
    q = urllib.parse.quote(f"{artist} {title}")
    url = ("https://www.flyingnun.co.nz/search/suggest.json?q=" + q +
           "&resources[type]=product&resources[limit]=10"
           "&resources[options][unavailable_products]=show")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (records-app)"})
    products = json.loads(urllib.request.urlopen(req, timeout=10).read())[
        "resources"]["results"]["products"]
    na, nt = _norm(artist), _norm(title)
    matches = []
    for p in products:
        if not any(_norm(t) == "vinyl" for t in p.get("tags") or []):
            continue
        pt = _norm(p.get("title"))
        if nt not in pt:
            continue
        if na not in pt and na != _norm(p.get("vendor")):
            continue
        matches.append(p)
    if not matches:
        payload = {"found": False}
    else:
        # several editions can exist (repress, coloured vinyl) — prefer one you can buy
        p = next((m for m in matches if m.get("available")), matches[0])
        payload = {"found": True, "available": bool(p.get("available")),
                   "price": p.get("price"), "title": p.get("title"),
                   "url": "https://www.flyingnun.co.nz/products/" + p["handle"]}
    FN_CACHE[key] = (time.time(), payload)
    return payload


def merge_with_latest(incoming):
    """Union the incoming snapshot with the newest stored one so a stale device
    (e.g. a Mac that hasn't opened the app in days) can never shrink the collection.
    The client sends id→date tombstones in `deleted`; those ids stay gone. On an id
    conflict the incoming copy wins (it carries the user's latest edit)."""
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "backup-*.json")))
    if not files:
        return incoming
    try:
        with open(files[-1]) as f:
            existing = json.load(f)
    except Exception:
        return incoming
    dead = {**(existing.get("deleted") or {}), **(incoming.get("deleted") or {})}
    by_id = {r.get("id"): r for r in (existing.get("records") or [])}
    by_id.update({r.get("id"): r for r in (incoming.get("records") or [])})
    merged = dict(incoming)
    merged["records"] = [r for i, r in by_id.items() if i not in dead]
    merged["deleted"] = dead
    if len(merged["records"]) > len(incoming.get("records") or []):
        print(f"merge: incoming {len(incoming.get('records') or [])} + stored "
              f"{len(existing.get('records') or [])} -> {len(merged['records'])}", flush=True)
    return merged


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/index.html"):
            try:
                with open(HTML, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, {"error": "app html missing"})
        elif path == "/config":
            self._send(200, {"anthropic_api_key": API_KEY})
        elif path == "/fn":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            artist = (qs.get("artist") or [""])[0]
            title = (qs.get("title") or [""])[0]
            if not artist or not title:
                self._send(400, {"error": "artist and title required"})
                return
            try:
                self._send(200, fn_lookup(artist, title))
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif path == "/restore":
            files = sorted(glob.glob(os.path.join(BACKUP_DIR, "backup-*.json")))
            if not files:
                self._send(404, {"error": "no backups"})
                return
            with open(files[-1], "rb") as f:
                self._send(200, f.read())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path != "/backup":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            assert isinstance(data.get("records"), list)
        except Exception:
            self._send(400, {"error": "bad snapshot"})
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        data = merge_with_latest(data)
        dest = os.path.join(BACKUP_DIR, f"backup-{date.today().isoformat()}.json")
        with open(dest, "w") as f:
            json.dump(data, f)
        files = sorted(glob.glob(os.path.join(BACKUP_DIR, "backup-*.json")))
        for old in files[:-60]:
            os.remove(old)
        self._send(200, {"ok": True, "records": len(data["records"])})
        push_backup_sensor()

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print(f"records: serving {HTML} on :{PORT}, backups in {BACKUP_DIR}", flush=True)
    push_backup_sensor()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
