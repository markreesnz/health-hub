#!/usr/bin/env python3
"""Serve the journal page for any path (robust under HA ingress), and mount the
Arete daily protocol + Stoic reflection app under /protocol/."""
import os
import sys
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "arete"))
import server as arete  # noqa: E402  (arete/server.py)

HTML_PATH = os.environ.get("JOURNAL_HTML", "/app/index.html")
PORT = int(os.environ.get("JOURNAL_PORT", "8770"))
PREFIX = "/protocol"


class Handler(arete.Handler):
    def _arete(self):
        """True if this request is for Arete; strips the prefix from self.path."""
        p = self.path
        if p == PREFIX or p.startswith(PREFIX + "/") or p.startswith(PREFIX + "?"):
            self.path = p[len(PREFIX):] or "/"
            return True
        return False

    def do_GET(self):
        if self.path == PREFIX:  # need the trailing slash so relative URLs resolve
            self.send_response(302)
            self.send_header("Location", "protocol/")
            self.end_headers()
            return
        if self._arete():
            return super().do_GET()
        try:
            with open(HTML_PATH, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(500, "page missing")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def mutate(self, method):
        if not self._arete():
            return self.send_error(404)
        return super().mutate(method)


if __name__ == "__main__":
    arete.STATE = arete.load()
    if arete.PUBLISH and arete.SUPERVISOR_TOKEN:
        threading.Thread(target=arete.publisher_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
