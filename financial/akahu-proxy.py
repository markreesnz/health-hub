#!/usr/bin/env python3
"""
Run this to open the financial plan dashboard with live Akahu balances.

Usage:  python3 akahu-proxy.py
        (leave the terminal open while using the dashboard)
"""
import urllib.request, json, webbrowser, os, shutil, threading, datetime, time, traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

APP_TOKEN   = os.environ.get("AKAHU_APP_TOKEN", "")
USER_TOKEN  = os.environ.get("AKAHU_USER_TOKEN", "")
PORT        = 8765
HERE        = os.path.dirname(os.path.abspath(__file__))
HTML_FILE   = os.path.join(HERE, "financial-plan-dashboard.html")
DATA_DIR    = os.environ.get("FIN_DATA_DIR", HERE)   # /share/financial on the HA add-on
BACKUP_DIR  = os.path.join(DATA_DIR, "backups")

# Server-side daily snapshots — written even when the dashboard isn't open. The dashboard
# merges any dates it doesn't already have into its own history on load.
AUTO_SNAP_FILE = os.path.join(DATA_DIR, "auto-snapshots.json")

# AI insights — model + key (read from a file so the LaunchAgent doesn't need the shell env).
ANTHROPIC_KEY_FILE = os.path.expanduser("~/.config/anthropic/key")
ANTHROPIC_MODEL = "claude-opus-4-7"
INSIGHTS_CACHE_FILE = os.path.join(DATA_DIR, "ai-insights-cache.json")


def anthropic_key():
    try:
        return open(ANTHROPIC_KEY_FILE).read().strip()
    except Exception:
        return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def _money(n):
    try:
        return "$" + format(round(float(n)), ",")
    except Exception:
        return str(n)


def financial_context(state=None):
    """A compact, current snapshot of the plan for the model to reason over.
    Prefers live `state` from the dashboard; falls back to the latest backup file."""
    s = state if isinstance(state, dict) and state else _latest_backup_state()
    snap = None
    try:
        snap = build_snapshot()
    except Exception:
        pass

    def g(k, d=0):
        return float(s.get(k) or d)

    b1 = g("b1_float") + g("b1_td6") + g("b1_td12")
    b2 = (snap or {}).get("b2", g("b2_balance") + g("b2_cash"))
    b3 = g("b3_balance")
    ks = g("ks_balance")
    nottingham = g("property_nottingham")
    gtk = g("gentrack_shares") * g("gentrack_price")
    net_worth = b1 + b2 + b3 + ks + nottingham + gtk + g("lti_tranche1_net") + g("dvrp_net")

    # Recent spend pace from transactions, if present.
    txns = s.get("transactions") or []
    lines = [
        f"Net worth (excl. primary home): {_money(net_worth)}",
        f"Bucket 1 Cash: {_money(b1)} (target $300,000)",
        f"Bucket 2 Balanced+Cash: {_money(b2)} (target $1,000,000)",
        f"Bucket 3 Growth: {_money(b3)} (target $4,087,332)",
        f"KiwiSaver (locked to 65): {_money(ks)}",
        f"Target spend: $165,000/yr at 3.5% draw",
        f"Retirement: planned after 21 Nottingham St sale (~Dec 2026)",
        f"Recorded transactions on file: {len(txns)}",
    ]
    return "\n".join(lines)


SYSTEM_PROMPT_FINANCE = (
    "You are a sharp, concrete personal-finance coach embedded in Mark's retirement-planning "
    "dashboard. Mark is 51, a NZ tech executive planning to retire after selling an investment "
    "property (~Dec 2026), drawing $165K/yr at 3.5%. He runs a 3-bucket strategy (Cash / Balanced "
    "/ Growth) plus KiwiSaver. Given the current state below, produce exactly THREE insights that "
    "are MANAGEMENT ACTIONS or BEHAVIOUR CHANGES he can take — not observations, not restatements "
    "of the numbers. Each must be specific, actionable, and tied to his actual position. Prefer "
    "behaviour (spending discipline, rebalancing cadence, sequencing pre-bucket assets, review "
    "habits) over generic advice. Be direct and brief."
)

SYSTEM_PROMPT_SPENDING = (
    "You are a sharp, concrete spending coach embedded in Mark's finance dashboard. Mark is 51, a "
    "NZ tech executive heading into retirement with a $165,000/yr spending target (~$6,346 per "
    "fortnight). Focus ONLY on day-to-day spending behaviour — category drift, fortnightly budget "
    "discipline, recurring/subscription creep, large one-offs, and habits that move the annual "
    "run-rate toward or away from the $165K target. Given the spending summary below, produce "
    "exactly THREE insights that are MANAGEMENT ACTIONS or BEHAVIOUR CHANGES on spending — not "
    "observations, not restatements of the numbers, and NOT about investing or rebalancing. Each "
    "must be specific, actionable, and tied to his actual categories/amounts. Be direct and brief."
)


def spending_context(state=None):
    """Spending-focused summary from transactions.
    Prefers live `state` from the dashboard; falls back to the latest backup file."""
    s = state if isinstance(state, dict) and state else _latest_backup_state()
    txns = s.get("transactions") or []
    today = datetime.date.today()

    def parse(d):
        try:
            return datetime.date.fromisoformat(d[:10])
        except Exception:
            return None

    # Mirror the dashboard's exclusions (CATEGORIES with excluded/oneOff in the HTML):
    #   EXCLUDED — not lifestyle spend, never counted toward the $165K target.
    #   ONEOFF   — tracked but excluded from the core run-rate (lumpy, deliberate).
    EXCLUDED_CATS = {"tax", "transfer", "investing", "income", "salary"}
    ONEOFF_CATS = {"renovation", "vehicle", "legal fees"}

    def is_spend(t):
        cat = (t.get("category") or "").lower()
        return cat not in EXCLUDED_CATS and cat not in ONEOFF_CATS and cat != ""

    def window(days, pred):
        cut = today - datetime.timedelta(days=days)
        return [t for t in txns if (parse(t.get("date")) or today) >= cut and pred(t)]

    last90 = window(90, is_spend)

    # Per-category budgets. Categories with a month/quarter/year override are FIXED/LUMPY bills
    # (Body corp, Rates, Insurance, Health, School fees) — use their budgeted annual amount, NOT a
    # 90-day annualisation (a single annual lump in the window would otherwise blow up ~4x).
    caf = s.get("categoryAnnualForecast") or {}
    PPY = {"fortnight": 26, "month": 12, "quarter": 4, "year": 1}

    def override_annual(cat):
        o = caf.get(cat)
        if o is None:
            return None, None
        if isinstance(o, (int, float)):
            return float(o), "year"
        return (float(o.get("amount") or 0)) * PPY.get(o.get("period", "year"), 1), o.get("period", "year")

    # 90-day living spend per category.
    cats90 = {}
    for t in last90:
        c = t.get("category") or "Uncategorised"
        cats90[c] = cats90.get(c, 0) + abs(float(t.get("amount") or 0))

    behavioural = {}   # controllable: annualise the 90-day rate
    fixed = {}         # committed lumpy bills: use the budgeted annual
    for c in set(list(cats90.keys()) + list(caf.keys())):
        ann, period = override_annual(c)
        if period in ("month", "quarter", "year"):
            fixed[c] = ann
        else:
            v90 = cats90.get(c, 0)
            if v90 > 0:
                behavioural[c] = v90 * 365.25 / 90

    behav_total = sum(behavioural.values())
    fixed_total = sum(v for v in fixed.values() if v)
    top_behav = sorted(behavioural.items(), key=lambda kv: kv[1], reverse=True)[:8]
    top_fixed = sorted(((c, v) for c, v in fixed.items() if v), key=lambda kv: kv[1], reverse=True)

    lines = [
        "Target spend: $137,500/yr (~$5,288/fortnight). Education is a separate pre-funded pot.",
        f"CONTROLLABLE (behavioural) spend, annualised from last 90 days: {_money(behav_total)}",
        "  Top controllable categories: " + ", ".join(f"{c} {_money(v)}/yr" for c, v in top_behav),
        f"FIXED/committed annual bills (use these exact annual figures — do NOT annualise from a short "
        f"window, they are lumpy and already budgeted): {_money(fixed_total)}/yr total — "
        + ", ".join(f"{c} {_money(v)}/yr" for c, v in top_fixed),
        "Notes for recommendations:",
        "  - Body corporate is on 1 Kensington (Mark's home); Rates — Nottingham is the investment "
        "property being SOLD ~Dec 2026. Do NOT suggest appealing/revaluing Nottingham's rating value "
        "(it is pre-sale) or 'reconsidering holding Nottingham' (already being sold).",
        "  - Tax/transfers/investing/income and one-offs (renovation/vehicle/legal) are excluded; do "
        "not flag them.",
        "  - Focus recommendations on the CONTROLLABLE categories above, not fixed obligations.",
        f"Transactions on file: {len(txns)}",
    ]
    return "\n".join(lines)


def call_anthropic(system_prompt, context_text, dismissed, feedback=None):
    """Ask the model for 3 insights as JSON.
    dismissed = insight titles to avoid; feedback = free-text steering notes from the user."""
    key = anthropic_key()
    if not key:
        raise RuntimeError("No Anthropic API key available")

    avoid = ""
    if dismissed:
        avoid = ("\n\nThe user marked these earlier insights as NOT relevant — do not repeat them "
                 "or anything similar:\n- " + "\n- ".join(dismissed[-20:]))

    steer = ""
    if feedback:
        steer = ("\n\nThe user has given this feedback on past suggestions — follow it closely "
                 "when choosing and wording the new ones:\n- " + "\n- ".join(feedback[-20:]))

    user_msg = (
        f"Current state:\n{context_text}{avoid}{steer}\n\n"
        "Respond with ONLY a JSON array of exactly 3 objects, each "
        '{"title": "<=6 words", "body": "1-2 sentence concrete action"}. No prose outside the JSON.'
    )

    payload = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1024,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_msg}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    text = "".join(blk.get("text", "") for blk in data.get("content", []) if blk.get("type") == "text").strip()
    # Strip code fences if present, then pull the JSON array.
    if text.startswith("```"):
        text = text.split("```", 2)[1].lstrip("json").strip() if "```" in text[3:] else text.strip("`")
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    insights = json.loads(text)
    # Normalise + id each one.
    out = []
    for i, it in enumerate(insights[:3]):
        out.append({"id": f"i{i}", "title": str(it.get("title", "")).strip(),
                    "body": str(it.get("body", "")).strip()})
    return out

# Akahu account (connection, name) -> plan state key. Mirrors AKAHU_SYNC_MAP in the dashboard.
AKAHU_SNAP_MAP = [
    ("Simplicity", "Growth Fund",      "b3_balance"),
    ("Simplicity", "Balanced Fund",    "b2_balance"),
    ("Simplicity", "Mark's Kiwisaver", "ks_balance"),
    ("BNZ",        "Bucket 1",         "b1_float"),
]


def akahu_accounts():
    """Fetch current Akahu account list."""
    req = urllib.request.Request(
        "https://api.akahu.io/v1/accounts",
        headers={"Authorization": f"Bearer {USER_TOKEN}", "X-Akahu-ID": APP_TOKEN,
                 "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read()).get("items", [])


def _akahu_value(a):
    """Simplicity funds: prefer shares x price (live); else balance.current."""
    port = ((a.get("meta") or {}).get("portfolio") or [None])[0]
    if port and isinstance(port.get("shares"), (int, float)) and isinstance(port.get("price"), (int, float)):
        return port["shares"] * port["price"]
    bal = a.get("balance") or {}
    return bal.get("current") if isinstance(bal.get("current"), (int, float)) else None


def _latest_backup_state():
    """Most recent dashboard state (for manual fields Akahu can't supply: cash, pending, nwExtra)."""
    try:
        files = sorted(f for f in os.listdir(BACKUP_DIR)
                       if f.startswith("financial-plan-") and f.endswith(".json"))
        if not files:
            return {}
        with open(os.path.join(BACKUP_DIR, files[-1])) as f:
            return json.load(f)
    except Exception:
        return {}


def build_snapshot():
    """Compute today's snapshot from live Akahu balances + manual fields from the latest backup."""
    s = _latest_backup_state()
    accounts = akahu_accounts()

    # Start from last-known balances so a missing Akahu account doesn't zero a bucket.
    bal = {k: float(s.get(k) or 0) for k in ("b1_float", "b2_balance", "b3_balance", "ks_balance")}
    for conn, name, key in AKAHU_SNAP_MAP:
        a = next((x for x in accounts
                  if (x.get("connection") or {}).get("name") == conn and x.get("name") == name), None)
        if a is not None:
            v = _akahu_value(a)
            if v is not None:
                bal[key] = v

    cash    = float(s.get("b2_cash") or 0)
    pending = float((s.get("b2_pending") or {}).get("amount") or 0) if isinstance(s.get("b2_pending"), dict) else 0
    td6     = float(s.get("b1_td6") or 0)
    td12    = float(s.get("b1_td12") or 0)
    nw_extra = (float(s.get("property_nottingham") or 0)
                + float(s.get("gentrack_shares") or 0) * float(s.get("gentrack_price") or 0)
                + float(s.get("westpac_td_jun18") or 0) + float(s.get("westpac_td_jun20") or 0)
                + float(s.get("lti_tranche1_net") or 0) + float(s.get("dvrp_net") or 0))

    return {
        "date": datetime.date.today().isoformat(),
        "b1_float": bal["b1_float"], "b1_td6": td6, "b1_td12": td12,
        "b2": bal["b2_balance"] + cash + pending,
        "b3": bal["b3_balance"], "ks": bal["ks_balance"],
        "nwExtra": nw_extra,
        "auto": True,
    }


def write_daily_snapshot():
    """Ensure today's snapshot exists in AUTO_SNAP_FILE; replace any existing entry for today."""
    snap = build_snapshot()
    try:
        existing = json.load(open(AUTO_SNAP_FILE)) if os.path.exists(AUTO_SNAP_FILE) else []
    except Exception:
        existing = []
    existing = [e for e in existing if e.get("date") != snap["date"]]
    existing.append(snap)
    existing.sort(key=lambda e: e.get("date", ""))
    tmp = AUTO_SNAP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(existing, f)
    os.replace(tmp, AUTO_SNAP_FILE)
    print(f"[auto-snapshot] {snap['date']}: NW-investable "
          f"${snap['b1_float']+snap['b1_td6']+snap['b1_td12']+snap['b2']+snap['b3']+snap['ks']:,.0f}")
    return snap


def snapshot_scheduler():
    """Once an hour, make sure today's snapshot has been written (catches up after sleep/restart)."""
    last_date = None
    while True:
        try:
            today = datetime.date.today().isoformat()
            if today != last_date:
                write_daily_snapshot()
                last_date = today
        except Exception:
            print("[auto-snapshot] failed:\n" + traceback.format_exc())
        time.sleep(3600)


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/financial-plan-dashboard.html"):
            override = os.path.join(DATA_DIR, "financial-plan-dashboard.html")  # editable via Samba, no rebuild
            self._serve_file(override if os.path.exists(override) else HTML_FILE, "text/html; charset=utf-8")
        elif path in ("/home", "/home.html"):
            self._serve_file(os.path.expanduser("~/home.html"), "text/html; charset=utf-8")
        elif path == "/journal":
            self._serve_file(os.path.expanduser("~/journal/index.html"), "text/html; charset=utf-8")
        elif path == "/restore":
            self._restore_backup()
        elif path == "/auto-snapshots":
            self._serve_auto_snapshots()
        elif path == "/accounts":
            self._proxy_akahu()
        elif path.startswith("/transactions"):
            self._proxy_transactions()
        elif path.startswith("/share-price"):
            self._proxy_share_price()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/backup":
            self._save_backup()
        elif path == "/refresh":
            self._proxy_refresh()
        elif path == "/ai-insights":
            self._ai_insights()
        else:
            self.send_response(404)
            self.end_headers()

    def _ai_insights(self):
        """Generate 3 behaviour/management insights. Body may include
        {kind:'plan'|'spending', dismissed:[titles], feedback:[notes]}."""
        cache_file = INSIGHTS_CACHE_FILE
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            kind = body.get("kind") or "plan"
            dismissed = body.get("dismissed") or []
            feedback = body.get("feedback") or []
            live = body.get("state")  # current dashboard state, if sent (beats the daily backup)
            if kind == "spending":
                prompt, ctx = SYSTEM_PROMPT_SPENDING, spending_context(live)
            else:
                prompt, ctx = SYSTEM_PROMPT_FINANCE, financial_context(live)
            cache_file = INSIGHTS_CACHE_FILE.replace(".json", f"-{kind}.json")
            insights = call_anthropic(prompt, ctx, dismissed, feedback)
            try:
                with open(cache_file, "w") as f:
                    json.dump({"at": datetime.datetime.now().isoformat(), "insights": insights}, f)
            except Exception:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers()
            self.wfile.write(json.dumps({"insights": insights}).encode())
        except Exception as e:
            # Fall back to the last good set for this kind if the API call fails.
            try:
                cached = json.load(open(cache_file)).get("insights", [])
            except Exception:
                cached = []
            self.send_response(200 if cached else 500)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers()
            self.wfile.write(json.dumps({"insights": cached, "error": str(e)}).encode())

    def _restore_backup(self):
        """Return the latest backup for an app (used to seed localStorage on http migration)."""
        try:
            params = parse_qs(urlparse(self.path).query)
            app = params.get("app", [""])[0]
            if app not in self.BACKUP_APPS:
                raise ValueError(f"unknown app: {app}")
            backup_dir, prefix, _ = self.BACKUP_APPS[app]
            files = sorted(f for f in os.listdir(backup_dir) if f.startswith(prefix + "-") and f.endswith(".json"))
            if not files:
                self.send_response(404)
                self._cors(); self.end_headers()
                self.wfile.write(b'{"error":"no backups"}')
                return
            with open(os.path.join(backup_dir, files[-1]), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _serve_auto_snapshots(self):
        """Server-side daily snapshots for the dashboard to merge into its history."""
        try:
            data = open(AUTO_SNAP_FILE, "rb").read() if os.path.exists(AUTO_SNAP_FILE) else b"[]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self._cors(); self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self._cors(); self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _serve_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"HTML file not found")

    def _proxy_akahu(self):
        try:
            req = urllib.request.Request(
                "https://api.akahu.io/v1/accounts",
                headers={
                    "Authorization": f"Bearer {USER_TOKEN}",
                    "X-Akahu-ID": APP_TOKEN,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            error = json.dumps({"error": str(e)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(error)

    def _proxy_share_price(self):
        """Fetch a share price from Yahoo Finance server-side (browser hits CORS walls)."""
        import re
        try:
            params = parse_qs(urlparse(self.path).query)
            symbol = params.get("symbol", ["GTK.NZ"])[0]
            if not re.fullmatch(r"[A-Za-z0-9.\-]{1,12}", symbol):
                raise ValueError("invalid symbol")
            req = urllib.request.Request(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
            body = json.dumps({"symbol": symbol, "price": price}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            error = json.dumps({"error": str(e)}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(error)

    def _proxy_transactions(self):
        """Fetch transactions from Akahu, following pagination cursors."""
        try:
            params = parse_qs(urlparse(self.path).query)
            start = params.get('start', [None])[0] or \
                    (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
            all_items = []
            url = f"https://api.akahu.io/v1/transactions?start={start}"
            while url:
                req = urllib.request.Request(
                    url,
                    headers={
                        "Authorization": f"Bearer {USER_TOKEN}",
                        "X-Akahu-ID": APP_TOKEN,
                        "Accept": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                all_items.extend(data.get("items", []))
                cursor = (data.get("cursor") or {}).get("next")
                url = f"https://api.akahu.io/v1/transactions?start={start}&cursor={cursor}" if cursor else None
            body = json.dumps({"items": all_items}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            error = json.dumps({"error": str(e)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(error)

    def _proxy_refresh(self):
        """Trigger Akahu to pull fresh data from all connected banks."""
        CONNECTIONS = [
            "conn_cmbu1mnvn000408kzdn1reev8",  # BNZ
            "conn_cmb01ceg1000008l53yw4a6ez",  # Westpac
            "conn_cjgaaeein000001mqusk30tfg",   # Simplicity
        ]
        try:
            results = []
            for conn_id in CONNECTIONS:
                req = urllib.request.Request(
                    f"https://api.akahu.io/v1/refresh/{conn_id}",
                    data=b"{}",
                    headers={
                        "Authorization": f"Bearer {USER_TOKEN}",
                        "X-Akahu-ID": APP_TOKEN,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        results.append(json.loads(resp.read()))
                except Exception as e:
                    results.append({"error": str(e), "connection": conn_id})
            body = json.dumps({"success": True, "results": results}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            error = json.dumps({"error": str(e)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(error)

    # Per-app backup destinations: app key -> (directory, filename prefix, source files to mirror)
    BACKUP_APPS = {
        "finance": (BACKUP_DIR, "financial-plan", [HTML_FILE, os.path.abspath(__file__)]),
    }

    def _save_backup(self):
        try:
            params = parse_qs(urlparse(self.path).query)
            app = params.get("app", ["finance"])[0]
            if app not in self.BACKUP_APPS:
                raise ValueError(f"unknown app: {app}")
            backup_dir, prefix, source_files = self.BACKUP_APPS[app]
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            json.loads(body)  # validate JSON before writing
            os.makedirs(backup_dir, exist_ok=True)
            date_str = datetime.date.today().isoformat()
            path = os.path.join(backup_dir, f"{prefix}-{date_str}.json")
            with open(path, "wb") as f:
                f.write(body)
            # Mirror source files into <backup_dir>/code/ — overwrites latest each time.
            code_dir = os.path.join(backup_dir, "code")
            os.makedirs(code_dir, exist_ok=True)
            for src in source_files:
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(code_dir, os.path.basename(src)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({"saved": path}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Opening dashboard at {url}")
    print("Keep this terminal open while using the dashboard.")
    print("Ctrl+C to stop.\n")
    if not os.environ.get("ADDON"):
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    # Daily balance snapshot — runs in the background so history is recorded even when the
    # dashboard is never opened. Catches up on startup and once an hour thereafter.
    threading.Thread(target=snapshot_scheduler, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
