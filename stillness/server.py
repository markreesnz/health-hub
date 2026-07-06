#!/usr/bin/env python3
"""Serve the single stillness page for any path (robust under HA ingress)."""
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

HTML_PATH = os.environ.get("STILL_HTML", "/app/index.html")
PORT = int(os.environ.get("STILL_PORT", "8770"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
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

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
