#!/usr/bin/env python3
"""Wine Watchlist add-on server.

Serves the app, persists data to /share/wine/data.json, checks Real Review
weekly for new vintages of watched wines, and notifies via HA mobile push.

Endpoints:
  GET  /             app HTML
  GET  /api/data     full data JSON
  POST /api/data     save full data JSON
  GET  /api/status   {lastChecked, nextCheck, hasCredentials, checking}
  POST /api/check    run a vintage check now, return {found, checked, error?}
"""

import json
import os
import re
import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

try:
    import requests
    from bs4 import BeautifulSoup
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    print("[wine] WARNING: requests/beautifulsoup4 not installed — vintage checks disabled")

PORT      = int(os.environ.get("PORT", "8775"))
DATA_DIR  = "/share/wine"
DATA_FILE = os.path.join(DATA_DIR, "data.json")
HTML_FILE = os.environ.get("WINE_HTML", os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))
NZ        = ZoneInfo("Pacific/Auckland")
RR_BASE   = "https://www.therealreview.com"

_checking = False
_check_lock = threading.Lock()

# ── Data ──────────────────────────────────────────────────────────────

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"watchlist": None, "cellar": [], "drinkLog": [], "settings": {}}

def save_data(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DATA_FILE)

# ── HA notification ───────────────────────────────────────────────────

def ha_notify(title: str, message: str):
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        print(f"[notify] no SUPERVISOR_TOKEN — skipping: {title!r}")
        return
    try:
        body = json.dumps({"title": title, "message": message}).encode()
        req = urllib.request.Request(
            "http://supervisor/core/api/services/notify/mobile_app_marks_iphone",
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[notify] sent {title!r} → HTTP {r.status}")
    except Exception as e:
        print(f"[notify] error: {e}")

# ── Real Review vintage check ─────────────────────────────────────────

def rr_login(email: str, password: str) -> "requests.Session":
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    s.get(f"{RR_BASE}/login/", timeout=15)
    r = s.post(f"{RR_BASE}/wp-login.php", data={
        "log": email, "pwd": password, "wp-submit": "Log In",
        "redirect_to": "/", "rememberme": "forever", "testcookie": "1",
    }, timeout=15, allow_redirects=True)
    print(f"[rr] login → {r.url} (status {r.status_code})")
    return s

def find_new_vintage(session, producer: str, wine: str, last_vintage) -> int | None:
    """Search RR for producer+wine, return latest vintage if newer than last_vintage."""
    query = urllib.parse.quote(f"{producer} {wine}")
    try:
        r = session.get(f"{RR_BASE}/?s={query}", timeout=15)
        soup = BeautifulSoup(r.text, "lxml")
        this_year = datetime.now(NZ).year
        # Look for years in headings and links that also contain the producer name
        found_years = set()
        for tag in soup.find_all(["h2", "h3", "h4", "a"]):
            text = tag.get_text()
            if producer.lower()[:6] in text.lower():
                for m in re.finditer(r"\b(20[12]\d)\b", text):
                    yr = int(m.group(1))
                    if 2015 <= yr <= this_year:
                        found_years.add(yr)
        if not found_years:
            return None
        latest = max(found_years)
        if last_vintage is None or latest > int(last_vintage):
            return latest
    except Exception as e:
        print(f"[check] error for {producer!r} {wine!r}: {e}")
    return None

def run_check() -> dict:
    global _checking
    with _check_lock:
        if _checking:
            return {"error": "Check already in progress", "found": [], "checked": 0}
        _checking = True

    try:
        data = load_data()
        settings = data.get("settings", {})
        email    = settings.get("rrEmail", "").strip()
        password = settings.get("rrPassword", "").strip()
        watchlist = data.get("watchlist") or []

        to_check = [w for w in watchlist if w.get("status") in ("watching", "tracking")]
        print(f"[check] checking {len(to_check)} wines")

        if not email or not password:
            return {"error": "Enter Real Review email and password in Settings", "found": [], "checked": 0}
        if not HAS_REQUESTS:
            return {"error": "requests library not available in this build", "found": [], "checked": 0}

        try:
            session = rr_login(email, password)
        except Exception as e:
            return {"error": f"Real Review login failed: {e}", "found": [], "checked": 0}

        new_finds = []
        for w in to_check:
            new_v = find_new_vintage(session, w["producer"], w["wine"], w.get("latestVintage"))
            if new_v:
                new_finds.append({
                    "id":         w["id"],
                    "producer":   w["producer"],
                    "wine":       w["wine"],
                    "newVintage": new_v,
                    "oldVintage": w.get("latestVintage"),
                    "wineryUrl":  w.get("wineryUrl", ""),
                    "ml":         w.get("ml", False),
                })
                w["latestVintage"] = new_v
                print(f"[check] NEW → {w['producer']} {w['wine']} {new_v}")
            time.sleep(0.5)  # be polite

        now_str = datetime.now(NZ).isoformat()
        data["lastChecked"] = now_str
        save_data(data)

        if new_finds:
            lines = []
            for f in new_finds[:4]:
                how = " (mailing list)" if f["ml"] else ""
                lines.append(f"{f['producer']} {f['wine']} {f['newVintage']}{how}")
            ha_notify("🍷 New NZ Wine Vintage", "\n".join(lines))

        return {"found": new_finds, "checked": len(to_check), "lastChecked": now_str}

    finally:
        _checking = False

# ── Weekly scheduler: Monday 9am NZ ──────────────────────────────────

def _next_monday_9am() -> datetime:
    now = datetime.now(NZ)
    days = (7 - now.weekday()) % 7 or 7
    candidate = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=days)
    return candidate

def scheduler():
    while True:
        nxt = _next_monday_9am()
        wait = (nxt - datetime.now(NZ)).total_seconds()
        print(f"[schedule] next check in {wait/3600:.1f}h ({nxt.strftime('%a %d %b %H:%M NZ')})")
        time.sleep(max(wait, 60))
        run_check()

# ── HTTP handler ──────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress per-request logging

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        try:
            with open(HTML_FILE, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/index.html"):
            self._send_html()
        elif path == "/api/data":
            self._send_json(load_data())
        elif path == "/api/status":
            data = load_data()
            nxt  = _next_monday_9am()
            self._send_json({
                "lastChecked":    data.get("lastChecked"),
                "nextCheck":      nxt.isoformat(),
                "hasCredentials": bool(data.get("settings", {}).get("rrEmail")),
                "checking":       _checking,
            })
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path   = self.path.split("?")[0].rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else b"{}"

        if path == "/api/data":
            try:
                save_data(json.loads(body))
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)

        elif path == "/api/check":
            result = run_check()
            self._send_json(result)

        else:
            self.send_response(404)
            self.end_headers()

# ── Boot ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    threading.Thread(target=scheduler, daemon=True, name="scheduler").start()
    print(f"[wine] listening on port {PORT}")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
