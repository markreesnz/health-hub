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
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8773"))
HTML = os.environ.get("RECORDS_HTML", os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))
BACKUP_DIR = os.environ.get("RECORDS_BACKUP_DIR", "/share/records/backups")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


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
        dest = os.path.join(BACKUP_DIR, f"backup-{date.today().isoformat()}.json")
        with open(dest, "w") as f:
            json.dump(data, f)
        files = sorted(glob.glob(os.path.join(BACKUP_DIR, "backup-*.json")))
        for old in files[:-60]:
            os.remove(old)
        self._send(200, {"ok": True, "records": len(data["records"])})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print(f"records: serving {HTML} on :{PORT}, backups in {BACKUP_DIR}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
