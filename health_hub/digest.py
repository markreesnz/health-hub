"""Weekly Digest — one email of key insights across the three apps on the Green:
Health (this hub, :8768), Energy (dashboard :8766) and Money (Akahu proxy :8765).

Ported from the Mac's ~/bin/weekly-digest.py (LaunchAgent com.mark.weeklydigest,
retired 2026-07-13): the Mac sits on a different subnet to the Green, so the LAN
fetches never worked — the job now runs where the data lives.

Runs inside the health-hub add-on:
  - scheduler_loop() sends Mondays 07:30 Pacific/Auckland (same-day catch-up if
    the add-on was down at 07:30; won't double-send thanks to last_sent guard)
  - POST /digest/run  {dry: true} on the hub triggers a manual run
  - state (dated balance snapshots for week-over-week deltas) lives at
    /share/health/weekly_digest_state.json

Secrets come from add-on options: gmail_address + gmail_app_password (SMTP) and
anthropic_api_key (insights, optional — digest still sends without it).
"""

import csv
import html
import io
import json
import os
import smtplib
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    NZ = ZoneInfo("Pacific/Auckland")
except Exception:                                  # tzdata missing — scheduler still works, in UTC
    NZ = None

STATE_FILE = Path("/share/health/weekly_digest_state.json")
CREDS_FILE = Path("/share/health/digest_creds.json")
SEED_ENTITY = "sensor.weekly_digest_seed"
FIN_HIDE_ZERO = True

_cfg = {}
_anthropic_key = ""


def configure(cfg, anthropic_key):
    global _cfg, _anthropic_key
    _cfg, _anthropic_key = cfg or {}, anthropic_key or ""


def _base():
    return (_cfg.get("digest_base") or "http://192.168.0.209").rstrip("/")


def _hub():
    return "http://127.0.0.1:8768"


def log(msg):
    print(f"[digest] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def get(url, timeout=60, as_json=True):
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                body = r.read()
            return json.loads(body) if as_json else body.decode(errors="replace")
        except Exception as e:
            if attempt == 2:
                raise
            log(f"retrying {url} ({e})")


def post(url, payload=None, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ── State (dated balance snapshots) ─────────────────────────────────────────────
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def _gmail_creds():
    """(address, app_password) — /share creds file first, then add-on options."""
    try:
        c = json.loads(CREDS_FILE.read_text())
        if c.get("gmail_address") and c.get("gmail_app_password"):
            return c["gmail_address"], c["gmail_app_password"]
    except Exception:
        pass
    return _cfg.get("gmail_address", ""), _cfg.get("gmail_app_password", "")


def absorb_seed():
    """One-time bootstrap over the HA core API. The Supervisor options API isn't reachable
    off-LAN, so gmail creds and the migrated snapshot state arrive as attributes of a
    sensor set remotely (POST /api/states/sensor.weekly_digest_seed). Persist them to
    /share, then delete the sensor so the password doesn't linger in HA's state machine."""
    tok = os.environ.get("SUPERVISOR_TOKEN")
    if not tok:
        return
    base = "http://supervisor/core/api/states/" + SEED_ENTITY
    try:
        req = urllib.request.Request(base, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            attrs = json.loads(r.read()).get("attributes", {})
    except Exception:
        return                                     # sensor not set — nothing to absorb
    try:
        if attrs.get("gmail_address") and attrs.get("gmail_app_password"):
            CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
            CREDS_FILE.write_text(json.dumps({"gmail_address": attrs["gmail_address"],
                                              "gmail_app_password": attrs["gmail_app_password"]}))
            os.chmod(CREDS_FILE, 0o600)
            log("seed: gmail creds persisted")
        if attrs.get("snapshots"):
            state = load_state()
            merged = dict(attrs["snapshots"])
            merged.update(state.get("snapshots", {}))   # never clobber snapshots taken here
            state["snapshots"] = merged
            save_state(state)
            log(f"seed: {len(attrs['snapshots'])} snapshot(s) merged")
        req = urllib.request.Request(base, headers={"Authorization": f"Bearer {tok}"},
                                     method="DELETE")
        urllib.request.urlopen(req, timeout=10).read()
        log("seed: sensor deleted")
    except Exception as e:
        log(f"seed absorb failed: {e}")


def baseline_balances(snaps, today):
    """The snapshot closest to a week ago (whatever exists — better slightly off than empty)."""
    if not snaps:
        return {}, None
    target = today - timedelta(days=7)
    best = min(snaps, key=lambda ds: abs((date.fromisoformat(ds) - target).days))
    return snaps[best], best


def refresh_sources():
    """Ask each app for fresh data before reading it. Akahu's bank refresh is asynchronous,
    so trigger it first and give the banks a minute to report back; Oura sync is synchronous.
    Energy needs no nudge: the proxy pre-warms its Octopus cache every 30 min."""
    akahu = False
    try:
        post(f"{_base()}:8765/refresh", timeout=90)
        akahu = True
        log("akahu refresh triggered")
    except Exception as e:
        log(f"akahu refresh failed ({e}) — using last-synced balances")
    try:
        r = post(f"{_hub()}/oura/sync", {"days": 7}, timeout=120)
        log(f"oura sync: {'skipped (recent)' if r.get('skipped') else 'done'}")
    except Exception as e:
        log(f"oura sync failed ({e}) — using last-synced health data")
    if akahu:
        time.sleep(75)   # let Akahu finish pulling from BNZ/Westpac/Simplicity


def avg(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else None


def week_split(series, today):
    """[{t,v}] → (this-week avg, prior-week avg) by calendar date."""
    wk = (today - timedelta(days=7)).isoformat()
    prev = (today - timedelta(days=14)).isoformat()
    return (avg([p["v"] for p in series if p["t"] >= wk]),
            avg([p["v"] for p in series if prev <= p["t"] < wk]))


# ── Health ──────────────────────────────────────────────────────────────────────
def health_stats(today):
    hist = get(f"{_hub()}/history?days=14")
    w = get(f"{_hub()}/weight")
    out = {}
    for key, label, dec in [("sleep", "Sleep (h)", 1), ("hrv", "HRV (ms)", 0),
                            ("resting_hr", "Resting HR (bpm)", 0), ("steps", "Steps", 0),
                            ("active_energy", "Active energy (kcal)", 0),
                            ("readiness_score", "Readiness", 0)]:
        cur, prior = week_split(hist.get(key) or [], today)
        rnd = (lambda v: round(v, 1)) if dec else (lambda v: int(round(v)))
        if cur is not None:
            out[label] = {"week": rnd(cur), "prior": rnd(prior) if prior is not None else None}
    wh = w.get("history") or []
    ago = next((p["v"] for p in reversed(wh) if p["t"] <= (today - timedelta(days=7)).isoformat()), None)
    out["weight"] = {"current": w.get("current"), "week_ago": ago, "goal": w.get("goal"),
                     "trend_kg_wk": w.get("rate_kg_wk")}
    return out


# ── Energy ──────────────────────────────────────────────────────────────────────
def rate_period(day, slot):
    dow = date.fromisoformat(day).weekday()          # 0=Mon..6=Sun
    if slot < 14 or slot >= 46:
        return "night"
    if dow >= 5:
        return "offpeak"
    return "peak" if (slot <= 21 or 34 <= slot <= 41) else "offpeak"


def energy_stats(today):
    rates = get(f"{_base()}:8766/tariff")["rates"]
    bill = get(f"{_base()}:8766/bill")
    hw = get(f"{_base()}:8766/hotwater")
    body = get(f"{_base()}:8766/octopus?days=15", as_json=False)
    days = {}
    for row in csv.DictReader(io.StringIO(body)):
        d, t = row["read_start"][:10], row["read_start"][11:16]
        slot = int(t[:2]) * 2 + (1 if int(t[3:5]) >= 30 else 0)
        kwh = float(row["kwh"] or 0)
        rec = days.setdefault(d, {"kwh": 0.0, "cost": 0.0, "night": 0.0})
        rec["kwh"] += kwh
        rec["cost"] += kwh * rates[rate_period(d, slot)]
        if rate_period(d, slot) == "night":
            rec["night"] += kwh
    ds = sorted(days)[-14:]
    wk_ds, prev_ds = ds[-7:], ds[:-7]
    daily = rates.get("daily", 0)
    wk_cost = sum(days[d]["cost"] + daily for d in wk_ds)
    prev_cost = sum(days[d]["cost"] + daily for d in prev_ds) if prev_ds else None
    wk_kwh = sum(days[d]["kwh"] for d in wk_ds)
    night_pct = round(100 * sum(days[d]["night"] for d in wk_ds) / wk_kwh) if wk_kwh else None
    return {"week_cost": round(wk_cost, 2),
            "prior_week_cost": round(prev_cost, 2) if prev_cost is not None else None,
            "week_kwh": round(wk_kwh, 1), "night_pct": night_pct,
            "metered_through": ds[-1] if ds else None,
            "hot_water_level": hw.get("level"),
            "balance_owing": bill.get("amount_owing"),
            "payment_due": bill.get("payment_due_date"),
            "payment_note": "collected automatically by direct debit — no action needed",
            "next_bill_date": bill.get("next_billing_date")}


# ── Money ───────────────────────────────────────────────────────────────────────
def money_stats(today):
    accounts = get(f"{_base()}:8765/accounts")["items"]
    txns = get(f"{_base()}:8765/transactions")["items"]
    prev, prev_date = baseline_balances(load_state().get("snapshots", {}), today)
    accts = []
    for a in accounts:
        cur = (a.get("balance") or {}).get("current") or 0
        name = f"{(a.get('connection') or {}).get('name', '')} {a.get('name', '')}".strip()
        change = round(cur - prev[name], 2) if name in prev else None
        # Skip empty accounts unless they moved this week (an account emptied to $0 is news).
        if FIN_HIDE_ZERO and abs(cur) < 1 and not change:
            continue
        accts.append({"name": name, "balance": round(cur, 2), "change": change})
    # Term deposits: Akahu reports them as $0 — the real values live in the dashboard's manual
    # state, served by the financial proxy's /term-deposits (from the latest daily backup).
    td_labels = {"b1_td6": ("BNZ TD 6M PIE", "3.65% · matures 8 Dec 2026"),
                 "b1_td12": ("BNZ TD 12M PIE", "4.15% · matures 8 Jun 2027")}
    try:
        for k, v in get(f"{_base()}:8765/term-deposits").items():
            if k not in td_labels:
                continue
            name, note = td_labels[k]
            change = round(v - prev[name], 2) if name in prev else None
            accts.append({"name": name, "note": note, "balance": round(v, 2), "change": change})
    except Exception as e:
        log(f"term deposits unavailable ({e})")
    accts.sort(key=lambda x: -x["balance"])
    all_balances = {f"{(a.get('connection') or {}).get('name', '')} {a.get('name', '')}".strip():
                    round((a.get("balance") or {}).get("current") or 0, 2) for a in accounts}
    all_balances.update({a["name"]: a["balance"] for a in accts})   # incl. term deposits
    wk = (today - timedelta(days=7)).isoformat()
    # Internal transfers between Mark's own accounts would otherwise dominate both totals.
    recent = [t for t in txns if (t.get("date") or "")[:10] >= wk
              and t.get("type") not in ("TRANSFER", "STANDING ORDER")]
    money_in = round(sum(t["amount"] for t in recent if t["amount"] > 0), 2)
    money_out = round(-sum(t["amount"] for t in recent if t["amount"] < 0), 2)
    top = sorted((t for t in recent if t["amount"] < 0), key=lambda t: t["amount"])[:3]
    return {"accounts": accts, "all_balances": all_balances,
            "week_in": money_in, "week_out": money_out,
            "txn_count": len(recent),
            "top_spends": [{"desc": t.get("description", "")[:60], "amount": round(-t["amount"], 2),
                            "date": (t.get("date") or "")[:10]} for t in top]}


# ── Insights via Claude ─────────────────────────────────────────────────────────
def claude_insights(stats):
    if not _anthropic_key:
        raise RuntimeError("no anthropic key configured")
    prompt = (
        "You are writing the 'Key insights' section of Mark's weekly personal digest, from this "
        "week's data across his health, home-energy and money apps (JSON below; 'prior' = the week "
        "before). Write 4-6 one-line insights: lead with what changed or needs action, compare "
        "week-on-week where the data allows, be specific with numbers, no filler, no headings. "
        "Return ONLY a JSON array of strings.\n\n" + json.dumps(stats)
    )
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        headers={"Content-Type": "application/json", "x-api-key": _anthropic_key,
                 "anthropic-version": "2023-06-01"},
        data=json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 800,
                         "messages": [{"role": "user", "content": prompt}]}).encode())
    with urllib.request.urlopen(req, timeout=90) as r:
        text = json.load(r)["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()
    return json.loads(text)


# ── Email ───────────────────────────────────────────────────────────────────────
def fmt_money(v):
    return f"${v:,.2f}"


def build_html(today, insights, h, e, m):
    esc = html.escape
    css_h2 = "font-size:15px;margin:26px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px"
    row = "padding:3px 10px 3px 0;font-size:13.5px"
    # musl strftime has no %-d — build the date by hand
    date_str = f"{today.day} {today.strftime('%b %Y')}"
    parts = [f"<div style='font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:640px;"
             f"margin:0 auto;color:#1a1a1a'><h1 style='font-size:19px'>Weekly Digest — {date_str}</h1>"]
    if insights:
        parts.append("<ul style='padding-left:20px;line-height:1.7;font-size:14px'>")
        parts += [f"<li>{esc(i)}</li>" for i in insights]
        parts.append("</ul>")

    parts.append(f"<h2 style='{css_h2}'>🏋️ Health</h2><table>")
    wt = h.pop("weight", {})
    for label, v in h.items():
        delta = ""
        if v.get("prior") not in (None, 0):
            d = v["week"] - v["prior"]
            delta = f" <span style='color:#888'>({'+' if d >= 0 else '−'}{abs(round(d, 1))} vs prior wk)</span>"
        parts.append(f"<tr><td style='{row}'>{esc(label)}</td><td style='{row}'><b>{v['week']}</b>{delta}</td></tr>")
    if wt.get("current") is not None:
        d = f" ({wt['current'] - wt['week_ago']:+.1f} kg this week)" if wt.get("week_ago") else ""
        parts.append(f"<tr><td style='{row}'>Weight</td><td style='{row}'><b>{wt['current']} kg</b>"
                     f"{esc(d)} <span style='color:#888'>goal {wt.get('goal')} kg</span></td></tr>")
    parts.append("</table>")

    parts.append(f"<h2 style='{css_h2}'>⚡ Energy</h2><table>")
    wow = ""
    if e.get("prior_week_cost"):
        d = e["week_cost"] - e["prior_week_cost"]
        wow = f" <span style='color:{'#c0392b' if d > 0 else '#1e8e3e'}'>({'+' if d >= 0 else '−'}{fmt_money(abs(d))} vs prior wk)</span>"
    parts.append(f"<tr><td style='{row}'>Power cost (7d)</td><td style='{row}'><b>{fmt_money(e['week_cost'])}</b>{wow}</td></tr>")
    parts.append(f"<tr><td style='{row}'>Usage (7d)</td><td style='{row}'><b>{e['week_kwh']} kWh</b> · night-rate share {e.get('night_pct')}%</td></tr>")
    parts.append(f"<tr><td style='{row}'>Hot water</td><td style='{row}'><b>{esc(str(e.get('hot_water_level')))}</b></td></tr>")
    if e.get("balance_owing"):
        due = f" · due {e['payment_due']}" if e.get("payment_due") else ""
        parts.append(f"<tr><td style='{row}'>Octopus owing</td><td style='{row}'><b>{fmt_money(e['balance_owing'])}</b>{esc(due)}</td></tr>")
    parts.append(f"<tr><td style='{row}'>Next bill</td><td style='{row}'>{esc(str(e.get('next_bill_date')))}"
                 f" <span style='color:#888'>(metered through {esc(str(e.get('metered_through')))})</span></td></tr></table>")

    parts.append(f"<h2 style='{css_h2}'>💰 Money</h2><table>")
    parts.append(f"<tr><td style='{row}'>This week</td><td style='{row}'>in <b style='color:#1e8e3e'>{fmt_money(m['week_in'])}</b>"
                 f" · out <b style='color:#c0392b'>{fmt_money(m['week_out'])}</b> · {m['txn_count']} transactions</td></tr>")
    for t in m["top_spends"]:
        parts.append(f"<tr><td style='{row};color:#888'>{esc(t['date'])}</td><td style='{row}'>{fmt_money(t['amount'])} — {esc(t['desc'])}</td></tr>")
    parts.append("</table><table style='margin-top:8px'>")
    for a in m["accounts"]:
        c = a.get("change")
        if c is None:
            chg = "<span style='color:#bbb'>—</span>"
        elif abs(c) < 0.01:
            chg = "<span style='color:#bbb'>$0.00</span>"
        else:
            chg = (f"<span style='color:{'#1e8e3e' if c > 0 else '#c0392b'}'>"
                   f"{'+' if c > 0 else '−'}{fmt_money(abs(c))}</span>")
        note = f" <span style='color:#999;font-size:11px'>{esc(a['note'])}</span>" if a.get("note") else ""
        parts.append(f"<tr><td style='{row}'>{esc(a['name'])}{note}</td>"
                     f"<td style='{row};text-align:right'><b>{fmt_money(a['balance'])}</b></td>"
                     f"<td style='{row};text-align:right;font-size:12px'>{chg}</td></tr>")
    total = sum(a["balance"] for a in m["accounts"])
    tchg = [a["change"] for a in m["accounts"] if a.get("change") is not None]
    tc = sum(tchg) if tchg else None
    tcs = ("<span style='color:#bbb'>—</span>" if tc is None
           else f"<span style='color:{'#1e8e3e' if tc >= 0 else '#c0392b'}'>{'+' if tc >= 0 else '−'}{fmt_money(abs(tc))}</span>")
    parts.append(f"<tr><td style='{row};border-top:1px solid #ddd'>Total</td>"
                 f"<td style='{row};text-align:right;border-top:1px solid #ddd'><b>{fmt_money(total)}</b></td>"
                 f"<td style='{row};text-align:right;border-top:1px solid #ddd;font-size:12px'>{tcs}</td></tr>")
    parts.append("</table>")
    parts.append("<p style='color:#999;font-size:11px;margin-top:26px'>Generated from the "
                 "health, energy and financial apps on the HA Green · health-hub add-on digest.py</p></div>")
    return "".join(parts)


def run(dry=False, skip_refresh=False):
    """Build (and unless dry, send) the digest. Returns a result dict for the API route."""
    # NZ date, not container-local (UTC): Monday 07:30 NZ is still Sunday in UTC, and the
    # scheduler's last_sent guard compares against the NZ date.
    today = _now_nz().date()
    if not skip_refresh:
        refresh_sources()
    stats, sections = {}, {}
    for name, fn in [("health", health_stats), ("energy", energy_stats), ("money", money_stats)]:
        try:
            sections[name] = fn(today)
            stats[name] = sections[name]
            log(f"{name}: ok")
        except Exception as e:
            log(f"{name}: FAILED ({e})")
            sections[name] = None
    if not any(sections.values()):
        log("no data from any app — not sending")
        return {"ok": False, "error": "no data from any app"}
    insights = []
    try:
        insights = claude_insights(stats)
        log(f"insights: {len(insights)}")
    except Exception as e:
        log(f"insights failed ({e}) — sending numbers only")
    body = build_html(today, insights, sections.get("health") or {},
                      sections.get("energy") or {},
                      sections.get("money") or {"accounts": [], "week_in": 0, "week_out": 0,
                                                "txn_count": 0, "top_spends": []})
    if dry:
        log("dry run — not sending")
        return {"ok": True, "dry": True, "html": body}

    address, password = _gmail_creds()
    if not address or not password:
        log("gmail_address / gmail_app_password not configured — cannot send")
        return {"ok": False, "error": "gmail options not configured", "html": body}
    msg = MIMEText(body, "html")
    msg["Subject"] = f"Weekly Digest — health · energy · money — {today.day} {today.strftime('%b')}"
    msg["From"] = address
    msg["To"] = address
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(address, password)
        smtp.send_message(msg)
    log("sent")
    state = load_state()
    if sections.get("money"):
        snaps = state.get("snapshots", {})
        snaps[today.isoformat()] = sections["money"]["all_balances"]
        cutoff = (today - timedelta(days=35)).isoformat()
        state["snapshots"] = {d: b for d, b in snaps.items() if d >= cutoff}
        log("balance snapshot saved")
    state["last_sent"] = today.isoformat()
    save_state(state)
    return {"ok": True, "sent": True}


# ── Scheduler — Mondays 07:30 NZ, with same-day catch-up ────────────────────────
def _now_nz():
    return datetime.now(NZ) if NZ else datetime.now()


def scheduler_loop():
    # Boot grace: leaves a window to migrate/adjust state after an add-on update
    # before a pending catch-up send fires.
    time.sleep(300)
    log(f"scheduler up — Mondays 07:30 {'NZ' if NZ else 'container-local'} time")
    while True:
        try:
            absorb_seed()
            now = _now_nz()
            due = now.weekday() == 0 and (now.hour, now.minute) >= (7, 30)
            if due and load_state().get("last_sent") != now.date().isoformat():
                log("scheduled send starting")
                run()
        except Exception as e:
            log(f"scheduler error: {e}")
        time.sleep(60)


def start_scheduler():
    threading.Thread(target=scheduler_loop, daemon=True).start()
