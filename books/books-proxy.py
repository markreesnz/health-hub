#!/usr/bin/env python3
"""Bookshelf add-on server.

Serves the single-file app and persists localStorage snapshots to /share so
the reading list survives browser resets and migrates across devices.
Endpoints (all relative — works through HA ingress and direct port):
  GET  /            the app
  GET  /config      {"anthropic_api_key": ...} from the add-on options
  POST /backup      store a full snapshot (one file per day)
  GET  /restore     latest stored snapshot
"""
import glob
import http.cookiejar
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8772"))
HTML = os.environ.get("BOOKS_HTML", os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))
BACKUP_DIR = os.environ.get("BOOKS_BACKUP_DIR", "/share/books/backups")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
WCL_CARD = os.environ.get("WCL_CARD", "")
WCL_PIN = os.environ.get("WCL_PIN", "")
WCL_BASE = "https://catalogue.wcl.govt.nz"


def _wcl_login():
    """Return an authenticated urllib opener, or raise."""
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (Macintosh) books-app")]
    home = op.open(WCL_BASE + "/cgi-bin/spydus.exe/MSGTRN/OPAC/HOME", timeout=30).read().decode("utf-8", "ignore")
    action = re.search(r'<form id="frmLogin" method="post" action="([^"]+)"', home).group(1)
    rdt = re.search(r'name="RDT" value="([^"]+)"', home)
    data = {"BRWLID": WCL_CARD, "BRWLPWD": WCL_PIN}
    if rdt:
        data["RDT"] = rdt.group(1)
    resp = op.open(urllib.request.Request(WCL_BASE + action, data=urllib.parse.urlencode(data).encode()),
                   timeout=30).read().decode("utf-8", "ignore")
    if "BRWENQ" not in resp:
        raise RuntimeError("login failed")
    acct_url = re.search(r'url=([^"]+)"', resp).group(1).replace("&amp;", "&")
    acct = op.open(WCL_BASE + acct_url if acct_url.startswith("/") else acct_url, timeout=30).read().decode("utf-8", "ignore")
    return op, acct


def _wcl_items(op, acct, fmt):
    """Fetch a loans/reserves detail page (by FMT code) and parse titles."""
    m = re.search(r'href="(/cgi-bin/spydus\.exe/ENQ/OPAC/(?:LOANRENQ|RSVCENQ)/[^"]*FMT=' + fmt + r'[^"]*)"', acct)
    if not m:
        return []
    import html as _html
    url = _html.unescape(m.group(1))
    page = op.open(WCL_BASE + url, timeout=30).read().decode("utf-8", "ignore")
    # Title lives in the thumbnail alt text; strip common Spydus subtitle tail
    titles = [_html.unescape(t) for t in re.findall(r'alt="Thumbnail for ([^"]+)"', page)]
    out = []
    for t in titles:
        clean = re.sub(r"\s*[:.]\s+(?:a novel|a memoir|a story.*|a meditation.*)$", "", t, flags=re.I)
        out.append({"title": clean.strip(), "full": t})
    return out


def wcl_library():
    """Return {loans:[...], reserves:[...]} from the borrower's account."""
    if not WCL_CARD or not WCL_PIN:
        return {"error": "Library card not configured"}
    try:
        op, acct = _wcl_login()
        loans = _wcl_items(op, acct, "CL")
        reserves = _wcl_items(op, acct, "WR") + _wcl_items(op, acct, "AR")
        return {"loans": loans, "reserves": reserves}
    except Exception as e:
        return {"error": f"Library error: {e}"}


def wcl_reserve(title, author):
    """Log in to Wellington City Libraries and place a hold on the first
    matching record. Returns (ok, message). Stdlib only."""
    if not WCL_CARD or not WCL_PIN:
        return False, "Library card not configured — add WCL_CARD/WCL_PIN in add-on options"
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (Macintosh) books-app")]

    def get(u):
        return op.open(WCL_BASE + u if u.startswith("/") else u, timeout=30).read().decode("utf-8", "ignore")

    def post(u, d):
        req = urllib.request.Request(WCL_BASE + u, data=urllib.parse.urlencode(d).encode())
        return op.open(req, timeout=30).read().decode("utf-8", "ignore")

    try:
        home = get("/cgi-bin/spydus.exe/MSGTRN/OPAC/HOME")
        action = re.search(r'<form id="frmLogin" method="post" action="([^"]+)"', home).group(1)
        rdt = re.search(r'name="RDT" value="([^"]+)"', home)
        data = {"BRWLID": WCL_CARD, "BRWLPWD": WCL_PIN}
        if rdt:
            data["RDT"] = rdt.group(1)
        resp = post(action, data)
        if "BRWENQ" not in resp:
            return False, "Library login failed — check card number and PIN"

        # strip series suffix / subtitle for cleaner keyword matching
        clean = re.sub(r"\s*\([^)]*\)\s*$", "", title).split(":")[0]
        q = urllib.parse.quote(f"{clean} {author or ''}".strip())
        results = get(f"/cgi-bin/spydus.exe/ENQ/OPAC/BIBENQ?ENTRY={q}&ENTRY_NAME=BS&ENTRY_TYPE=K&NRECS=20")
        links = re.findall(r'href="([^"]+)"[^>]*>\s*Place reservation', results)
        if not links:
            return False, "Not found in the catalogue (or no copies to reserve)"

        page = get(links[0].replace("&amp;", "&"))
        form = re.search(r'<form id="mainForm".*?</form>', page, re.S)
        if not form:
            return False, "Couldn't open the reservation form"
        form = form.group(0)
        faction = re.search(r'action="([^"]+)"', form).group(1)
        fields = {}
        for inp in re.findall(r"<input[^>]*>", form):
            name = re.search(r'name="([^"]+)"', inp)
            typ = (re.search(r'type="([^"]+)"', inp) or [None, "text"])[1]
            if not name or typ == "button":
                continue
            val = re.search(r'value="([^"]*)"', inp)
            fields[name.group(1)] = val.group(1) if val else ""
        fields.setdefault("ITM", "")
        fields["XDAYS"] = "0"  # no expiry

        done = post(faction, fields)
        if "Reservation placed" in done:
            return True, "Reservation placed"
        if re.search(r"already reserved|already have", done, re.I):
            return False, "You've already reserved this"
        if re.search(r"unable|cannot|not available", done, re.I):
            return False, "Library couldn't place the hold (no reservable copies)"
        return False, "Reservation status unclear — check your library account"
    except Exception as e:
        return False, f"Library error: {e}"


_LIB_CACHE = {"at": 0.0, "data": None}


def _library_cached(force=False):
    import time
    # login + two page fetches is ~3s; cache 5 min. force bypasses.
    if not force and _LIB_CACHE["data"] is not None and time.time() - _LIB_CACHE["at"] < 300:
        return _LIB_CACHE["data"]
    data = wcl_library()
    if "error" not in data:
        _LIB_CACHE["data"] = data
        _LIB_CACHE["at"] = time.time()
    return data


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
            self._send(200, {"anthropic_api_key": API_KEY,
                             "library": bool(WCL_CARD and WCL_PIN)})
        elif path == "/library":
            force = "force=1" in self.path
            self._send(200, _library_cached(force))
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
        if path == "/reserve":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                title = (body.get("title") or "").strip()
                assert title
            except Exception:
                self._send(400, {"error": "title required"})
                return
            ok, msg = wcl_reserve(title, body.get("author", ""))
            self._send(200 if ok else 502, {"ok": ok, "message": msg})
            return
        if path != "/backup":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            assert isinstance(data.get("books"), list)
        except Exception:
            self._send(400, {"error": "bad snapshot"})
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dest = os.path.join(BACKUP_DIR, f"backup-{date.today().isoformat()}.json")
        with open(dest, "w") as f:
            json.dump(data, f)
        # keep the newest 60 daily files
        files = sorted(glob.glob(os.path.join(BACKUP_DIR, "backup-*.json")))
        for old in files[:-60]:
            os.remove(old)
        self._send(200, {"ok": True, "books": len(data["books"])})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print(f"bookshelf: serving {HTML} on :{PORT}, backups in {BACKUP_DIR}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
