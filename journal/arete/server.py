"""Arete - daily protocol + Stoic reflection. Home Assistant app backend.

Standard library only. State is a single JSON file in ARETE_DATA_DIR.
"""

import json
import os
import re
import threading
import time
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from quotes import QUOTES

DATA_DIR = Path(os.environ.get("ARETE_DATA_DIR", Path(__file__).parent / "data"))
DATA_FILE = DATA_DIR / "arete.json"
STATIC_DIR = Path(__file__).parent / "static"
PORT = int(os.environ.get("ARETE_PORT", "8099"))
PUBLISH = os.environ.get("ARETE_PUBLISH_SENSORS", "false").lower() == "true"
STREAK_RULE = os.environ.get("ARETE_STREAK_RULE", "all_committed")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LOCK = threading.RLock()

MORNING_PROMPTS = [
    {"key": "intention", "label": "Who do I choose to be today?",
     "hint": "Name the identity you're stepping into."},
    {"key": "obstacles", "label": "What might go wrong, and how will I respond?",
     "hint": "Premeditatio malorum. Rehearse the hard moments now."},
    {"key": "control", "label": "What is within my control today?",
     "hint": "Put your effort here. Let the rest go."},
]
EVENING_PROMPTS = [
    {"key": "well", "label": "What did I do well today?", "hint": "Give yourself credit."},
    {"key": "short", "label": "Where did I fall short?", "hint": "Be honest, not harsh."},
    {"key": "better", "label": "What will I do better tomorrow?", "hint": "One concrete change."},
    {"key": "grateful", "label": "What am I grateful for?", "hint": "Amor fati. Love what happened."},
]


def _id():
    return uuid.uuid4().hex[:10]


def default_state():
    def area(name, identity, traits, targets):
        return {
            "id": _id(), "name": name, "identity": identity, "traits": traits,
            "targets": [{"id": _id(), "title": t, "committed": True, "archived": False}
                        for t in targets],
        }
    return {
        "version": 1,
        "areas": [
            area("Energy", "Disciplined Athlete", ["Disciplined", "Consistent", "Focused"],
                 ["Digital Sunset", "Exercise", "Follow My Nutrition Protocol"]),
            area("Work", "Philosopher", ["Curious", "Equanimous", "Self-Mastery"],
                 ["Read fifty pages", "1-2-3 Journal"]),
            area("Love", "Peaceful", ["Present", "Joyful", "Connected", "Grateful"],
                 ["Listen with Full Presence", "Spend time in Nature"]),
        ],
        "days": {},
    }


def load():
    if DATA_FILE.exists():
        with DATA_FILE.open() as f:
            return json.load(f)
    state = default_state()
    save(state)
    return state


def save(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=1)
    tmp.replace(DATA_FILE)


STATE = None


def committed_targets(state):
    return [t for a in state["areas"] for t in a["targets"]
            if t["committed"] and not t["archived"]]


def get_day(state, d, create=False):
    day = state["days"].get(d)
    if day is None:
        day = {"done": [], "committed": [t["id"] for t in committed_targets(state)]}
        if create:
            state["days"][d] = day
    return day


def refresh_snapshot(state, d):
    """Today's (or a touched day's) committed set follows the live protocol."""
    day = get_day(state, d, create=True)
    if d >= date.today().isoformat():
        day["committed"] = [t["id"] for t in committed_targets(state)]
    return day


def day_score(state, d):
    day = state["days"].get(d)
    if d == date.today().isoformat() or day is None:
        day = get_day(state, d)
    committed = set(day.get("committed", []))
    done = set(day.get("done", [])) & committed
    total = len(committed)
    return len(done), total


def day_complete(state, d):
    day = state["days"].get(d) or {}
    done, total = day_score(state, d)
    reflected = bool(day.get("evening"))
    if STREAK_RULE == "any_target":
        return bool(set(day.get("done", [])))
    if STREAK_RULE == "reflection":
        return reflected
    return total > 0 and done >= total


def streaks(state, today):
    t = date.fromisoformat(today)
    cur = 0
    d = t if day_complete(state, t.isoformat()) else t - timedelta(days=1)
    while day_complete(state, d.isoformat()):
        cur += 1
        d -= timedelta(days=1)
    best, run = 0, 0
    if state["days"]:
        start = date.fromisoformat(min(state["days"]))
        d = start
        while d <= t:
            run = run + 1 if day_complete(state, d.isoformat()) else 0
            best = max(best, run)
            d += timedelta(days=1)
    return cur, max(best, cur)


def quote_for(d):
    q = QUOTES[date.fromisoformat(d).toordinal() % len(QUOTES)]
    return {"text": q[0], "author": q[1]}


def area_view(state, area, d):
    day = get_day(state, d) if d != date.today().isoformat() else refresh_snapshot(state, d)
    done = set(day.get("done", []))
    committed_ids = set(day.get("committed", []))
    targets = [dict(t, done=t["id"] in done) for t in area["targets"] if not t["archived"]]
    active = [t for t in targets if t["id"] in committed_ids or (t["committed"] and d >= date.today().isoformat())]
    hit = sum(1 for t in active if t["done"])
    return dict(area, targets=targets, hit=hit, total=len(active))


def snapshot(state, d):
    today = date.today().isoformat()
    cur, best = streaks(state, today)
    week = []
    base = date.fromisoformat(today)
    for i in range(6, -1, -1):
        dd = (base - timedelta(days=i)).isoformat()
        week.append({"date": dd, "complete": day_complete(state, dd)})
    day = state["days"].get(d) or {}
    done, total = day_score(state, d)
    return {
        "date": d,
        "today": today,
        "areas": [area_view(state, a, d) for a in state["areas"]],
        "progress": {"done": done, "total": total},
        "morning": day.get("morning"),
        "evening": day.get("evening"),
        "streak": cur,
        "best_streak": best,
        "week": week,
        "quote": quote_for(d),
        "prompts": {"morning": MORNING_PROMPTS, "evening": EVENING_PROMPTS},
        "streak_rule": STREAK_RULE,
    }


def history(state, days=84):
    today = date.fromisoformat(date.today().isoformat())
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        done, total = day_score(state, d)
        day = state["days"].get(d) or {}
        out.append({"date": d, "done": done, "total": total,
                    "complete": day_complete(state, d),
                    "morning": bool(day.get("morning")),
                    "evening": bool(day.get("evening"))})
    journal = []
    for d in sorted(state["days"], reverse=True):
        day = state["days"][d]
        if day.get("morning") or day.get("evening"):
            journal.append({"date": d, "morning": day.get("morning"),
                            "evening": day.get("evening")})
    per_target = {}
    for a in state["areas"]:
        for t in a["targets"]:
            if t["archived"]:
                continue
            last30 = [(today - timedelta(days=i)).isoformat() for i in range(30)]
            hits = sum(1 for d in last30 if t["id"] in (state["days"].get(d) or {}).get("done", []))
            per_target[t["id"]] = {"title": t["title"], "area": a["name"], "hits30": hits}
    return {"days": out, "journal": journal, "targets": list(per_target.values()),
            "prompts": {"morning": MORNING_PROMPTS, "evening": EVENING_PROMPTS}}


# ---------------------------------------------------------------- Home Assistant

def ha_post(entity, state_value, attrs):
    if not (PUBLISH and SUPERVISOR_TOKEN):
        return
    body = json.dumps({"state": state_value, "attributes": attrs}).encode()
    req = urllib.request.Request(
        f"http://supervisor/core/api/states/{entity}", data=body, method="POST",
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                 "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:  # never let HA hiccups break the app
        print(f"[arete] could not publish {entity}: {e}", flush=True)


def publish():
    with LOCK:
        today = date.today().isoformat()
        cur, best = streaks(STATE, today)
        done, total = day_score(STATE, today)
        day = STATE["days"].get(today) or {}
        areas = {a["name"]: f'{v["hit"]}/{v["total"]}'
                 for a in STATE["areas"] for v in [area_view(STATE, a, today)]}
    pct = round(100 * done / total) if total else 0
    ha_post("sensor.arete_streak", cur, {
        "friendly_name": "Arete streak", "unit_of_measurement": "days",
        "icon": "mdi:fire", "best": best, "state_class": "measurement"})
    ha_post("sensor.arete_today", pct, {
        "friendly_name": "Arete today", "unit_of_measurement": "%",
        "icon": "mdi:pillar", "done": done, "total": total, **areas})
    ha_post("sensor.arete_morning_reflection", "done" if day.get("morning") else "pending",
            {"friendly_name": "Arete morning reflection", "icon": "mdi:weather-sunset-up"})
    ha_post("sensor.arete_evening_reflection", "done" if day.get("evening") else "pending",
            {"friendly_name": "Arete evening reflection", "icon": "mdi:weather-night"})


def publish_async():
    threading.Thread(target=publish, daemon=True).start()


def publisher_loop():
    while True:
        publish()
        time.sleep(600)


# ---------------------------------------------------------------- HTTP

def find_area(state, aid):
    return next((a for a in state["areas"] if a["id"] == aid), None)


def find_target(state, tid):
    for a in state["areas"]:
        for t in a["targets"]:
            if t["id"] == tid:
                return a, t
    return None, None


def clean_str(v, n=200):
    return str(v or "").strip()[:n]


class Handler(BaseHTTPRequestHandler):
    server_version = "Arete/1.0"

    def log_message(self, fmt, *args):
        pass

    # helpers
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n) or b"{}")

    def path_parts(self):
        path = self.path.split("?", 1)[0]
        return [p for p in path.split("/") if p]

    def query(self):
        if "?" not in self.path:
            return {}
        return dict(kv.split("=", 1) for kv in self.path.split("?", 1)[1].split("&") if "=" in kv)

    def qdate(self):
        d = self.query().get("date") or date.today().isoformat()
        return d if DATE_RE.match(d) else date.today().isoformat()

    # static
    def serve_static(self, rel):
        rel = rel or "index.html"
        f = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in f.parents or not f.is_file():
            f = STATIC_DIR / "index.html"
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                 ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png",
                 ".json": "application/manifest+json"}.get(f.suffix, "application/octet-stream")
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.path_parts()
        if p[:1] != ["api"]:
            return self.serve_static("/".join(p))
        with LOCK:
            if p == ["api", "state"]:
                return self.send_json(snapshot(STATE, self.qdate()))
            if p == ["api", "history"]:
                return self.send_json(history(STATE))
            if p == ["api", "export"]:
                return self.send_json(STATE)
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        self.mutate("POST")

    def do_PUT(self):
        self.mutate("PUT")

    def do_DELETE(self):
        self.mutate("DELETE")

    def mutate(self, method):
        p = self.path_parts()
        try:
            b = self.body()
        except ValueError:
            return self.send_json({"error": "bad json"}, 400)
        with LOCK:
            res = self.route(method, p, b)
            if res is None:
                return self.send_json({"error": "not found"}, 404)
            save(STATE)
        publish_async()
        self.send_json(snapshot(STATE, res))

    def route(self, method, p, b):
        """Apply a mutation; return the date to render, or None for 404."""
        s = STATE
        today = date.today().isoformat()
        # /api/day/<date>/toggle  {target_id}
        if method == "POST" and len(p) == 4 and p[:2] == ["api", "day"] and p[3] == "toggle":
            d = p[2]
            if not DATE_RE.match(d) or d > today:
                return None
            day = refresh_snapshot(s, d)
            tid = b.get("target_id")
            if tid in day["done"]:
                day["done"].remove(tid)
            else:
                if find_target(s, tid)[1] is None:
                    return None
                day["done"].append(tid)
                if tid not in day["committed"]:
                    day["committed"].append(tid)
            return d
        # /api/day/<date>/reflection/<morning|evening>
        if method == "PUT" and len(p) == 5 and p[:2] == ["api", "day"] and p[3] == "reflection":
            d, kind = p[2], p[4]
            if not DATE_RE.match(d) or kind not in ("morning", "evening"):
                return None
            day = refresh_snapshot(s, d)
            answers = {k: clean_str(v, 4000) for k, v in (b.get("answers") or {}).items()}
            if not any(answers.values()):
                day.pop(kind, None)
            else:
                entry = {"answers": answers, "saved_at": datetime.now().isoformat(timespec="minutes")}
                if kind == "evening" and b.get("rating"):
                    entry["rating"] = max(1, min(5, int(b["rating"])))
                day[kind] = entry
            return d
        # /api/areas/<id>
        if method == "PUT" and len(p) == 3 and p[:2] == ["api", "areas"]:
            a = find_area(s, p[2])
            if not a:
                return None
            if "name" in b:
                a["name"] = clean_str(b["name"], 40) or a["name"]
            if "identity" in b:
                a["identity"] = clean_str(b["identity"], 60) or a["identity"]
            if "traits" in b:
                a["traits"] = [clean_str(t, 30) for t in b["traits"] if clean_str(t)][:8]
            return today
        # /api/areas  (new area)
        if method == "POST" and p == ["api", "areas"]:
            s["areas"].append({"id": _id(), "name": clean_str(b.get("name"), 40) or "New area",
                               "identity": clean_str(b.get("identity"), 60) or "Who will you be?",
                               "traits": [clean_str(t, 30) for t in b.get("traits", []) if clean_str(t)][:8],
                               "targets": []})
            return today
        if method == "DELETE" and len(p) == 3 and p[:2] == ["api", "areas"]:
            a = find_area(s, p[2])
            if not a:
                return None
            s["areas"].remove(a)
            refresh_snapshot(s, today)
            return today
        # /api/areas/<id>/targets
        if method == "POST" and len(p) == 4 and p[:2] == ["api", "areas"] and p[3] == "targets":
            a = find_area(s, p[2])
            title = clean_str(b.get("title"), 80)
            if not a or not title:
                return None
            a["targets"].append({"id": _id(), "title": title,
                                 "committed": bool(b.get("committed", True)), "archived": False})
            refresh_snapshot(s, today)
            return today
        # /api/areas/<id>/order {ids}
        if method == "PUT" and len(p) == 4 and p[:2] == ["api", "areas"] and p[3] == "order":
            a = find_area(s, p[2])
            if not a:
                return None
            order = {tid: i for i, tid in enumerate(b.get("ids", []))}
            a["targets"].sort(key=lambda t: order.get(t["id"], 999))
            return today
        # /api/targets/<id>
        if len(p) == 3 and p[:2] == ["api", "targets"]:
            a, t = find_target(s, p[2])
            if not t:
                return None
            if method == "PUT":
                if "title" in b:
                    t["title"] = clean_str(b["title"], 80) or t["title"]
                if "committed" in b:
                    t["committed"] = bool(b["committed"])
            elif method == "DELETE":
                t["archived"] = True  # keep history intact
            else:
                return None
            refresh_snapshot(s, today)
            return today
        return None


def main():
    global STATE
    STATE = load()
    if PUBLISH and SUPERVISOR_TOKEN:
        threading.Thread(target=publisher_loop, daemon=True).start()
    print(f"[arete] listening on :{PORT}, data in {DATA_FILE}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
