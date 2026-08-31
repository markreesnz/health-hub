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
            cur.update(incoming)     # merge, so a stale client cannot wipe the rest
            save(cur)
        return self._send(200, json.dumps({"ok": True, "entries": len(cur)}))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"WSET study app on :{PORT} · state {STORE}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
