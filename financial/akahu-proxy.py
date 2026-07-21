#!/usr/bin/env python3
"""
Run this to open the financial plan dashboard with live Akahu balances.

Usage:  python3 akahu-proxy.py
        (leave the terminal open while using the dashboard)
"""
import urllib.request, urllib.error, json, webbrowser, os, shutil, threading, datetime, time, traceback
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


import re as _re


def _payee_key(p):
    """Exact mirror of the dashboard's payee keying: payeeKey(p) — lowercase, strip
    everything outside [a-z0-9], first 40 chars — falling back to the raw lowercased
    payee when that leaves nothing (matches the categorise-by-payee views)."""
    k = _re.sub(r"[^a-z0-9]", "", (p or "").lower())[:40]
    return k or (p or "(none)").lower()


def recover_lost_rules():
    """Self-heal after the 2026-07 cross-device clobber: union the payeeOverrides maps from
    every daily backup on /share (newest file wins per key) back into the latest state,
    re-apply the full rule map to transactions left sitting in 'Other', and publish the
    healed state with a fresh savedAt so every device pulls it. Idempotent — the union only
    adds keys the current state lacks, and 'Other' means "never categorised". Returns stats
    for the diagnostics sensor. Marker-guarded per code revision."""
    marker = os.path.join(BACKUP_DIR, ".rules-recovered-2026-07-20b")
    if os.path.exists(marker):
        return {"skipped": "already ran"}
    stats = {}
    try:
        files = sorted(f for f in os.listdir(BACKUP_DIR)
                       if f.startswith("financial-plan-") and f.endswith(".json"))
        if not files:
            return {"skipped": "no backups"}
        union = {}
        for name in files:      # oldest -> newest, so the newest file wins per key
            try:
                with open(os.path.join(BACKUP_DIR, name)) as f:
                    rules = json.load(f).get("payeeOverrides") or {}
                if isinstance(rules, dict):
                    union.update({k: v for k, v in rules.items() if v})
            except Exception:
                continue
        with open(os.path.join(BACKUP_DIR, files[-1])) as f:
            state = json.load(f)
        current = state.get("payeeOverrides") or {}
        # Current state wins for every key it still has (incl. null delete-tombstones);
        # the union only fills in what the clobber destroyed.
        missing = {k: v for k, v in union.items() if k not in current}
        current.update(missing)
        state["payeeOverrides"] = current
        reapplied = 0
        for t in state.get("transactions") or []:
            if t.get("category") == "Other":
                cat = current.get(_payee_key(t.get("payee")))
                if cat and cat != "Other":
                    t["category"] = cat
                    reapplied += 1
        stats = {"restored_rules": len(missing), "reapplied_tx": reapplied,
                 "union_rules": len(union), "source_files": len(files)}
        if missing or reapplied:
            state["savedAt"] = int(time.time() * 1000)
            path = os.path.join(BACKUP_DIR, f"financial-plan-{datetime.date.today().isoformat()}.json")
            if os.path.exists(path):
                shutil.copy2(path, path + ".pre-recovery")
            with open(path, "w") as f:
                json.dump(state, f)
        print(f"rules recovery: {stats}")
        open(marker, "w").close()
        return stats
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}


def _fuzzy_tx_key(t):
    """Mirror of the dashboard's import dedupeKey: date|amount|payee|source."""
    try:
        amt = f"{float(t.get('amount') or 0):.2f}"
    except Exception:
        amt = str(t.get("amount"))
    return f"{t.get('date')}|{amt}|{(t.get('payee') or '').lower()}|{t.get('source') or ''}"


def dedupe_cross_type(state):
    """Collapse duplicate transactions where the same underlying purchase exists under two
    id schemes — a stable akahu_<id> row and a random-id row (CSV-era tx_..., or a device
    re-import) sharing the import dedupe key. The 2026-07-19 union-by-id sync merge grafted
    ~250 of these. Keeps the akahu_ row (future syncs match it), adopts the dropped row's
    category when the kept one is uncategorised, and preserves the excluded flag. Same-type
    matches are left alone: two genuine identical same-day purchases are legitimate. Pairs
    rows one-for-one so a group with more random-id rows than akahu rows keeps the excess.
    Returns the number of rows dropped (state is modified in place)."""
    txs = state.get("transactions") or []
    groups = {}
    for t in txs:
        groups.setdefault(_fuzzy_tx_key(t), []).append(t)
    drop = set()
    for rows in groups.values():
        ak = [t for t in rows if str(t.get("id") or "").startswith("akahu_")]
        other = [t for t in rows if not str(t.get("id") or "").startswith("akahu_")]
        for keep, dup in zip(ak, other):
            if keep.get("category") in (None, "", "Other") and dup.get("category") not in (None, "", "Other"):
                keep["category"] = dup["category"]
            if dup.get("excluded"):
                keep["excluded"] = True
            drop.add(id(dup))
    if drop:
        state["transactions"] = [t for t in txs if id(t) not in drop]
    return len(drop)


def dedupe_latest_backup():
    """One-shot repair of the latest daily state file, marker-guarded. Bumps savedAt so
    every device pulls the deduplicated state."""
    marker = os.path.join(BACKUP_DIR, ".tx-deduped-2026-07-20")
    if os.path.exists(marker):
        return {"skipped": "already ran"}
    try:
        files = sorted(f for f in os.listdir(BACKUP_DIR)
                       if f.startswith("financial-plan-") and f.endswith(".json"))
        if not files:
            return {"skipped": "no backups"}
        path = os.path.join(BACKUP_DIR, files[-1])
        with open(path) as f:
            state = json.load(f)
        dropped = dedupe_cross_type(state)
        if dropped:
            state["savedAt"] = int(time.time() * 1000)
            shutil.copy2(path, path + ".pre-dedupe")
            with open(path, "w") as f:
                json.dump(state, f)
        print(f"tx dedupe: dropped {dropped} duplicate rows")
        open(marker, "w").close()
        return {"dropped": dropped}
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}


BAN_FILE = os.path.join(DATA_DIR, "banned-tx-ids.json")


def _load_banned():
    try:
        with open(BAN_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def rebuild_from_known_good():
    """One-shot: reconstruct the shared state from the last pre-merge daily file.

    The 2026-07-19 file is known-good and complete (1314 tx, 0 uncategorised): the phone
    lost its local state that day and re-imported 566 rows of history from Akahu, and the
    sync merge grafted ~255 of them as duplicates. Most are invisible to the fuzzy key —
    Akahu's merchant names differ from the CSV-era payee text, and re-fetched history can
    even carry NEW akahu ids for the same purchase — so instead of heuristics: take the
    19 Jul transaction list wholesale, keep everything else (rules, snapshots, scalars,
    balances, rollover state) from the current file, add back only current rows genuinely
    dated on/after 2026-07-17 that aren't in the base by id or fuzzy key, and BAN the ids
    of everything else so no device holding the dirty state can push them back in."""
    marker = os.path.join(BACKUP_DIR, ".state-rebuilt-2026-07-20")
    if os.path.exists(marker):
        return {"skipped": "already ran"}
    try:
        base_path = os.path.join(BACKUP_DIR, "financial-plan-2026-07-19.json")
        if not os.path.exists(base_path):
            return {"skipped": "no base file"}
        files = sorted(f for f in os.listdir(BACKUP_DIR)
                       if f.startswith("financial-plan-") and f.endswith(".json"))
        cur_path = os.path.join(BACKUP_DIR, files[-1])
        with open(base_path) as f:
            base = json.load(f)
        with open(cur_path) as f:
            cur = json.load(f)
        # Candidate rows: the pre-dedupe snapshot if it exists (superset that still holds
        # the 61 rows already dropped — their ids must be banned too, devices still have them).
        cand = cur
        if os.path.exists(cur_path + ".pre-dedupe"):
            with open(cur_path + ".pre-dedupe") as f:
                cand = json.load(f)
        state = dict(cur)   # current wins for rules/snapshots/scalars/balances/rollover
        base_tx = base.get("transactions") or []
        ids = {t.get("id") for t in base_tx}
        fuzzy = {_fuzzy_tx_key(t) for t in base_tx}
        added, banned = [], []
        for t in cand.get("transactions") or []:
            if t.get("id") in ids:
                continue
            if (t.get("date") or "") >= "2026-07-17" and _fuzzy_tx_key(t) not in fuzzy:
                added.append(t)
            else:
                banned.append(t.get("id"))
        state["transactions"] = base_tx + added
        rules = state.get("payeeOverrides") or {}
        reapplied = 0
        for t in state["transactions"]:
            if t.get("category") == "Other":
                cat = rules.get(_payee_key(t.get("payee")))
                if cat and cat != "Other":
                    t["category"] = cat
                    reapplied += 1
        state["savedAt"] = int(time.time() * 1000)
        all_banned = _load_banned() | {b for b in banned if b}
        with open(BAN_FILE, "w") as f:
            json.dump(sorted(all_banned), f)
        shutil.copy2(cur_path, cur_path + ".pre-rebuild")
        with open(cur_path, "w") as f:
            json.dump(state, f)
        stats = {"kept": len(base_tx), "added": len(added), "banned": len(banned),
                 "reapplied": reapplied}
        print(f"state rebuild: {stats}")
        open(marker, "w").close()
        return stats
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}


PATCH_LOG = os.path.join(DATA_DIR, "patches.json")


def _load_patch_log():
    try:
        with open(PATCH_LOG) as f:
            log = json.load(f)
        return log if isinstance(log, list) else []
    except Exception:
        return []


def apply_remote_patch():
    """Apply scalar field updates posted to sensor.financial_state_patch in HA.

    Remote-management channel: the Green's ports are LAN-only, and hardcoding values (e.g.
    account balances) in this public repo is not on — so scalar patches travel via the HA
    core API instead. Post a sensor with attributes {"patch": {field: value}, "patch_id":
    "unique-id"}; on startup this merges the patch into the latest state file (scalars only
    — collections are protected), bumps savedAt so devices pull, and remembers the id so
    each patch applies once."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return {"skipped": "no token"}
    try:
        req = urllib.request.Request(
            "http://supervisor/core/api/states/sensor.financial_state_patch",
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            attrs = json.load(r).get("attributes") or {}
        patch, pid = attrs.get("patch"), attrs.get("patch_id")
        stale = attrs.get("stale") if isinstance(attrs.get("stale"), dict) else {}
        if not isinstance(patch, dict) or not pid:
            return {"skipped": "no patch"}
        id_file = os.path.join(BACKUP_DIR, ".last-patch-id")
        try:
            if open(id_file).read().strip() == str(pid):
                return {"skipped": f"patch {pid} already applied"}
        except Exception:
            pass
        files = sorted(f for f in os.listdir(BACKUP_DIR)
                       if f.startswith("financial-plan-") and f.endswith(".json"))
        if not files:
            return {"skipped": "no backups"}
        path = os.path.join(BACKUP_DIR, files[-1])
        with open(path) as f:
            state = json.load(f)
        PROTECTED = {"transactions", "snapshots", "payeeOverrides", "savedAt", "appliedPatchIds"}
        applied = {}
        for k, v in patch.items():
            # Any JSON value except the sync-managed collections — small objects like
            # openLog (the stillness gate counter) are legitimate patch targets.
            if k in PROTECTED:
                continue
            state[k] = v
            applied[k] = v
        if applied:
            acks = state.get("appliedPatchIds")
            if not isinstance(acks, list):
                acks = state["appliedPatchIds"] = []
            if pid not in acks:
                acks.append(pid)
            state["savedAt"] = int(time.time() * 1000)
            with open(path, "w") as f:
                json.dump(state, f)
            # Ledger: patches must also reach the DEVICES' local copies — an active device's
            # savedAt outruns the server's, so it never adopts the server blob. /restore
            # embeds this ledger and each client applies unseen ids to its own state;
            # /backup force-applies un-acked patches to incoming pushes.
            log = _load_patch_log()
            if not any(p.get("id") == pid for p in log):
                log.append({"id": str(pid), "fields": applied,
                            "stale": {k: v for k, v in stale.items() if k in applied}})
                with open(PATCH_LOG, "w") as f:
                    json.dump(log, f)
        with open(id_file, "w") as f:
            f.write(str(pid))
        print(f"remote patch {pid}: applied {applied}")
        return {"patch_id": pid, "applied": sorted(applied)}
    except urllib.error.HTTPError as e:
        return {"skipped": f"HTTP {e.code}"}
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}


# Mirrors of the dashboard's category constants for the run-rate diagnostic.
RUNRATE_EXCLUDED = {"Tax", "Transfer", "Investing", "Income", "Reimbursable"}
RUNRATE_ONEOFF = {"Legal fees", "Renovation", "Vehicle"}
RUNRATE_PERIODS = {"fortnight": 26, "month": 12, "quarter": 4, "year": 1}


def _runrate_summary(s):
    """Mirror of renderForecast's 12-month run-rate: per-category since-baseline spend
    annualised (or the manual override), so the headline figure can be verified remotely."""
    today = datetime.date.today()
    baseline = s.get("baselineDate") or f"{today.year}-01-01"
    try:
        days = max(1, (today - datetime.date.fromisoformat(baseline)).days + 1)
    except Exception:
        baseline, days = f"{today.year}-01-01", max(1, today.timetuple().tm_yday)
    today_iso = today.isoformat()
    cat_ytd = {}
    salary_latest = ""
    for t in s.get("transactions") or []:
        amt = t.get("amount") or 0
        blob = f"{t.get('description') or ''} {t.get('payee') or ''}".lower()
        if amt > 0 and not t.get("excluded") and "bnz salar" in blob:
            salary_latest = max(salary_latest, t.get("date") or "")
        cat = t.get("category")
        if cat in RUNRATE_EXCLUDED or cat in RUNRATE_ONEOFF or t.get("excluded"):
            continue
        if amt >= 0 or not (baseline <= (t.get("date") or "") <= today_iso):
            continue
        cat_ytd[cat] = cat_ytd.get(cat, 0) - amt
    overrides = s.get("categoryAnnualForecast") or {}
    def forecast(cat):
        ytd = cat_ytd.get(cat, 0)
        o = overrides.get(cat)
        if o is not None:
            if isinstance(o, (int, float)):
                amt, period = float(o), "year"
            else:
                amt, period = float(o.get("amount") or 0), o.get("period") or "year"
            if amt >= 0:
                return max(ytd, amt * RUNRATE_PERIODS.get(period, 1)), True
        return ytd * 365.0 / days, False
    cats = set(cat_ytd) | {k for k in overrides
                           if k not in RUNRATE_EXCLUDED and k not in RUNRATE_ONEOFF}
    rows, total_ytd, total_fc = [], 0.0, 0.0
    for c in cats:
        f, manual = forecast(c)
        total_fc += f
        total_ytd += cat_ytd.get(c, 0)
        rows.append({"cat": c, "ytd": round(cat_ytd.get(c, 0)), "fc": round(f), "manual": manual})
    rows.sort(key=lambda r: -r["fc"])
    return {"baseline": baseline, "days": days, "ytd": round(total_ytd),
            "forecast": round(total_fc), "fortnightStart": s.get("fortnightStart"),
            "lastRolloverSalaryDate": s.get("lastRolloverSalaryDate"),
            "latestSalary": salary_latest,
            "tds": {"b1_td6": s.get("b1_td6"), "b1_td12": s.get("b1_td12")},
            "patchAcks": s.get("appliedPatchIds"), "openLog": s.get("openLog"),
            "top": rows[:10]}


def push_diagnostics(extra=None):
    """Publish sensor.financial_plan_sync into HA — per-day rule/transaction counts from the
    daily state files plus the last recovery result. This is the remote debugging channel:
    the Green's filesystem and add-on ports are unreachable from the Mac, but HA's core API
    (Nabu Casa) can read this sensor. Needs homeassistant_api: true in config.yaml."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return
    try:
        rows = []
        files = sorted(f for f in os.listdir(BACKUP_DIR)
                       if f.startswith("financial-plan-") and f.endswith(".json"))
        for name in files[-14:]:
            try:
                with open(os.path.join(BACKUP_DIR, name)) as f:
                    s = json.load(f)
                txs = s.get("transactions") or []
                rows.append({
                    "date": name[len("financial-plan-"):-len(".json")],
                    "rules": len([v for v in (s.get("payeeOverrides") or {}).values() if v]),
                    "tx": len(txs),
                    "other": len([t for t in txs
                                  if t.get("category") == "Other" and (t.get("amount") or 0) < 0]),
                    "savedAt": s.get("savedAt"),
                })
            except Exception as e:
                rows.append({"date": name, "error": str(e)})
        # Residual duplicate analysis on the latest file: group by the import fuzzy key and
        # classify multi-row groups by id-type mix, with samples — enough to tell churned
        # akahu ids, double CSV imports and genuine same-day purchases apart from the Mac.
        dup = {"ak_ak": 0, "mixed": 0, "rand_rand": 0}
        samples, imports, runrate = [], {}, {}
        try:
            if files:
                with open(os.path.join(BACKUP_DIR, files[-1])) as f:
                    s = json.load(f)
                runrate = _runrate_summary(s)
                groups = {}
                for t in s.get("transactions") or []:
                    d = t.get("importedAt") or "?"
                    imports[d] = imports.get(d, 0) + 1
                    groups.setdefault(_fuzzy_tx_key(t), []).append(t)
                for k, g in groups.items():
                    if len(g) < 2:
                        continue
                    ak = sum(1 for t in g if str(t.get("id") or "").startswith("akahu_"))
                    kind = "ak_ak" if ak == len(g) else ("rand_rand" if ak == 0 else "mixed")
                    dup[kind] += 1
                    if len(samples) < 12:
                        samples.append({"key": k, "kind": kind,
                                        "ids": [str(t.get("id"))[:18] for t in g],
                                        "cats": [t.get("category") for t in g],
                                        "imported": [t.get("importedAt") for t in g]})
        except Exception as e:
            samples = [{"error": str(e)}]
        recent_imports = dict(sorted(imports.items())[-10:])
        payload = {"state": str(rows[-1]["rules"]) if rows else "0",
                   "attributes": {"friendly_name": "Financial plan sync",
                                  "files": rows, "dup_groups": dup, "dup_samples": samples,
                                  "imports_by_day": recent_imports, "runrate": runrate,
                                  **(extra or {})}}
        req = urllib.request.Request(
            "http://supervisor/core/api/states/sensor.financial_plan_sync",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST")
        urllib.request.urlopen(req, timeout=10).read()
        print("diagnostics sensor pushed")
    except Exception:
        traceback.print_exc()


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
    # Pending decays as the deployed funds rise above the baseline — the rise is money that has
    # landed, so only the remainder is still in transit (mirrors b2PendingRemaining in the dashboard).
    pending = 0.0
    p = s.get("b2_pending")
    if isinstance(p, dict):
        arrived = max(0.0, bal["b2_balance"] + cash - float(p.get("baseline") or 0))
        pending = max(0.0, float(p.get("amount") or 0) - arrived)
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
            # Always serve the baked-in file shipped with the add-on. A /share override used to be
            # supported ("edit via Samba, no rebuild") but a stale override silently shadows GitHub
            # updates — opt in explicitly with FIN_HTML_OVERRIDE=1 if you really want that workflow.
            html = HTML_FILE
            if os.environ.get("FIN_HTML_OVERRIDE") == "1":
                override = os.path.join(DATA_DIR, "financial-plan-dashboard.html")
                if os.path.exists(override):
                    html = override
            self._serve_file(html, "text/html; charset=utf-8")
        elif path in ("/home", "/home.html"):
            self._serve_file(os.path.expanduser("~/home.html"), "text/html; charset=utf-8")
        elif path == "/journal":
            self._serve_file(os.path.expanduser("~/journal/index.html"), "text/html; charset=utf-8")
        elif path == "/term-deposits":
            # TD balances live only in the dashboard's manual state (Akahu reports TDs as $0).
            # Serves {key: value} for the TD fields — used by the weekly digest's money table.
            s = _latest_backup_state()
            tds = {k: v for k, v in s.items()
                   if isinstance(v, (int, float)) and v > 0
                   and ("_td" in k.lower() or k.lower().startswith("td_"))}
            body = json.dumps(tds).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        elif path == "/restore":
            self._restore_backup()
        elif path == "/backups":
            self._list_backups()
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

    def _list_backups(self):
        """Backup inventory — file, size, savedAt, rule/transaction counts. Debugging aid
        for the cross-device sync (which daily state file holds what)."""
        out = []
        try:
            for name in sorted(os.listdir(BACKUP_DIR)):
                if not (name.startswith("financial-plan-") and name.endswith(".json")):
                    continue
                p = os.path.join(BACKUP_DIR, name)
                row = {"file": name, "bytes": os.path.getsize(p)}
                try:
                    with open(p) as f:
                        s = json.load(f)
                    row["savedAt"] = s.get("savedAt")
                    row["rules"] = len([v for v in (s.get("payeeOverrides") or {}).values() if v])
                    row["transactions"] = len(s.get("transactions") or [])
                except Exception as e:
                    row["error"] = str(e)
                out.append(row)
        except Exception as e:
            out = [{"error": str(e)}]
        body = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

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
            # Tell finance clients which transaction ids were banned by the state rebuild —
            # devices whose local copy is "newer" never adopt the server state, so this is
            # the only way they drop the grafted duplicates they still hold. The client
            # strips matching rows and removes the field before persisting.
            if app == "finance":
                banned = _load_banned()
                patches = _load_patch_log()
                if banned or patches:
                    try:
                        state = json.loads(body)
                        if banned:
                            state["_bannedTxIds"] = sorted(banned)
                        if patches:
                            state["_patches"] = patches
                        body = json.dumps(state).encode()
                    except Exception:
                        pass
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
            incoming = json.loads(body)  # validate JSON before writing
            # A device still holding the dirty pre-rebuild state (or running old sync code)
            # can push the grafted duplicates back in — strip banned ids and cross-id-type
            # duplicates on every write so the shared state stays clean regardless of what
            # clients hold. savedAt is left untouched.
            if app == "finance" and isinstance(incoming, dict):
                dirty = dedupe_cross_type(incoming)
                banned = _load_banned()
                if banned:
                    txs = incoming.get("transactions") or []
                    kept = [t for t in txs if t.get("id") not in banned]
                    if len(kept) != len(txs):
                        incoming["transactions"] = kept
                        dirty = True
                # Regression guard: a device that never adopted the healed state (its local
                # savedAt outruns the server's, so it never pulls) would otherwise overwrite
                # the shared rules/snapshots with an older copy on every save. Union in
                # whatever only the server has; the device wins for keys it carries —
                # including null delete-tombstones.
                prev = _latest_backup_state()
                rules = incoming.get("payeeOverrides")
                if not isinstance(rules, dict):
                    rules = incoming["payeeOverrides"] = {}
                for k, v in (prev.get("payeeOverrides") or {}).items():
                    if v and k not in rules:
                        rules[k] = v
                        dirty = True
                snaps = incoming.get("snapshots")
                if not isinstance(snaps, list):
                    snaps = incoming["snapshots"] = []
                have = {s.get("date") for s in snaps if isinstance(s, dict)}
                for snap in (prev.get("snapshots") or []):
                    if isinstance(snap, dict) and snap.get("date") not in have:
                        snaps.append(snap)
                        dirty = True
                # Forward-only scalars (see client mergeMissing): fortnight anchor and
                # rollover ack only ever advance; the tracking baseline is set once. Stop a
                # device holding old values from regressing the shared copies — that's what
                # kept re-showing the "Start new fortnight" prompt.
                for k in ("fortnightStart", "lastRolloverSalaryDate"):
                    if (prev.get(k) or "") > (incoming.get(k) or ""):
                        incoming[k] = prev[k]
                        dirty = True
                if (prev.get("fnSeedVersion") or 0) > (incoming.get("fnSeedVersion") or 0):
                    incoming["fnSeedVersion"] = prev["fnSeedVersion"]
                    dirty = True
                if not incoming.get("baselineDate") and prev.get("baselineDate"):
                    incoming["baselineDate"] = prev["baselineDate"]
                    dirty = True
                # Force-apply any remote patch this device hasn't acknowledged — an active
                # device whose savedAt outruns the server never pulls, so without this its
                # pushes would revert patched fields (e.g. TD balances) on every save.
                acks = incoming.get("appliedPatchIds")
                if not isinstance(acks, list):
                    acks = incoming["appliedPatchIds"] = []
                for p in _load_patch_log():
                    pid = p.get("id")
                    if not pid:
                        continue
                    acked = pid in acks
                    stale = p.get("stale") or {}
                    for k, v in (p.get("fields") or {}).items():
                        cur = incoming.get(k)
                        if cur == v:
                            continue
                        # Apply when un-acked, but ALSO when the field still holds its
                        # known-stale value — v1.10 clients imported acks without values
                        # ("poisoned ack"), so an ack alone must not shield stale data.
                        # Any other value is a genuine manual edit and is left alone.
                        if not acked or cur is None or (k in stale and cur == stale[k]):
                            incoming[k] = v
                            dirty = True
                    if not acked:
                        acks.append(pid)
                        dirty = True
                if dirty:
                    body = json.dumps(incoming).encode()
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
    push_diagnostics({"recovery": recover_lost_rules(), "dedupe": dedupe_latest_backup(),
                      "rebuild": rebuild_from_known_good(), "patch": apply_remote_patch()})
    # Daily balance snapshot — runs in the background so history is recorded even when the
    # dashboard is never opened. Catches up on startup and once an hour thereafter.
    threading.Thread(target=snapshot_scheduler, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
