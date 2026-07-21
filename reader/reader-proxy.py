#!/usr/bin/env python3
"""Reader add-on server.

RSS reader backend: fetches subscribed feeds on a schedule, keeps items and
read state in /share/reader/state.json so they sync across devices.
Endpoints (relative — works through HA ingress and direct port):
  GET  /             the app
  GET  /state        {"feeds": [...], "items": [...], "refreshing": bool, "last_refresh": iso}
  POST /refresh      re-fetch all feeds in the background
  POST /feeds/add    {"url": ...} validate, fetch title, subscribe
  POST /feeds/remove {"url": ...} unsubscribe and drop its items
  POST /read         {"ids": [...], "read": true|false}
  POST /read-all     {"feed": url-or-null} mark everything (or one feed) read
"""
import calendar
import json
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import feedparser

PORT = int(os.environ.get("PORT", "8774"))
HTML = os.environ.get("READER_HTML", os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))
DATA_DIR = os.environ.get("READER_DATA_DIR", "/share/reader")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
AGENT = "Mozilla/5.0 (reader)"
socket.setdefaulttimeout(30)  # a dead feed must not hang the whole refresh

# Feeds refresh weekly (Monday 7am NZ, same cadence as the old email digest);
# the in-app refresh button pulls on demand between updates.
REFRESH_DAY = 0              # Monday
REFRESH_HOUR = 7
NZ = ZoneInfo("Pacific/Auckland")
RETENTION_DAYS = 120         # read items older than this are dropped
NEW_FEED_UNREAD_DAYS = 14    # on a feed's first fetch, older items arrive already read

DEFAULT_FEEDS = [
    ("The Convivial Society", "https://theconvivialsociety.substack.com/feed"),
    ("Escaping Flatland", "https://www.henrikkarlsson.xyz/feed"),
    ("The Intrinsic Perspective", "https://www.theintrinsicperspective.com/feed"),
    ("Experimental History", "https://www.experimental-history.com/feed"),
    ("Interconnects", "https://www.interconnects.ai/feed"),
    ("Notes from Henry", "https://henryshukman.substack.com/feed"),
    ("Raptitude", "https://www.raptitude.com/feed/"),
    ("One Useful Thing", "https://www.oneusefulthing.org/feed"),
    ("The Ruffian", "https://ianleslie.substack.com/feed"),
    ("The Marginalian", "https://www.themarginalian.org/feed/"),
    ("The Kākā", "https://thekaka.substack.com/feed"),
    ("The Clearing (Katherine May)", "https://katherinemay.substack.com/feed"),
    ("Mindful News", "https://mindfulsundays.substack.com/feed"),
    ("Conspicuous Cognition", "https://conspicuouscognition.substack.com/feed"),
    ("Towards Democracy", "https://towardsdemocracy.substack.com/feed"),
    ("Greenpeace NZ", "https://greenpeacenz.substack.com/feed"),
    ("Shamubeel Eaqub", "https://shamubeel.substack.com/feed"),
    ("Climate Club NZ", "https://climateclubnz.substack.com/feed"),
    ("David Whyte", "https://davidwhyte.substack.com/feed"),
    ("From Scratch (Josh Summers)", "https://joshuasummers.substack.com/feed"),
    ("Joan Tollifson", "https://joantollifson.substack.com/feed"),
    ("The Pause (On Being)", "https://onbeing.substack.com/feed"),
    ("The Free Press", "https://bariweiss.substack.com/feed"),
    ("The Therapy Room (Vicki Connop)", "https://drvickiconnop.substack.com/feed"),
    ("Sam Harris", "https://samharris.substack.com/feed"),
    ("A Slow Living Path", "https://aslowlivingpath.substack.com/feed"),
    ("One Mindful Breath", "https://onemindfulbreath.substack.com/feed"),
    ("Pulling the Thread (Elise Loehnen)", "https://eliseloehnen.substack.com/feed"),
    ("Import AI", "https://importai.substack.com/feed"),
    ("Psychopolitica", "https://psychopolitica.substack.com/feed"),
    ("Stephan Bodian", "https://stephanbodian.substack.com/feed"),
    ("The Open Heart Project (Susan Piver)", "https://susanpiver.substack.com/feed"),
    ("Letters from an American", "https://heathercoxrichardson.substack.com/feed"),
    ("The Sommpour", "https://thesommpour.substack.com/feed"),
    ("Not The News (Paula Penfold)", "https://paulapenfold.substack.com/feed"),
    ("NonZero", "https://nonzero.substack.com/feed"),
    ("The Pragmatic Engineer", "https://pragmaticengineer.substack.com/feed"),
    ("Small World (David Skilling)", "https://davidskilling.substack.com/feed"),
]

lock = threading.Lock()
state = {"feeds": [], "items": {}, "last_refresh": None}
refreshing = threading.Event()


def load_state():
    global state
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {"feeds": [{"name": n, "url": u} for n, u in DEFAULT_FEEDS],
                 "items": {}, "last_refresh": None}


def save_state():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def entry_id(entry):
    return entry.get("id") or entry.get("link") or entry.get("title", "")


def entry_date(entry):
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return None


def fetch_one(feed, first_fetch):
    """Fetch a feed and merge; returns error string or None."""
    parsed = feedparser.parse(feed["url"], agent=AGENT)
    if not parsed.entries:
        return str(parsed.get("bozo_exception", "no entries"))
    now = datetime.now(timezone.utc)
    unread_floor = now - timedelta(days=NEW_FEED_UNREAD_DAYS)
    with lock:
        for e in parsed.entries:
            eid = entry_id(e)
            if not eid or eid in state["items"]:
                continue
            date = entry_date(e)
            state["items"][eid] = {
                "id": eid,
                "feed": feed["url"],
                "title": e.get("title", "(untitled)"),
                "link": e.get("link", ""),
                "date": date.isoformat() if date else None,
                "read": bool(first_fetch and date and date < unread_floor),
            }
    return None


def refresh_all():
    if refreshing.is_set():
        return
    refreshing.set()
    try:
        with lock:
            feeds = list(state["feeds"])
            known_feeds = {i["feed"] for i in state["items"].values()}
        for feed in feeds:
            err = fetch_one(feed, first_fetch=feed["url"] not in known_feeds)
            with lock:
                for f in state["feeds"]:
                    if f["url"] == feed["url"]:
                        f["error"] = err
        retention_floor = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        with lock:
            urls = {f["url"] for f in state["feeds"]}
            for eid in [i["id"] for i in state["items"].values()
                        if i["feed"] not in urls
                        or (i["read"] and i["date"]
                            and datetime.fromisoformat(i["date"]) < retention_floor)]:
                del state["items"][eid]
            state["last_refresh"] = datetime.now(timezone.utc).isoformat()
            save_state()
        print(f"reader: refreshed {len(feeds)} feeds, {len(state['items'])} items", flush=True)
    finally:
        refreshing.clear()


def next_refresh_time():
    now = datetime.now(NZ)
    target = now.replace(hour=REFRESH_HOUR, minute=0, second=0, microsecond=0)
    days_ahead = (REFRESH_DAY - now.weekday()) % 7
    target += timedelta(days=days_ahead)
    if target <= now:
        target += timedelta(days=7)
    return target


def refresh_loop():
    # A brand-new install fetches once so the app isn't empty until Monday.
    if not state["items"]:
        try:
            refresh_all()
        except Exception as exc:
            print(f"reader: initial refresh failed: {exc}", flush=True)
    while True:
        target = next_refresh_time()
        print(f"reader: next refresh {target.isoformat()}", flush=True)
        time.sleep(max(60, (target - datetime.now(NZ)).total_seconds()))
        try:
            refresh_all()
        except Exception as exc:
            print(f"reader: refresh failed: {exc}", flush=True)


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

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/index.html"):
            try:
                with open(HTML, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, {"error": "app html missing"})
        elif path == "/state":
            with lock:
                self._send(200, {
                    "feeds": state["feeds"],
                    "items": sorted(state["items"].values(),
                                    key=lambda i: i["date"] or "", reverse=True),
                    "refreshing": refreshing.is_set(),
                    "last_refresh": state["last_refresh"],
                })
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        try:
            data = self._body()
        except Exception:
            self._send(400, {"error": "bad json"})
            return

        if path == "/refresh":
            threading.Thread(target=refresh_all, daemon=True).start()
            self._send(200, {"ok": True})

        elif path == "/feeds/add":
            url = (data.get("url") or "").strip()
            if not url.startswith("http"):
                self._send(400, {"error": "not a url"})
                return
            with lock:
                if any(f["url"] == url for f in state["feeds"]):
                    self._send(409, {"error": "already subscribed"})
                    return
            parsed = feedparser.parse(url, agent=AGENT)
            if not parsed.entries:
                self._send(400, {"error": "no entries at that url — is it an RSS feed?"})
                return
            feed = {"name": parsed.feed.get("title", url), "url": url}
            with lock:
                state["feeds"].append(feed)
            fetch_one(feed, first_fetch=True)
            with lock:
                save_state()
            self._send(200, {"ok": True, "name": feed["name"]})

        elif path == "/feeds/remove":
            url = data.get("url")
            with lock:
                state["feeds"] = [f for f in state["feeds"] if f["url"] != url]
                for eid in [i["id"] for i in state["items"].values() if i["feed"] == url]:
                    del state["items"][eid]
                save_state()
            self._send(200, {"ok": True})

        elif path == "/read":
            ids, read = data.get("ids", []), bool(data.get("read", True))
            with lock:
                for eid in ids:
                    if eid in state["items"]:
                        state["items"][eid]["read"] = read
                save_state()
            self._send(200, {"ok": True})

        elif path == "/read-all":
            feed = data.get("feed")
            with lock:
                for item in state["items"].values():
                    if feed is None or item["feed"] == feed:
                        item["read"] = True
                save_state()
            self._send(200, {"ok": True})

        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    load_state()
    threading.Thread(target=refresh_loop, daemon=True).start()
    print(f"reader: serving {HTML} on :{PORT}, state in {STATE_FILE}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
