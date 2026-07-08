#!/usr/bin/env python3
"""
Data proxy for the Energy Dashboard. Serves the dashboard and bridges
Home Assistant (heater state, per-room energy statistics) to it.
Heater control and scheduling live in Home Assistant.

Runs as a Home Assistant add-on on the Green (see run.sh / config.yaml); the
same file still runs standalone on the Mac with `python3 energy-proxy.py`.
"""

import json
import os
import sys
import time
import shutil
import subprocess
import tempfile
import threading
import webbrowser
import datetime
import urllib.error
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlparse, quote

# ── Runtime context ─────────────────────────────────────────────────────────────
# The same file runs standalone on the Mac and as a Home Assistant add-on on the Green.
# On the Green, Supervisor injects SUPERVISOR_TOKEN and run.sh sets the ENERGY_* paths.
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
ADDON = bool(SUPERVISOR_TOKEN or os.environ.get("ADDON"))
DATA_DIR = Path(os.environ.get("ENERGY_DATA_DIR", Path(__file__).parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)

LOG_PATH  = DATA_DIR / "heating-state.jsonl"
SCHED_FILE = DATA_DIR / "schedule.json"      # display-only heating windows (HA is the real scheduler)
DASHBOARD = Path(os.environ.get("ENERGY_HTML", Path(__file__).parent / "index.html"))
PORT      = 8766

ROOMS = ["lounge", "main_bedroom", "bathroom", "back_bedroom", "study"]
APPLIANCES = [("fridge", "Fridge"), ("washer_dryer", "Washer/Dryer"), ("tv_stereo", "TV / Stereo")]

_DEFAULT_SCHEDULE = [
    {"label": "Morning", "on": "05:30", "off": "07:00"},
    {"label": "Evening", "on": "18:00", "off": "21:30"},
]


def get_schedule():
    try:
        return json.loads(SCHED_FILE.read_text()).get("schedule", _DEFAULT_SCHEDULE)
    except Exception:
        return _DEFAULT_SCHEDULE


def save_schedule(windows):
    SCHED_FILE.write_text(json.dumps({"schedule": windows}, indent=2))


def schedule_status():
    now = datetime.datetime.now()
    t   = now.strftime("%H:%M")
    for w in get_schedule():
        if w["on"] <= t < w["off"]:
            return {"active": True, "label": w["label"], "next": "off", "at": w["off"]}
    windows = sorted(get_schedule(), key=lambda w: w["on"])
    nxt = next((w for w in windows if w["on"] > t), windows[0])
    return {"active": False, "next": "on", "label": nxt["label"], "at": nxt["on"]}


# ── Home Assistant ─────────────────────────────────────────────────────────────
# As an add-on we reach Core through the Supervisor proxy with the injected token;
# standalone on the Mac we hit the LAN address with a long-lived token file.
if SUPERVISOR_TOKEN:
    HA_API = "http://supervisor/core/api"
    HA_WS  = "ws://supervisor/core/websocket"
    def ha_token():
        return SUPERVISOR_TOKEN
else:
    HA_URL = "192.168.0.209:8123"
    HA_API = f"http://{HA_URL}/api"
    HA_WS  = f"ws://{HA_URL}/api/websocket"
    HA_TOKEN_PATH = Path.home() / "scripts/tuya/ha_token"
    def ha_token():
        return HA_TOKEN_PATH.read_text().strip()


def _ha_states():
    req = urllib.request.Request(
        f"{HA_API}/states", headers={"Authorization": f"Bearer {ha_token()}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return {s["entity_id"]: s for s in json.loads(resp.read())}


def fetch_appliance_power():
    """Current watts per appliance from HA states (server-side, so the browser needs no HA token)."""
    states = _ha_states()
    out = {}
    for key, _ in APPLIANCES:
        try:
            out[key] = float(states.get(f"sensor.{key}_power", {}).get("state"))
        except (TypeError, ValueError):
            out[key] = None
    return out


def fetch_room_states():
    """Per-room on/temp/watts from HA states."""
    states = _ha_states()
    rooms = {}
    for room in ROOMS:
        climate = states.get(f"climate.{room}", {})
        power   = states.get(f"sensor.{room}_power", {})
        attrs   = climate.get("attributes", {})
        try:
            watts = float(power.get("state", 0))
        except (TypeError, ValueError):
            watts = 0
        rooms[room] = {
            "on":   climate.get("state") == "heat",
            "temp": attrs.get("current_temperature"),
            "w":    watts,
        }
    return rooms


def fetch_ha_statistics(statistic_ids, start_date):
    """Hourly mean statistics from HA recorder for the given ids."""
    import websocket
    start_local = datetime.datetime.fromisoformat(start_date).astimezone()
    ws = websocket.create_connection(HA_WS, timeout=10)
    try:
        ws.recv()
        ws.send(json.dumps({"type": "auth", "access_token": ha_token()}))
        auth = json.loads(ws.recv())
        if auth.get("type") != "auth_ok":
            raise RuntimeError("HA auth failed")
        ws.send(json.dumps({
            "id": 1, "type": "recorder/statistics_during_period",
            "start_time": start_local.isoformat(), "period": "hour",
            "statistic_ids": statistic_ids, "types": ["mean"],
        }))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == 1 and r.get("type") == "result":
                break
        if not r.get("success"):
            raise RuntimeError(str(r.get("error")))
        return r.get("result") or {}
    finally:
        ws.close()


def fetch_heating_energy(start_date):
    """Hourly kWh per room from HA recorder statistics (mean W / 1000)."""
    sensors = {room: f"sensor.{room}_power" for room in ROOMS}
    stats = fetch_ha_statistics(list(sensors.values()), start_date)
    rooms = {}
    for room, sensor in sensors.items():
        rooms[room] = [
            {"start": datetime.datetime.fromtimestamp(row["start"] / 1000).isoformat(timespec="minutes"),
             "kwh": round((row.get("mean") or 0) / 1000, 4)}
            for row in stats.get(sensor, [])
        ]
    return rooms


def fetch_appliance_energy(start_date):
    """Hourly kWh per appliance from HA recorder statistics (Tuya plug power sensors)."""
    sensors = {key: f"sensor.{key}_power" for key, _ in APPLIANCES}
    stats = fetch_ha_statistics(list(sensors.values()), start_date)
    out = {}
    for key, sensor in sensors.items():
        out[key] = [
            {"start": datetime.datetime.fromtimestamp(row["start"] / 1000).isoformat(timespec="minutes"),
             "kwh": round((row.get("mean") or 0) / 1000, 4)}
            for row in stats.get(sensor, [])
        ]
    return out


def fetch_context_stats(start_date):
    """Hourly outdoor temperature and per-person home fractions from HA."""
    people = {"mark": "sensor.mark_home", "lucia": "sensor.lucia_home", "lucy": "sensor.lucy_home"}
    ids = {"temp": "sensor.outdoor_temperature", **people}
    stats = fetch_ha_statistics(list(ids.values()), start_date)
    def rows(sid):
        return [
            {"start": datetime.datetime.fromtimestamp(row["start"] / 1000).isoformat(timespec="minutes"),
             "mean": round(row.get("mean") or 0, 2)}
            for row in stats.get(sid, [])
        ]
    return {"temp": rows(ids["temp"]),
            "people": {name: rows(sid) for name, sid in people.items()}}


# ── Octopus NZ smart meter data ────────────────────────────────────────────────

OCTO_CFG_PATH = Path(os.environ.get("ENERGY_OCTO_CFG", Path.home() / "scripts/octopus/config.json"))
OCTO_URL = "https://api.oenz-kraken.energy/v1/graphql/"
_octo_token = {"token": None, "expires": 0}


def octo_gql(query, variables, token=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token
    req = urllib.request.Request(
        OCTO_URL, headers=headers,
        data=json.dumps({"query": query, "variables": variables}).encode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read())
    if resp.get("errors"):
        raise RuntimeError(resp["errors"][0].get("message", "GraphQL error"))
    return resp["data"]


def octo_token():
    if _octo_token["token"] and time.time() < _octo_token["expires"]:
        return _octo_token["token"]
    cfg = json.loads(OCTO_CFG_PATH.read_text())
    data = octo_gql(
        """mutation($email: String!, $pw: String!) {
             obtainKrakenToken(input: {email: $email, password: $pw}) { token }
           }""",
        {"email": cfg["email"], "pw": cfg["password"]})
    _octo_token["token"] = data["obtainKrakenToken"]["token"]
    _octo_token["expires"] = time.time() + 50 * 60
    return _octo_token["token"]


_octo_cache = {}  # days -> (fetched_at, csv_bytes)
OCTO_CACHE_TTL = 6 * 3600


def _request_usage_csv(days):
    cfg = json.loads(OCTO_CFG_PATH.read_text())
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    # Requests reaching before the supply start date are rejected as Unauthorized.
    supply_start = datetime.date.fromisoformat(cfg.get("supply_start", "2026-06-03"))
    start = max(start, supply_start)
    mutation = """mutation($input: RequestUsageDataInputType!) {
                    requestUsageData(input: $input) { downloadUrl }
                  }"""
    variables = {"input": {"accountNumber": cfg["account"], "icpNumber": cfg["icp"],
                           "startDate": start.isoformat(), "endDate": end.isoformat()}}
    try:
        data = octo_gql(mutation, variables, token=octo_token())
    except RuntimeError as e:
        if "unauthorized" not in str(e).lower():
            raise
        # Kraken invalidates older tokens when new ones are issued — re-auth once.
        _octo_token["token"] = None
        data = octo_gql(mutation, variables, token=octo_token())
    url = data["requestUsageData"]["downloadUrl"]
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


# ── Fresher overlay: the half-hourly `measurements` API matches the Octopus app and leads the
# requestUsageData bulk export by ~a day — but it 504s on wide windows (fine to ~7 days, dies at
# 14+). So use the fast export for the full history and overlay measurements for the recent days.
NZ_TZ = ZoneInfo("Pacific/Auckland")
# EIEP13 register identifier suffix → content code: :1 = UN (anytime/uncontrolled),
# :2 = CN (controlled load — hot water). Validated against the EIEP13 export 2026-06-27.
_CHANNEL_CONTENT = {"1": "UN", "2": "CN"}
OCTO_RECENT_DAYS = 4  # measurements overlay window — kept small to stay under the API's gateway timeout
_EIEP_HEADER = ("icp_number,serial_number,energy_direction,register_content_code,channel_number,"
                "period_of_availability,read_start,read_end,read_quality,kwh")

_MEAS_Q = """query($a:String!,$s:DateTime!,$e:DateTime!,$rid:String!,$after:String){
  account(accountNumber:$a){ properties{ measurements(first:600, after:$after,
    startAt:$s, endAt:$e, timezone:"Pacific/Auckland",
    utilityFilters:{electricityFilters:{registerId:$rid, readingFrequencyType:THIRTY_MIN_INTERVAL}}){
      pageInfo{ hasNextPage endCursor }
      edges{ node{ value ... on IntervalMeasurementType{ startAt endAt } } } } } } }"""


def _octo_registers(token, account):
    """Discover this account's consumption registers → [(registerId, channel, content_code, serial, icp)]."""
    q = """query($a:String!){account(accountNumber:$a){properties{meterPoints{
              marketIdentifier registers{ id identifier isFeedIn } }}}}"""
    data = octo_gql(q, {"a": account}, token=token)
    out = []
    for prop in data["account"]["properties"]:
        for mp in (prop.get("meterPoints") or []):
            icp = mp.get("marketIdentifier") or ""
            for reg in mp.get("registers", []):
                if reg.get("isFeedIn"):
                    continue
                serial, _, chan = (reg.get("identifier") or "").partition(":")
                content = _CHANNEL_CONTENT.get(chan)
                if content:
                    out.append((reg["id"], chan, content, serial, icp))
    return out


def _measurements_recent(days):
    """Recent half-hourly data via the fresher measurements API (small window only — it 504s on
    wide ranges). Returns {(read_start, content_code): eiep13_csv_line}."""
    token = octo_token()
    cfg = json.loads(OCTO_CFG_PATH.read_text())
    end = datetime.datetime.now(NZ_TZ)
    start = end - datetime.timedelta(days=days)
    out = {}
    for rid, chan, content, serial, icp in _octo_registers(token, cfg["account"]):
        after = None
        while True:
            data = octo_gql(_MEAS_Q, {"a": cfg["account"], "s": start.isoformat(),
                                      "e": end.isoformat(), "rid": rid, "after": after},
                            token=token, timeout=60)
            conns = [p["measurements"] for p in data["account"]["properties"] if p.get("measurements")]
            if not conns:
                break
            m = conns[0]
            for edge in m["edges"]:
                n = edge["node"]
                if not n.get("startAt"):
                    continue
                st = datetime.datetime.fromisoformat(n["startAt"]).astimezone(NZ_TZ)
                en = (datetime.datetime.fromisoformat(n["endAt"]).astimezone(NZ_TZ)
                      if n.get("endAt") else st + datetime.timedelta(minutes=30))
                rs = st.strftime("%Y-%m-%d %H:%M:%S")
                out[(rs, content)] = ",".join([
                    icp or serial, serial, "Import", content, chan, "",
                    rs, en.strftime("%Y-%m-%d %H:%M:%S"), "Actual", f"{float(n['value']):.5f}"])
            if m["pageInfo"]["hasNextPage"]:
                after = m["pageInfo"]["endCursor"]
            else:
                break
    return out


def _merge_csv(export_bytes, overlay):
    """Merge bulk-export CSV with measurement overlay rows, keyed by (read_start, content_code) so
    the fresher measurements win and the export only fills gaps they don't cover. Re-emits one
    deduped, sorted EIEP13-style CSV."""
    rows = dict(overlay)
    lines = export_bytes.decode(errors="replace").splitlines() if export_bytes else []
    if lines:
        hdr = lines[0].split(",")
        try:
            i_rs, i_rc = hdr.index("read_start"), hdr.index("register_content_code")
        except ValueError:
            i_rs = None
        for ln in lines[1:]:
            c = ln.split(",")
            if i_rs is None or len(c) <= max(i_rs, i_rc):
                continue
            rows.setdefault((c[i_rs], c[i_rc]), ln)
    ordered = sorted(rows.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    return (_EIEP_HEADER + "\n" + "\n".join(v for _, v in ordered) + "\n").encode()


def _octo_cache_file(days):
    return DATA_DIR / f"octopus-cache-{days}d.csv"


def _octo_refresh(days):
    try:
        base = _request_usage_csv(days)  # fast bulk export (lags ~1-2 days)
    except Exception as e:
        sys.stderr.write(f"[octopus] usage export failed ({e})\n")
        base = b""
    try:
        overlay = _measurements_recent(OCTO_RECENT_DAYS)  # fresher last few days
    except Exception as e:
        sys.stderr.write(f"[octopus] measurements overlay failed ({e}); export only\n")
        overlay = {}
    if not base and not overlay:
        raise RuntimeError("Octopus: both usage export and measurements unavailable")
    body = _merge_csv(base, overlay)
    _octo_cache[days] = (time.time(), body)
    try:
        _octo_cache_file(days).write_bytes(body)
    except Exception:
        pass
    return body


_octo_refreshing = set()   # days values with a background refresh in flight


def fetch_octopus_csv(days):
    """Stale-while-revalidate: any cached copy (memory, then disk — survives restarts) is served
    immediately; if it's past TTL a background refresh replaces it for the next request. Only the
    very first fetch ever blocks on Octopus, so the dashboard never waits ~a minute on the export."""
    cached = _octo_cache.get(days)
    if not cached:
        f = _octo_cache_file(days)
        if f.exists():
            cached = _octo_cache[days] = (f.stat().st_mtime, f.read_bytes())
    if cached:
        if time.time() - cached[0] >= OCTO_CACHE_TTL and days not in _octo_refreshing:
            _octo_refreshing.add(days)

            def _bg():
                try:
                    _octo_refresh(days)
                except Exception as e:
                    sys.stderr.write(f"[octopus] background refresh failed ({e})\n")
                finally:
                    _octo_refreshing.discard(days)

            threading.Thread(target=_bg, daemon=True).start()
        return cached[1]
    return _octo_refresh(days)


OCTO_WARM_DAYS = 30   # matches the dashboard's fetch window


def octo_warm_loop():
    """Pre-load the Octopus data at startup and keep the cache warm, so the dashboard's request
    is always served instantly from cache (fetch_octopus_csv refreshes in the background once
    the TTL lapses; this loop just makes sure that happens without waiting for a visitor)."""
    while True:
        try:
            fetch_octopus_csv(OCTO_WARM_DAYS)
        except Exception as e:
            sys.stderr.write(f"[octopus] cache warm failed ({e})\n")
        time.sleep(1800)


_tariff_cache = {"at": 0, "rates": None}

def fetch_tariff():
    """Current plan rates from the active agreement ($/kWh incl. tax)."""
    if _tariff_cache["rates"] and time.time() - _tariff_cache["at"] < 24 * 3600:
        return _tariff_cache["rates"]
    cfg = json.loads(OCTO_CFG_PATH.read_text())
    q = """query($acc: String!, $pid: ID!) {
      account(accountNumber: $acc) {
        property(id: $pid) {
          meterPoints { marketIdentifier
            activeAgreement { displayName
              rates { label bandCategory touBucketName rateIncludingTax } } }
        }
      }
    }"""
    data = octo_gql(q, {"acc": cfg["account"], "pid": cfg.get("property_id", "27574")},
                    token=octo_token())
    mp = next(m for m in data["account"]["property"]["meterPoints"]
              if m["marketIdentifier"] == cfg["icp"])
    bucket_map = {"OFFPEAK": "night", "SHOULDER": "offpeak", "PEAK": "peak"}
    rates = {"plan": mp["activeAgreement"]["displayName"]}
    for r in mp["activeAgreement"]["rates"]:
        cents = float(r["rateIncludingTax"])
        if r["bandCategory"] == "STANDING_CHARGE":
            rates["daily"] = round(cents / 100, 6)
        elif r["touBucketName"] in bucket_map:
            rates[bucket_map[r["touBucketName"]]] = round(cents / 100, 5)
    _tariff_cache.update(at=time.time(), rates=rates)
    return rates


_bill_cache = {"at": 0, "data": None}


def fetch_billing():
    """Billing period + next bill date from Kraken (no amount forecast exists for NZ —
    KT-CT-3949 — so the dashboard estimates the amount from the metered usage)."""
    if _bill_cache["data"] and time.time() - _bill_cache["at"] < 12 * 3600:
        return _bill_cache["data"]
    cfg = json.loads(OCTO_CFG_PATH.read_text())
    q = """query($a: String!) {
      account(accountNumber: $a) {
        balance
        billingOptions { currentBillingPeriodStartDate currentBillingPeriodEndDate nextBillingDate }
        bills(first: 1) { edges { node { issuedDate fromDate toDate } } }
      }
    }"""
    acct = octo_gql(q, {"a": cfg["account"]}, token=octo_token())["account"]
    opts = acct.get("billingOptions") or {}
    edges = ((acct.get("bills") or {}).get("edges")) or []
    data = {
        "balance": acct.get("balance"),
        "period_start": opts.get("currentBillingPeriodStartDate"),
        "period_end": opts.get("currentBillingPeriodEndDate"),
        "next_billing_date": opts.get("nextBillingDate"),
        "last_bill": edges[0]["node"] if edges else None,
    }
    _bill_cache.update(at=time.time(), data=data)
    return data


# ── Hot water control (Octopus Next.js server actions) ──────────────────────────
# The hot-water Maximiser setting isn't in the Kraken API — the Octopus web app drives it via
# Next.js server actions on the hot-water-control page. They take account/icp in the body and
# need NO auth, so we can read (and, with the setter id, set) the level: OFF/FLEX/SMART/MAXIMISER.
# The action ids are per-build hashes that rotate whenever Octopus deploys, so we discover them
# at runtime from the page's JS chunks (production bundles keep the action *names* as the last
# argument of createServerReference), cache them, and re-discover whenever a call 404s.
HW_PROPERTY_ID = "27574"
HW_ACTION_NAMES = {"get": "getHotWaterCustomerPreference", "set": "submitCustomerPreference"}
HW_ACTIONS_CACHE = DATA_DIR / "hotwater-actions.json"
HW_LEVELS = ("OFF", "FLEX", "SMART", "MAXIMISER")


def _hw_url(account):
    return f"https://octopusenergy.nz/dashboard/{account}/properties/{HW_PROPERTY_ID}/hot-water-control"


# Next-Router-State-Tree the server action expects (captured; encodes account A-AB3DCE8C + property
# 27574 + the hot-water-control route). Rebuilt by URL-decoding, swapping account, re-encoding.
_HW_NRST_TEMPLATE = ("%5B%22%22%2C%7B%22children%22%3A%5B%22dashboard%22%2C%7B%22children%22%3A%5B%5B"
    "%22accountNumber%22%2C%22{acct}%22%2C%22d%22%5D%2C%7B%22children%22%3A%5B%22properties%22%2C%7B"
    "%22children%22%3A%5B%5B%22propertyId%22%2C%22{prop}%22%2C%22d%22%5D%2C%7B%22children%22%3A%5B%22"
    "hot-water-control%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull"
    "%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2C"
    "null%2Cnull%2Ctrue%5D")


def _hw_discover_action_ids(account):
    """Scrape the current get/set action ids out of the hot-water page's JS chunks."""
    import re
    ua = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(_hw_url(account), headers=ua)
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode()
    ids = {}
    for chunk in sorted(set(re.findall(r'src="(/_next/static/[^"]+\.js)"', html))):
        try:
            req = urllib.request.Request("https://octopusenergy.nz" + chunk, headers=ua)
            with urllib.request.urlopen(req, timeout=30) as r:
                js = r.read().decode()
        except Exception:
            continue
        for m in re.finditer(r'createServerReference\)?\(["\']([0-9a-f]{40,44})["\'][^)]*?["\'](\w+)["\']\)', js):
            for key, name in HW_ACTION_NAMES.items():
                if m.group(2) == name:
                    ids[key] = m.group(1)
        if len(ids) == len(HW_ACTION_NAMES):
            break
    if len(ids) != len(HW_ACTION_NAMES):
        raise RuntimeError(f"could not discover hot-water action ids (got {ids})")
    HW_ACTIONS_CACHE.write_text(json.dumps(ids))
    sys.stderr.write(f"[hotwater] discovered action ids: {ids}\n")
    return ids


def _hw_action_ids(account, refresh=False):
    if not refresh:
        try:
            ids = json.loads(HW_ACTIONS_CACHE.read_text())
            if all(k in ids for k in HW_ACTION_NAMES):
                return ids
        except Exception:
            pass
    return _hw_discover_action_ids(account)


def _hw_action(account, which, payload):
    """Call the named ('get'/'set') server action; on a stale-id 404, re-discover and retry once."""
    url = _hw_url(account)
    nrst = _HW_NRST_TEMPLATE.format(acct=account, prop=HW_PROPERTY_ID)
    for attempt in ("cached", "fresh"):
        action_id = _hw_action_ids(account, refresh=(attempt == "fresh"))[which]
        headers = {"Content-Type": "text/plain;charset=UTF-8", "Accept": "text/x-component",
                   "Origin": "https://octopusenergy.nz", "Referer": url,
                   "next-action": action_id, "next-router-state-tree": nrst}
        req = urllib.request.Request(url, headers=headers, data=json.dumps(payload).encode())
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code == 404 and attempt == "cached":   # id rotated by an Octopus deploy
                continue
            raise
        # RSC stream: the "1:" line holds the action's return value
        for ln in raw.splitlines():
            if ln.startswith("1:"):
                return json.loads(ln[2:])
        return None


HW_LOG = DATA_DIR / "hotwater-log.jsonl"


def _hw_log(level):
    # Append the active level so per-setting efficiency can be compared over time. One row per day,
    # plus a row whenever the level changes — lets the dashboard validate a switch's real effect.
    if not level:
        return
    today = datetime.date.today().isoformat()
    try:
        lines = HW_LOG.read_text().splitlines() if HW_LOG.exists() else []
        if lines:
            last = json.loads(lines[-1])
            if last.get("date") == today and last.get("level") == level:
                return
        with HW_LOG.open("a") as f:
            f.write(json.dumps({"date": today,
                                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                                "level": level}) + "\n")
    except Exception:
        pass


def hotwater_status():
    cfg = json.loads(OCTO_CFG_PATH.read_text())
    acct, icp = cfg["account"], cfg["icp"]
    data = _hw_action(acct, "get", [{"accountNumber": acct, "icpNumber": icp}]) or {}
    active = data.get("active") or {}
    eff = active.get("effective_from") or ""
    if eff.startswith("$D"):
        eff = eff[2:]
    _hw_log(active.get("level"))
    return {"level": active.get("level"), "effective_from": eff, "levels": list(HW_LEVELS)}


def hotwater_set(level):
    level = (level or "").upper()
    if level not in HW_LEVELS:
        raise ValueError(f"invalid level {level!r} (expected one of {HW_LEVELS})")
    cfg = json.loads(OCTO_CFG_PATH.read_text())
    acct, icp = cfg["account"], cfg["icp"]
    res = _hw_action(acct, "set", [{"accountNumber": acct, "icpNumber": icp, "level": level}])
    if not (res and res.get("success")):
        raise RuntimeError(f"Octopus rejected the change: {res}")
    return {"success": True, "level": level}


# ── Hot water schedule (date-range overrides; enforced by hw_enforce_loop below;
#    a copy is still pushed to HA /config/scripts for the legacy HA-side automation) ──
HW_SCHED_FILE = DATA_DIR / "hotwater-schedule.json"


def hw_schedule_load():
    try:
        d = json.loads(HW_SCHED_FILE.read_text())
    except Exception:
        d = {}
    return {"baseline": d.get("baseline", "SMART"), "schedules": d.get("schedules", [])}


# HA Green enforces the hot-water schedule from its own copy under /config/scripts/. Running as an
# add-on we have that dir mounted (HA_HOTWATER_SCHED_PATH) and write it directly. Standalone on the
# Mac, HA→Mac is firewalled, so we push over Samba instead (.ha-samba holds the SMB URL to /config).
HA_SCHED_PATH  = os.environ.get("HA_HOTWATER_SCHED_PATH")
HW_SAMBA_CREDS = Path(__file__).parent / ".ha-samba"


def _push_schedule_to_ha(data):
    # Best-effort, so a sync hiccup never blocks the dashboard. HA falls back to baseline.
    if HA_SCHED_PATH:
        try:
            p = Path(HA_SCHED_PATH)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2))
        except Exception as e:
            sys.stderr.write(f"[hotwater] HA schedule write failed: {e}\n")
        return
    if not HW_SAMBA_CREDS.exists():
        return
    share = HW_SAMBA_CREDS.read_text().strip()
    # Always a fresh, unique mount — reusing a mount point leaves stale handles whose writes
    # silently don't persist to the server.
    mnt = tempfile.mkdtemp(prefix="energy-ha-")
    try:
        subprocess.run(["mount_smbfs", share, mnt], timeout=15, check=True, capture_output=True)
        Path(mnt, "scripts", "hotwater_schedule.json").write_text(json.dumps(data, indent=2))
    except Exception as e:
        sys.stderr.write(f"[hotwater] HA schedule sync failed: {e}\n")
    finally:
        subprocess.run(["umount", mnt], timeout=10, capture_output=True)
        try:
            os.rmdir(mnt)
        except Exception:
            pass


def hw_schedule_save(data):
    out = {"baseline": (data.get("baseline") or "SMART").upper(),
           "schedules": [{"id": s.get("id") or str(int(time.time() * 1000)),
                          "level": s["level"].upper(), "from": s["from"], "to": s["to"],
                          "note": s.get("note", "")}
                         for s in (data.get("schedules") or [])
                         if (s.get("level") or "").upper() in HW_LEVELS and s.get("from") and s.get("to")]}
    HW_SCHED_FILE.write_text(json.dumps(out, indent=2))
    threading.Thread(target=_push_schedule_to_ha, args=(out,), daemon=True).start()
    # Apply immediately if the save changed today's level (edge-triggered, so a no-op otherwise).
    threading.Thread(target=hw_enforce_tick, daemon=True).start()
    return out


def hw_active_level(date_str=None):
    date_str = date_str or datetime.date.today().isoformat()
    sched = hw_schedule_load()
    for s in sorted(sched["schedules"], key=lambda x: x.get("from", "")):
        if s["from"] <= date_str <= s["to"]:
            return {"level": s["level"], "source": "schedule", "until": s["to"], "note": s.get("note", "")}
    return {"level": sched["baseline"], "source": "baseline"}


# The proxy itself enforces the schedule (it runs 24/7 on the Green). Edge-triggered: Octopus is
# only touched when the scheduled level CHANGES (override starts/ends, baseline edited), so a level
# set manually in the dashboard or the Octopus app sticks until the next schedule boundary.
HW_ENFORCE_STATE = DATA_DIR / "hotwater-enforced.json"
HW_ENFORCE_EVERY_S = 15 * 60


def hw_enforce_tick(force=False):
    desired = hw_active_level()["level"]
    try:
        last = json.loads(HW_ENFORCE_STATE.read_text()).get("level")
    except Exception:
        last = None
    if desired == last and not force:
        return
    try:
        hotwater_set(desired)
        HW_ENFORCE_STATE.write_text(json.dumps(
            {"level": desired, "ts": datetime.datetime.now().isoformat(timespec="seconds")}))
        sys.stderr.write(f"[hotwater] schedule enforced: {last} -> {desired}\n")
    except Exception as e:
        sys.stderr.write(f"[hotwater] schedule enforcement failed ({desired}): {e}\n")


def hw_enforce_loop():
    while True:
        try:
            hw_enforce_tick()
        except Exception as e:
            sys.stderr.write(f"[hotwater] enforce loop error: {e}\n")
        time.sleep(HW_ENFORCE_EVERY_S)


# ── AI insights ─────────────────────────────────────────────────────────────────

ANTHROPIC_KEY_FILE = Path.home() / ".config/anthropic/key"
ANTHROPIC_MODEL = "claude-opus-4-7"
INSIGHTS_CACHE_FILE = DATA_DIR / "ai-insights-cache.json"

SYSTEM_PROMPT_ENERGY = (
    "You are a practical home-energy coach embedded in Mark's energy dashboard. The home heats "
    "with 5 Tuya smart heaters (lounge, main bedroom, bathroom, back bedroom, study) on a NZ "
    "time-of-use tariff (peak / off-peak / night rates). Heating is about half the power bill. "
    "Given the current usage and tariff below, produce exactly THREE insights that are MANAGEMENT "
    "ACTIONS or BEHAVIOUR CHANGES Mark can take to cut cost or improve comfort efficiency — not "
    "observations, not restatements of the numbers. Each must be specific and tied to the actual "
    "data (e.g. shift a room's heating off peak, drop the highest-consuming room, tighten a "
    "schedule window). Be direct and brief."
)


def anthropic_key():
    try:
        return ANTHROPIC_KEY_FILE.read_text().strip()
    except Exception:
        import os
        return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def energy_context():
    """Compact summary of recent per-room/appliance kWh + tariff for the model."""
    start = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
    lines = []
    try:
        rooms = fetch_heating_energy(start)
        totals = {r: round(sum(h["kwh"] for h in rows), 1) for r, rows in rooms.items()}
        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        lines.append("Heating kWh by room (last 14d): " +
                     ", ".join(f"{r} {v}" for r, v in ranked))
    except Exception as e:
        lines.append(f"Heating data unavailable: {e}")
    try:
        apps = fetch_appliance_energy(start)
        at = {k: round(sum(h["kwh"] for h in rows), 1) for k, rows in apps.items()}
        lines.append("Appliance kWh (last 14d): " + ", ".join(f"{k} {v}" for k, v in at.items()))
    except Exception:
        pass
    try:
        r = fetch_tariff()
        lines.append(f"Tariff $/kWh — peak {r.get('peak')}, off-peak {r.get('offpeak')}, "
                     f"night {r.get('night')}; daily {r.get('daily')}")
    except Exception:
        pass
    try:
        lines.append("Heating schedule: " +
                     "; ".join(f"{w['label']} {w['on']}-{w['off']}" for w in get_schedule()))
    except Exception:
        pass
    return "\n".join(lines)


def call_anthropic_insights(context_text, dismissed, feedback=None):
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
        "system": SYSTEM_PROMPT_ENERGY,
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
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    insights = json.loads(text)
    return [{"id": f"i{i}", "title": str(it.get("title", "")).strip(),
             "body": str(it.get("body", "")).strip()} for i, it in enumerate(insights[:3])]


# ── Background logger ──────────────────────────────────────────────────────────

LOG_INTERVAL = 300

def log_state(rooms):
    entry = {"ts": datetime.datetime.now().isoformat(timespec="minutes"), "rooms": rooms}
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def background_logger():
    while True:
        try:
            log_state(fetch_room_states())
        except Exception as e:
            print(f"  logger error: {e}")
        time.sleep(LOG_INTERVAL)


# ── HTTP handler ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {args[1]}  {self.path}")

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_OPTIONS(self):
        self.send_response(200)
        self.cors()
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = DASHBOARD.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/schedule":
            self.send_json({"ok": True, "schedule": get_schedule(), "status": schedule_status()})

        elif self.path.startswith("/heating-energy"):
            try:
                params = parse_qs(urlparse(self.path).query)
                start = params.get("start", [None])[0] or \
                    (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
                self.send_json({"ok": True, "rooms": fetch_heating_energy(start)})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif self.path.startswith("/appliance-energy"):
            try:
                params = parse_qs(urlparse(self.path).query)
                start = params.get("start", [None])[0] or \
                    (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
                self.send_json({"ok": True,
                                "appliances": fetch_appliance_energy(start),
                                "labels": dict(APPLIANCES)})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif self.path == "/appliance-power":
            try:
                self.send_json({"ok": True, "power": fetch_appliance_power()})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif self.path.startswith("/octopus"):
            try:
                params = parse_qs(urlparse(self.path).query)
                days = int(params.get("days", ["30"])[0])
                body = fetch_octopus_csv(min(days, 365))
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.cors()
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif self.path == "/tariff":
            try:
                self.send_json({"ok": True, "rates": fetch_tariff()})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif self.path == "/bill":
            try:
                self.send_json({"ok": True, **fetch_billing()})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif self.path == "/hotwater":
            try:
                self.send_json({"ok": True, **hotwater_status()})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif self.path == "/hotwater-schedule":
            try:
                self.send_json({"ok": True, **hw_schedule_load(), "active": hw_active_level()})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif self.path.startswith("/context-stats"):
            try:
                params = parse_qs(urlparse(self.path).query)
                start = params.get("start", [None])[0] or \
                    (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
                self.send_json({"ok": True, **fetch_context_stats(start)})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif self.path == "/heating-log":
            try:
                entries = []
                if LOG_PATH.exists():
                    for line in LOG_PATH.read_text().splitlines()[-288:]:  # last 24h (5-min intervals)
                        try: entries.append(json.loads(line))
                        except: pass
                self.send_json({"ok": True, "entries": entries})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/schedule":
            data = self.read_body()
            windows = data.get("schedule", [])
            if not windows:
                self.send_json({"ok": False, "error": "no schedule provided"}, 400)
                return
            try:
                # Schedules are managed by Home Assistant — saved for display only.
                save_schedule(windows)
                self.send_json({"ok": True, "schedule": windows,
                                "note": "display only — schedules are managed by Home Assistant"})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif path == "/hotwater":
            try:
                body = self.read_body()
                self.send_json({"ok": True, **hotwater_set(body.get("level"))})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif path == "/hotwater-schedule":
            try:
                saved = hw_schedule_save(self.read_body())
                self.send_json({"ok": True, **saved, "active": hw_active_level()})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif path == "/ai-insights":
            try:
                body = self.read_body()
                dismissed = body.get("dismissed") or []
                feedback = body.get("feedback") or []
                insights = call_anthropic_insights(energy_context(), dismissed, feedback)
                try:
                    INSIGHTS_CACHE_FILE.write_text(json.dumps(
                        {"at": datetime.datetime.now().isoformat(), "insights": insights}))
                except Exception:
                    pass
                self.send_json({"insights": insights})
            except Exception as e:
                try:
                    cached = json.loads(INSIGHTS_CACHE_FILE.read_text()).get("insights", [])
                except Exception:
                    cached = []
                self.send_json({"insights": cached, "error": str(e)}, 200 if cached else 500)

        elif path == "/backup":
            try:
                body = self.read_body()
                backup_dir = Path(os.environ.get(
                    "ENERGY_BACKUP_DIR",
                    Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Backups/energy"))
                backup_dir.mkdir(parents=True, exist_ok=True)
                date_str = datetime.date.today().isoformat()
                out = backup_dir / f"energy-{date_str}.json"
                out.write_text(json.dumps(body))
                # Mirror source files into <backup_dir>/code/ — overwrites latest each time.
                code_dir = backup_dir / "code"
                code_dir.mkdir(exist_ok=True)
                for src in (DASHBOARD, Path(__file__)):
                    if src.exists():
                        shutil.copy2(src, code_dir / src.name)
                self.send_json({"saved": str(out)})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    threading.Thread(target=background_logger, daemon=True).start()
    threading.Thread(target=hw_enforce_loop, daemon=True).start()
    threading.Thread(target=octo_warm_loop, daemon=True).start()
    host = "0.0.0.0" if ADDON else "localhost"     # add-on: reachable on the LAN + via HA ingress
    server = HTTPServer((host, PORT), Handler)
    url    = f"http://{host}:{PORT}"
    print(f"\n  Energy Dashboard → {url}\n")
    if not ADDON and "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
