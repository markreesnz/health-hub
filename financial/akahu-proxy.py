#!/usr/bin/env python3
"""
Run this to open the financial plan dashboard with live Akahu balances.

Usage:  python3 akahu-proxy.py
        (leave the terminal open while using the dashboard)
"""
import urllib.request, urllib.error, json, webbrowser, os, shutil, threading, datetime, time, traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from state_store import StateStore, Conflict, atomic_json
from urllib.parse import urlparse, parse_qs

APP_TOKEN   = os.environ.get("AKAHU_APP_TOKEN", "")
USER_TOKEN  = os.environ.get("AKAHU_USER_TOKEN", "")
PORT        = int(os.environ.get("FIN_PORT", "8765"))
HERE        = os.path.dirname(os.path.abspath(__file__))
HTML_FILE   = os.path.join(HERE, "financial-plan-dashboard.html")
DATA_DIR    = os.environ.get("FIN_DATA_DIR", HERE)   # /share/financial on the HA add-on
BACKUP_DIR  = os.path.join(DATA_DIR, "backups")

# Server-side daily snapshots — written even when the dashboard isn't open. The dashboard
# merges any dates it doesn't already have into its own history on load.
AUTO_SNAP_FILE = os.path.join(DATA_DIR, "auto-snapshots.json")

VERSION = "2.0.0"
STORE = StateStore(DATA_DIR)

# Bank names and roles match the dashboard. Empty funds must stay empty.
AKAHU_SNAP_MAP = [
    ("Simplicity", "Conservative Fund", "conservative_balance", False),
    ("Simplicity", "Balanced Fund", "b2_balance", False),
    ("Simplicity", "Mark's Kiwisaver", "ks_balance", True),
    ("BNZ", "Bucket 1", "b1_float", False),
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


def _akahu_value(a, use_current=False):
    """Simplicity funds: prefer shares x price (live); else balance.current."""
    cur = (a.get("balance") or {}).get("current")
    if cur == 0 or use_current:
        return cur if isinstance(cur, (int, float)) else None
    port = ((a.get("meta") or {}).get("portfolio") or [None])[0]
    if port and isinstance(port.get("shares"), (int, float)) and isinstance(port.get("price"), (int, float)):
        return port["shares"] * port["price"]
    bal = a.get("balance") or {}
    return bal.get("current") if isinstance(bal.get("current"), (int, float)) else None


def _latest_backup_state():
    """Most recent dashboard state (for manual fields Akahu can't supply: cash, pending, nwExtra)."""
    if STORE.path.exists():
        return STORE.read()["state"]
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
        state = _latest_backup_state()
        PROTECTED = {"transactions", "snapshots", "payeeOverrides", "savedAt", "appliedPatchIds", "openLog", "akahuAppToken", "akahuUserToken"}
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
            if STORE.path.exists():
                current = STORE.read()
                STORE.save(current["revision"], state, "remote-" + str(pid))
            else:
                atomic_json(path, state)
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
                                  "version": VERSION, "revision": STORE.read()["revision"],
                                  "current_counts": {"transactions": len(_latest_backup_state().get("transactions", [])),
                                                     "rules": len([v for v in _latest_backup_state().get("payeeOverrides", {}).values() if v])},
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

    bal = dict(s)
    for conn, name, key, use_current in AKAHU_SNAP_MAP:
        account = next((a for a in accounts if (a.get("connection") or {}).get("name") == conn
                        and a.get("name") == name), None)
        if account is not None:
            value = _akahu_value(account, use_current)
            if value is not None:
                bal[key] = value
    def n(key):
        return float(bal.get(key) or 0)
    shift = flight = 0
    switch = s.get("switch_pending")
    if switch:
        amount = float(switch.get("amount") or 0)
        # Missing differs from zero: a fully emptied source really has left.
        source = bal.get(switch["from"])
        left = min(amount, max(0, float(switch.get("fromBaseline") or 0) - float(source or 0))) if source is not None else 0
        arrived = min(amount, max(0, n(switch["to"]) - float(switch.get("toBaseline") or 0)))
        shift, flight = max(0, amount - left), max(0, left - arrived)
    pending = 0
    if isinstance(s.get("b2_pending"), dict):
        p = s["b2_pending"]
        arrived = max(0, n("conservative_balance") + n("b2_balance") + n("b2_cash") - float(p.get("baseline") or 0))
        pending = max(0, float(p.get("amount") or 0) - arrived)
    return {
        "date": datetime.date.today().isoformat(),
        "b1_float": n("b1_float"), "b1_td6": n("b1_td6"), "b1_td12": n("b1_td12"),
        "b2": max(0, n("conservative_balance") - shift) + n("b2_cash") + pending,
        "b3": n("b3_balance") + n("b2_balance") + shift + flight, "ks": n("ks_balance"),
        "nwExtra": n("property_nottingham") + n("gentrack_shares") * n("gentrack_price")
                   + n("westpac_td_jun18") + n("westpac_td_jun20") + n("dvrp_net"),
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
            STORE.backup()
            today = datetime.date.today().isoformat()
            if today != last_date:
                write_daily_snapshot()
                last_date = today
        except Exception:
            print("[auto-snapshot] failed:\n" + traceback.format_exc())
        time.sleep(3600)


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(403)
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/financial-plan-dashboard.html"):
            self._serve_file(HTML_FILE, "text/html; charset=utf-8")
        elif path in ("/sync.js", "/calculations.js", "/app.js", "/migrations.js"):
            self._serve_file(os.path.join(HERE, path[1:]), "text/javascript; charset=utf-8")
        elif path == "/reference.html":
            self._serve_file(os.path.join(HERE, "reference.html"), "text/html; charset=utf-8")
        elif path == "/status":
            self._json(200, {"version": VERSION, "schemaVersion": 1,
                             "bankConfigured": bool(APP_TOKEN and USER_TOKEN),
                             "revision": STORE.read()["revision"]})
        elif path == "/state":
            try:
                self._json(200, STORE.read())
            except Exception:
                self._json(503, {"error": "Current state cannot be read. No changes have been saved."})
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

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/backup":
            self._json(409, {"error": "This version can no longer save. Reload Finance to update safely."})
            return
        if self.headers.get("X-Finance-Client") != VERSION:
            self._json(409, {"error": "Reload Finance to use the current version."})
            return
        if path == "/state":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 20 * 1024 * 1024:
                    raise ValueError("Invalid state size")
                payload = json.loads(self.rfile.read(length))
                state = payload["state"]
                # Preserve the historical ban list and cross-format duplicate protection.
                if isinstance(state, dict):
                    banned = _load_banned()
                    if isinstance(state.get("transactions"), list):
                        state["transactions"] = [t for t in state["transactions"]
                                                 if not isinstance(t, dict) or t.get("id") not in banned]
                    from state_store import validate_state
                    validate_state(state)
                    dedupe_cross_type(state)
                self._json(200, STORE.save(payload["revision"], state, payload["mutationId"]))
            except Conflict as e:
                self._json(409, {"error": "State changed on another device", "current": e.current})
            except (ValueError, KeyError, TypeError) as e:
                self._json(400, {"error": str(e)})
            except Exception:
                traceback.print_exc()
                self._json(503, {"error": "Save failed; your previous state is intact."})
        elif path == "/refresh":
            self._proxy_refresh()
        else:
            self._json(404, {"error": "Not found"})

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
        # Read compatibility only: old clients may view but may never overwrite new state.
        try:
            state = STORE.read()["state"]
            self._json(200 if state is not None else 404, state or {"error": "no backups"})
        except Exception:
            self._json(503, {"error": "State unavailable"})

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
            body = json.dumps({"success": all("error" not in r for r in results), "results": results}).encode()
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

    def _cors(self):
        # Same-origin UI. No wildcard cross-origin access to financial records.
        self.send_header("Cache-Control", "no-store")

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Opening dashboard at {url}")
    print("Keep this terminal open while using the dashboard.")
    print("Ctrl+C to stop.\n")
    if not os.environ.get("ADDON"):
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    recovery = {}
    if not STORE.path.exists():
        recovery = {"rules": recover_lost_rules(), "dedupe": dedupe_latest_backup(),
                    "rebuild": rebuild_from_known_good()}
        STORE.migrate(_latest_backup_state())
    patch = apply_remote_patch()
    push_diagnostics({"recovery": recovery, "patch": patch})
    # Daily balance snapshot — runs in the background so history is recorded even when the
    # dashboard is never opened. Catches up on startup and once an hour thereafter.
    threading.Thread(target=snapshot_scheduler, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
