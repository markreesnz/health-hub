#!/usr/bin/env python3
"""
Health app service — the always-on hub for Mark's health dashboard.

Designed to run on the HA Green (anything-always-on lives there), but it is
pure-stdlib so it also runs locally on the Mac for development.

Responsibilities:
  * Serve index.html (the single-file browser app).
  * Receive Apple Health + Oura data pushed by Health Auto Export  (POST /ingest).
  * Expose the day's metrics, split by source            (GET  /metrics?date=).
  * Compute today's / any day's training session from the
    Living Program block                                 (GET  /workout?date=).
  * Persist entry history + the rolling AI summary        (GET/POST /state).
  * Accept daily localStorage backups                     (POST /backup).
  * Push the day's workout to the iPhone via Home Assistant.

CLI:
  python3 server.py            # run the HTTP server
  python3 server.py notify     # send today's workout to the iPhone (for cron)
  python3 server.py workout    # print today's workout (debug)
"""

import glob
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import threading
import time
import urllib.request
import urllib.error
import uuid
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
BACKUPS = os.path.join(HERE, "backups")
STATE_FILE = os.path.join(DATA, "state.json")
METRICS_FILE = os.path.join(DATA, "metrics.json")   # latest ingested Health Auto Export payload(s)
os.makedirs(DATA, exist_ok=True)
os.makedirs(BACKUPS, exist_ok=True)


def load_config():
    with open(os.path.join(HERE, "config.json")) as f:
        cfg = json.load(f)  # tolerate the _comment_* keys; they're ignored
    # When running as a Home Assistant add-on, overlay the add-on options.
    opts = "/data/options.json"
    if os.path.exists(opts):
        with open(opts) as f:
            cfg.update({k: v for k, v in json.load(f).items() if v not in ("", None)})
    return cfg


CONFIG = load_config()

# Secrets are read at runtime, never written to disk.
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "") or CONFIG.get("anthropic_api_key", "")
DEFAULT_MODEL = CONFIG.get("default_model", "claude-sonnet-4-6")

# Weekly cross-app digest (health + energy + money email) — runs on the Green because the
# Mac sits on a different subnet and could never reach the apps' LAN endpoints.
import digest as weekly_digest
import processing_risk
import zoe_rules
weekly_digest.configure(CONFIG, ANTHROPIC_KEY)


def ha_token() -> str:
    """HA token. As an add-on, the Supervisor injects SUPERVISOR_TOKEN (preferred).
    Otherwise: inline config, then the shared token file."""
    sup = os.environ.get("SUPERVISOR_TOKEN")
    if sup:
        return sup
    if CONFIG.get("ha_token"):
        return CONFIG["ha_token"]
    path = os.path.expanduser(CONFIG.get("ha_token_file", ""))
    if path and os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return ""


def ha_bases() -> list:
    """HA API base URLs. As an add-on, reach core via the Supervisor proxy;
    otherwise try the remote (Nabu Casa) URL then the LAN URL."""
    if os.environ.get("SUPERVISOR_TOKEN"):
        return ["http://supervisor/core"]
    return [u.rstrip("/") for u in (CONFIG.get("ha_url"), CONFIG.get("ha_url_local")) if u]


# --------------------------------------------------------------------------- #
#  Training program — the "Living Program" Block 1 (mirrors the Notion page).
#  Schedule is deterministic: weekday + week-in-block fully determine the day.
# --------------------------------------------------------------------------- #
PROGRAM = {
    # weekday() -> session template. Strings with {reps},{wcap},{tue},{sat} get
    # filled from the per-week table below.
    0: {  # Monday
        "focus": "Strength A — Hinge + Push",
        "type": "strength",
        "summary": "Hinge Lift + Incline Bench @ {reps} reps + chassis finisher",
        "detail": (
            "Warm-up ~8-10 min (general cardio + WGS, band pull-aparts; "
            "specific: Hinge Lift + Scotty Bob).\n"
            "Strength 1 — 5 rounds: Hinge Lift @ {reps} reps "
            "(Wk1 build to Hard-But-Doable; later R1 50% / R2 75% / R3 85% / R4-5 finishing) "
            "+ 3-6 Chin-Ups + Toe-Touch Complex.\n"
            "Strength 2 — 5 rounds: Incline Bench @ {reps} reps + 2-5 Pull-Ups + hip-flexor stretch.\n"
            "Finisher — 5 rounds: 8 Sandbag Get-Up @ 18/27kg + 10 Hanging Leg Raise.\n"
            "Cool-down: foam roll back + 30s dead hang."
        ),
    },
    1: {  # Tuesday
        "focus": "Endurance — Z2",
        "type": "endurance",
        "summary": "Zone 2 {tue} min · HR 125-135",
        "detail": (
            "Zone 2 — run / row / bike / spin / ruck @ 20kg / hike. Conversational, HR 125-135.\n"
            "Duration: {tue} min.\n"
            "Optional finisher: 4-6 x 20s strides / hill bursts, full recovery.\n"
            "Cool-down: 5 min easy + calf/hip stretch."
        ),
    },
    2: {  # Wednesday
        "focus": "REST",
        "type": "rest",
        "summary": "REST — walk / mobility only (non-negotiable)",
        "detail": "Full rest day. Walk and mobility only. Wed rest is non-negotiable.",
    },
    3: {  # Thursday
        "focus": "Strength B — Squat + Pull",
        "type": "strength",
        "summary": "Box Squat 6x4 (+load) + Pull-Ups + sandbag grind",
        "detail": (
            "Warm-up ~8-10 min (general cardio + WGS, Cossack, Spiderman, band pull-aparts; "
            "specific: Box Squat light + Scotty Bob).\n"
            "Strength — 6 rounds: 4 Box Squat (build fast to Hard-But-Doable by round 4; hold 4-6) "
            "+ 3-5 Strict Pull-Up + 3rd-World Squat stretch. Add LOAD each week, not reps.\n"
            "Finisher — 4-round sandbag grind: 10 Sit-Up + 10 Good Morning + 5 Cross-Clean/side @ 18/27kg.\n"
            "Carry burnout: 2 x 40m Farmer Carry + 20m Bear-Hug Walk.\n"
            "Cool-down: foam roll legs+back + Pigeon 60s/side."
        ),
    },
    4: {  # Friday
        "focus": "Work Capacity — grind",
        "type": "conditioning",
        "summary": "{wcap}-min grind + 9-min KB finisher",
        "detail": (
            "Warm-up — 3 rounds: 10 Air Squat + 10 Push-Up + 5 Prone-to-Sprint + stretch.\n"
            "Main grind — for time, {wcap} min steady (not frantic): "
            "4 Sandbag Cross-Clean + 4 Clean & Press + 20 Step-Ups @ 12-20in, all @ 18/27kg.\n"
            "Finisher — 9-min grind: 15 KB Swing @ 12/16kg + 3 Prone-to-Sprint + walk back.\n"
            "Cool-down: hip-flexor stretch + foam roll legs."
        ),
    },
    5: {  # Saturday
        "focus": "Endurance — long",
        "type": "endurance",
        "summary": "Long Zone 2 / ruck {sat} min · HR 125-135",
        "detail": (
            "Zone 2 long — run / row / bike / ruck @ 20kg / hike. Conversational, HR 125-135.\n"
            "Duration: {sat} min. Saturday = longer or ruck.\n"
            "Cool-down: 5 min easy + calf/hip stretch."
        ),
    },
    6: {  # Sunday
        "focus": "REST",
        "type": "rest",
        "summary": "REST",
        "detail": "Full rest day. Sun rest is non-negotiable.",
    },
}

# Per-week progression (Block 1). Week clamped to 1..4.
WEEK_PARAMS = {
    1: {"reps": 5, "wcap": 20, "tue": 45, "sat": 60},
    2: {"reps": 6, "wcap": 24, "tue": 45, "sat": 60},
    3: {"reps": 7, "wcap": 28, "tue": 50, "sat": 70},
    4: {"reps": 8, "wcap": 30, "tue": 50, "sat": 70},
}


# Order of *training* sessions within a week (skip Wed/Sun rest — they aren't cards
# you tick "done" on). Set INCLUDE_REST=True to make rest days part of the queue.
TRAINING_WEEKDAYS = [0, 1, 3, 4, 5]   # Mon, Tue, Thu, Fri, Sat
INCLUDE_REST = False
WD_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# The dicts above are a built-in fallback; program.json (if present) is the source
# of truth — the full plan plus the data needed to regenerate a block.
BLOCK_NAME = CONFIG.get("block_name", "")
BLOCK_START = CONFIG.get("block_start", "")
NOTION_URL = CONFIG.get("notion_program_url", "")
BLOCK_WEEKS = 4
PROGRAM_DATA = {}


def load_program():
    global PROGRAM, WEEK_PARAMS, TRAINING_WEEKDAYS, INCLUDE_REST
    global BLOCK_NAME, BLOCK_START, NOTION_URL, BLOCK_WEEKS, PROGRAM_DATA
    path = os.path.join(HERE, "program.json")
    if not os.path.exists(path):
        return
    with open(path) as f:
        PROGRAM_DATA = json.load(f)
    d, b = PROGRAM_DATA.get("design", {}), PROGRAM_DATA.get("block", {})
    PROGRAM = {WD_NAMES.index(k): v for k, v in b.get("sessions", {}).items()}
    WEEK_PARAMS = {int(k): v for k, v in b.get("week_params", {}).items()}
    TRAINING_WEEKDAYS = [WD_NAMES.index(x) for x in d.get("training_weekdays", TRAINING_WEEKDAYS)]
    INCLUDE_REST = d.get("include_rest_in_queue", INCLUDE_REST)
    BLOCK_WEEKS = d.get("block_weeks", 4)
    BLOCK_NAME = b.get("name", BLOCK_NAME)
    BLOCK_START = b.get("start", BLOCK_START)
    NOTION_URL = b.get("notion_url") or PROGRAM_DATA.get("notion_url", NOTION_URL)


load_program()


def build_sequence() -> list:
    """Flatten the block into an ordered list of sessions (the workout queue)."""
    weekdays = list(range(7)) if INCLUDE_REST else TRAINING_WEEKDAYS
    seq = []
    for week in range(1, BLOCK_WEEKS + 1):
        params = WEEK_PARAMS[week]
        for wd in weekdays:
            tmpl = PROGRAM[wd]
            detail = tmpl["detail"].format(**params)
            # bodyweight variant per session: prefer a precomputed one (generated blocks),
            # else derive it on the fly (works for the hand-tuned block too). None for rest.
            bw_t = tmpl.get("detail_bw")
            sub = globals().get("BW_SUB")
            summary = tmpl["summary"].format(**params)
            if bw_t:
                detail_bw = bw_t.format(**params)
            elif tmpl.get("type") in ("rest", "endurance") or not sub:
                detail_bw = None     # bodyweight swap only meaningful for strength/conditioning
            else:
                detail_bw = sub(detail)
            summary_bw = sub(summary) if (detail_bw and sub) else None
            seq.append({
                "index": len(seq),
                "week": week,
                "weekday": WD_NAMES[wd],
                "block": BLOCK_NAME,
                "focus": tmpl["focus"],
                "type": tmpl["type"],
                "summary": summary,
                "summary_bw": summary_bw if summary_bw != summary else None,
                "detail": detail,
                "detail_bw": detail_bw if detail_bw != detail else None,
            })
    return seq


SEQUENCE = build_sequence()


# --------------------------------------------------------------------------- #
#  MTI block generator (12-month macrocycle -> 4-week blocks from the MTI DB).
#  Optional: if the DB/module isn't present the hub still runs, endpoints 503.
# --------------------------------------------------------------------------- #
try:
    import mti_blocks
    MTI_OK = True
    BW_SUB = mti_blocks.bodyweight_substitute
except Exception as e:  # pragma: no cover
    MTI_OK = False
    BW_SUB = None
    print(f"[mti] block generator unavailable: {e}")

SEQUENCE = build_sequence()   # rebuild now that BW_SUB exists (adds per-session detail_bw)


def reload_program(reset_cursor: bool):
    """Re-read program.json after a block is (re)generated and rebuild the queue."""
    global SEQUENCE
    load_program()
    SEQUENCE = build_sequence()
    if reset_cursor:
        st = load_state()
        st["cursor"] = 0
        st["applied_workouts"] = []
        save_state(st)
    push_ha_card()


def generate_block(which: str, payload: dict) -> dict:
    """which: 'next' | 'regenerate' | 'bodyweight'. Returns a summary dict."""
    if not MTI_OK:
        raise RuntimeError("MTI generator unavailable (missing mti/mti.sqlite — run mti_backup.sh)")
    ms = mti_blocks._macro_state()
    cur_idx = ms.get("current_index", -1)
    cur_start = PROGRAM_DATA.get("block", {}).get("start") or mti_blocks._next_monday()
    bw_now = bool(PROGRAM_DATA.get("design", {}).get("bodyweight_mode", False))
    if which == "next":
        idx = cur_idx + 1
        start = payload.get("start") or mti_blocks._next_monday()
        blk = mti_blocks.write_block(idx, start, bodyweight=bool(payload.get("bodyweight", False)))
        reload_program(reset_cursor=True)
    elif which == "regenerate":
        idx = max(0, cur_idx)
        blk = mti_blocks.write_block(idx, cur_start, bodyweight=bool(payload.get("bodyweight", bw_now)),
                                     seed=random.randint(1, 99999))
        reload_program(reset_cursor=True)
    elif which == "bodyweight":
        idx = max(0, cur_idx)                       # same movements (default seed), details swapped
        on = bool(payload.get("on", not bw_now))
        blk = mti_blocks.write_block(idx, cur_start, bodyweight=on)
        reload_program(reset_cursor=False)          # mid-block toggle keeps your place
    else:
        raise ValueError(which)
    return {"ok": True, "block": blk["block"]["name"], "start": blk["block"]["start"],
            "emphasis": blk["block"]["emphasis"],
            "bodyweight": blk["design"]["bodyweight_mode"], "current": current_session()}


def current_session() -> dict:
    """The session the queue is currently pointing at (advances only on 'done')."""
    st = load_state()
    i = st.get("cursor", 0)
    total = len(SEQUENCE)
    if i >= total:
        return {
            "done_block": True, "index": i, "position": total, "total": total,
            "week": BLOCK_WEEKS, "weekday": "", "block": BLOCK_NAME,
            "focus": "Block complete 🎉", "type": "rest",
            "summary": "Block complete — re-plan the next block, then reset the cursor.",
            "detail": "", "notion_url": NOTION_URL,
        }
    s = dict(SEQUENCE[i])
    s.update({"position": i + 1, "total": total, "done_block": False})
    return s


def advance_session(notes=None, workout=None, weights=None) -> dict:
    """Mark the current session done, move the cursor to the next, refresh the card.
    `workout` (if from a recorded Apple workout) is stored so the plan shows what
    matched it. `weights` holds what Mark logged in the note ({movements,rpe,notes})."""
    st = load_state()
    i = st.get("cursor", 0)
    if i < len(SEQUENCE):
        s = SEQUENCE[i]
        st.setdefault("log", []).append({
            "index": i, "focus": s["focus"], "week": s["week"],
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "notes": notes,
            "workout": workout,   # {type, date} when matched from a recorded workout
            "weights": weights,   # {movements:{name:val}, rpe, notes} from the note
        })
        st["cursor"] = i + 1
        save_state(st)
    push_ha_card()
    return current_session()


# --------------------------------------------------------------------------- #
#  Recorded workouts (Health Auto Export AutoSync) → auto-advance the plan.
#  The .hae files are binary, but the filename = <type>_<YYYYMMDD>_<uuid>.hae,
#  which gives type + date — enough to match to the plan by category.
# --------------------------------------------------------------------------- #
WORKOUT_DIR = os.path.expanduser(
    "~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/AutoSync/Workouts")
WORKOUTS_FILE = os.path.join(DATA, "workouts.json")
KJ_PER_KCAL = 4.184


def _qty(v):
    """Pull a numeric quantity from a HAE field that's either a number or {qty: n}."""
    if isinstance(v, dict):
        v = v.get("qty")
    return v if isinstance(v, (int, float)) else None


def workout_category(name: str) -> str:
    """Map a workout type/name (snake_case OR human 'Functional Strength Training') to a plan category."""
    n = (name or "").lower().replace("_", " ")
    # Apple ("Traditional Strength Training") and Oura ("strengthTraining", "weightLifting",
    # "functionalTraining", "crossTraining") name gym work differently.
    if any(w in n for w in ("strength", "core training", "weight", "lifting", "resistance", "functional",
                            "crossfit", "cross training", "crosstraining")):
        return "strength"
    if any(w in n for w in ("interval", "hiit")):
        return "conditioning"
    if any(w in n for w in ("cycl", "walk", "run", "hik", "row", "elliptical", "swim", "ruck")):
        return "endurance"
    return "other"


def _load_workouts_file() -> dict:
    if os.path.exists(WORKOUTS_FILE):
        with open(WORKOUTS_FILE) as f:
            return json.load(f)
    return {}


def _save_workouts_file(store: dict):
    tmp = f"{WORKOUTS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, WORKOUTS_FILE)


APPLE_STATUS_FILE = os.path.join(DATA, "apple_workout_sync.json")


def apple_workout_status() -> dict:
    try:
        with open(APPLE_STATUS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def ingest_workouts(workouts: list) -> int:
    """Merge workouts from a Health Auto Export REST payload (data.workouts[]) into the store."""
    if workouts:                                    # remember the last Apple export, for the Training card
        try:
            with open(APPLE_STATUS_FILE, "w") as f:
                json.dump({"at": _nz_now().strftime("%Y-%m-%d %H:%M"), "received": len(workouts),
                           "latest": max((str(w.get("start") or w.get("startDate") or "")[:16] for w in workouts),
                                         default="")}, f)
        except OSError:
            pass
    store = _load_workouts_file()
    added = 0
    for w in workouts or []:
        name = (w.get("name") or w.get("workoutActivityType") or w.get("type") or "").strip()
        day = _day_of(w.get("start") or w.get("startDate") or w.get("date") or "")
        if not (name and day):
            continue
        wid = w.get("id") or f"{day}_{name.lower().replace(' ', '_')}"
        if wid not in store:
            added += 1
        dur = _qty(w.get("duration"))            # seconds
        hr = _qty(w.get("avgHeartRate"))
        kj = _qty(w.get("activeEnergyBurned")) or _qty(w.get("activeEnergy"))
        start = (w.get("start") or w.get("startDate") or "")
        z2 = _hae_zone2_minutes(w.get("heartRateData"))
        prev = store.get(wid) or {}
        store[wid] = {
            "id": wid, "date": day, "type": name, "category": workout_category(name),
            "time": start[11:16] if len(start) >= 16 else None,   # HH:MM
            "minutes": round(dur / 60) if isinstance(dur, (int, float)) else None,
            "avg_hr": round(hr) if isinstance(hr, (int, float)) else None,
            "calories": round(kj / 4.184) if isinstance(kj, (int, float)) else None,
            "source": "apple", **({"zone2_min": z2} if z2 is not None else {}),
            **{k: v for k, v in prev.items() if k in USER_FIELDS},
        }
    if added:
        _save_workouts_file(store)
    return added


def import_workouts() -> dict:
    """Workout log: workouts arrive via the HA webhook (POST /ingest) and are stored.
    Optionally also scan the local iCloud AutoSync folder when use_icloud_workouts is on."""
    if CONFIG.get("use_icloud_workouts", True) and os.path.isdir(WORKOUT_DIR):
        out = {}
        for fp in glob.glob(os.path.join(WORKOUT_DIR, "*.hae")):
            m = re.match(r"(.+)_(\d{8})_([0-9A-Fa-f-]+)\.hae$", os.path.basename(fp))
            if not m:
                continue
            typ, ymd, uid = m.group(1), m.group(2), m.group(3)
            out[uid] = {"id": uid, "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
                        "type": typ, "category": workout_category(typ)}
        if out:
            # merge with any webhook-ingested workouts rather than clobbering
            merged = _load_workouts_file(); merged.update(out); _save_workouts_file(merged)
            return merged
    return _load_workouts_file()


# --------------------------------------------------------------------------- #
#  Weekly training compliance (Mon–Sun): strength sessions, Zone 2 minutes, HIIT.
#  Sessions come from Health Auto Export, from Oura (which also imports Apple Watch
#  workouts) and from manual logs; overlapping duplicates are merged. Zone 2 minutes
#  are minutes the heart rate actually spent in range when samples exist.
# --------------------------------------------------------------------------- #
TRAINING_FILE = os.path.join(DATA, "training.json")
TRAINING_DEFAULTS = {"strength": 3, "hiit": 1, "zone2_minutes": 180, "zone2_hr": [120, 135]}
KIND_OF_CATEGORY = {"strength": "strength", "conditioning": "hiit", "endurance": "zone2"}
# Fields Mark sets on a session; device resyncs keep them.
USER_FIELDS = ("ignored", "user_category", "user_zone2")


def _effective(w: dict) -> dict:
    """A session with Mark's corrections applied (what it was, and its Zone 2 minutes)."""
    return {**w, "category": w.get("user_category") or w.get("category")} if w.get("user_category") else w


def edit_session(wid: str, kind: str = None, zone2=None) -> dict:
    """Correct a device session: what it was, and/or how many Zone 2 minutes it had."""
    cat = {v: k for k, v in KIND_OF_CATEGORY.items()}
    store = _load_workouts_file()
    w = store.get(wid)
    if not w:
        raise ValueError("no such session")
    if kind is not None:
        if kind not in cat and kind != "other":
            raise ValueError("kind must be strength, hiit, zone2 or other")
        w["user_category"] = cat.get(kind, "other")
    if zone2 is not None:
        z = int(zone2)
        if not 0 <= z <= 600:
            raise ValueError("Zone 2 minutes must be 0–600")
        w["user_zone2"] = z
    _save_workouts_file(store)
    return training_week(w["date"])


def training_settings() -> dict:
    s = dict(TRAINING_DEFAULTS)
    s.update(CONFIG.get("training") or {})
    if os.path.exists(TRAINING_FILE):
        try:
            with open(TRAINING_FILE) as f:
                s.update(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass
    return s


def save_training_settings(fields: dict) -> dict:
    cur = {}
    if os.path.exists(TRAINING_FILE):
        with open(TRAINING_FILE) as f:
            cur = json.load(f)
    for k, hi in (("strength", 14), ("hiit", 14), ("zone2_minutes", 1500)):
        if k in fields:
            v = int(fields[k])
            if not 0 <= v <= hi:
                raise ValueError(f"{k} must be 0–{hi}")
            cur[k] = v
    if "zone2_hr" in fields:
        lo, hi = (int(x) for x in fields["zone2_hr"])
        if not 60 <= lo < hi <= 200:
            raise ValueError("zone2_hr must be [low, high] between 60 and 200")
        cur["zone2_hr"] = [lo, hi]
    with open(TRAINING_FILE, "w") as f:
        json.dump(cur, f, indent=2)
    return training_week()


def zone2_minutes(samples: list, lo: int, hi: int) -> int:
    """[(datetime, bpm)] sorted → minutes in [lo, hi]; each sample covers the gap to the
    next one, capped at 5 minutes so a dropout doesn't count as time in zone."""
    total = 0.0
    for (t1, bpm), (t2, _) in zip(samples, samples[1:]):
        if lo <= bpm <= hi:
            total += min((t2 - t1).total_seconds() / 60, 5)
    return round(total)


def _hae_zone2_minutes(hr_data):
    if not isinstance(hr_data, list) or not hr_data:
        return None
    lo, hi = training_settings()["zone2_hr"]
    pts = []
    for x in hr_data:
        bpm = x.get("Avg") if isinstance(x.get("Avg"), (int, float)) else _qty(x)
        try:
            t = datetime.strptime(str(x.get("date")), "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            continue
        if isinstance(bpm, (int, float)):
            pts.append((t, bpm))
    pts.sort()
    return zone2_minutes(pts, lo, hi) if len(pts) >= 2 else None


def _oura_dt(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def sync_oura_workouts(days: int = 7) -> int:
    """Oura workouts (its own detections plus workouts it imports from Apple Health), with
    average heart rate and Zone 2 minutes from Oura's heart-rate samples."""
    end = _nz_now().date()
    start = end - timedelta(days=days)
    ws = _oura_get("workout", {"start_date": start.isoformat(), "end_date": end.isoformat()})
    samples = []
    try:                                               # heart rate is extra: workouts save without it
        d = start
        while d <= end:                                # heartrate allows ~30 days per call
            e = min(d + timedelta(days=29), end)
            for x in _oura_get("heartrate", {"start_datetime": f"{d.isoformat()}T00:00:00+00:00",
                                             "end_datetime": f"{e.isoformat()}T23:59:59+00:00"}):
                t = _oura_dt(x.get("timestamp"))
                if t and isinstance(x.get("bpm"), (int, float)):
                    samples.append((t, x["bpm"]))
            d = e + timedelta(days=1)
    except Exception as e:
        print(f"[oura] heart rate for workouts unavailable ({e}); saving workouts without Zone 2 detail")
    samples.sort()
    lo, hi = training_settings()["zone2_hr"]
    store = _load_workouts_file()
    for w in ws:
        t0, t1 = _oura_dt(w.get("start_datetime")), _oura_dt(w.get("end_datetime"))
        if not (t0 and t1 and w.get("id")):
            continue
        inside = [p for p in samples if t0 <= p[0] <= t1]
        name = w.get("label") or w.get("activity") or "workout"
        wid = "oura_" + w["id"]
        store[wid] = {"id": wid, "date": w.get("day") or t0.date().isoformat(), "type": name,
                      "category": workout_category(w.get("activity") or name),
                      "time": t0.strftime("%H:%M"), "minutes": round((t1 - t0).total_seconds() / 60),
                      "avg_hr": round(statistics.mean(b for _, b in inside)) if inside else None,
                      "zone2_min": zone2_minutes(inside, lo, hi) if len(inside) >= 2 else None,
                      "intensity": w.get("intensity"), "source": "oura",
                      **{k: v for k, v in (store.get(wid) or {}).items() if k in USER_FIELDS}}
    _save_workouts_file(store)
    _workout_sync_status(found=len(ws), days=days)
    return len(ws)


WORKOUT_STATUS_FILE = os.path.join(DATA, "oura_workout_sync.json")


def _workout_sync_status(found: int = None, days: int = None, error: str = None):
    """Remember the last Oura workout sync, so the Training card can say what happened."""
    st = {"at": _nz_now().strftime("%Y-%m-%d %H:%M"), "error": error[:200] if error else None}
    if error is None:
        st.update({"found": found, "days": days})
    try:
        with open(WORKOUT_STATUS_FILE, "w") as f:
            json.dump(st, f)
    except OSError:
        pass


def workout_sync_status() -> dict:
    try:
        with open(WORKOUT_STATUS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def log_session(kind: str, minutes=None, day: str = None) -> dict:
    """Manual fallback for a session no device recorded."""
    cat = {v: k for k, v in KIND_OF_CATEGORY.items()}.get(kind)
    if not cat:
        raise ValueError("kind must be strength, hiit or zone2")
    day = day or _nz_now().date().isoformat()
    date.fromisoformat(day)
    mins = int(minutes) if minutes not in (None, "") else None
    if kind == "zone2" and not (mins and 0 < mins <= 600):
        raise ValueError("Zone 2 needs minutes (1–600)")
    wid = "manual_" + uuid.uuid4().hex[:10]
    store = _load_workouts_file()
    store[wid] = {"id": wid, "date": day, "type": {"strength": "Strength", "hiit": "HIIT", "zone2": "Zone 2"}[kind],
                  "category": cat, "time": _nz_now().strftime("%H:%M") if day == _nz_now().date().isoformat() else None,
                  "minutes": mins, "source": "manual", **({"zone2_min": mins} if kind == "zone2" else {})}
    _save_workouts_file(store)
    return training_week(day)


def remove_session(wid: str) -> dict:
    """Manual logs are deleted; device sessions are hidden (so a resync doesn't bring them back)."""
    store = _load_workouts_file()
    w = store.get(wid)
    if not w:
        raise ValueError("no such session")
    if w.get("source") == "manual":
        del store[wid]
    else:
        w["ignored"] = True
    _save_workouts_file(store)
    return training_week(w["date"])


def _session_zone2(w: dict, lo: int, hi: int):
    """Zone 2 minutes for one session, and how sure we are."""
    if w.get("user_zone2") is not None:
        return w["user_zone2"], "set by you"
    if w.get("zone2_min") is not None:
        return w["zone2_min"], ("logged" if w.get("source") == "manual" else "heart rate")
    if KIND_OF_CATEGORY.get(w.get("category")) != "zone2" or not w.get("minutes"):
        return 0, None
    if w.get("avg_hr"):                               # no samples: the average has to sit in range
        if lo - 5 <= w["avg_hr"] <= hi + 5:
            return w["minutes"], "average HR"
        return 0, f"average HR {w['avg_hr']} outside {lo}–{hi}"
    return (w["minutes"], "no heart rate") if w["minutes"] >= 20 else (0, None)


def _dedupe_sessions(ws: list) -> list:
    """Apple and Oura both report the same workout: same day and kind, starts within 20 min,
    from *different* sources. Two sessions from the same device are always separate (Oura can
    detect a walk to the rower just before the row itself). The richer record wins, then the
    longer one."""
    def mins(t):
        return int(t[:2]) * 60 + int(t[3:5]) if t else None

    def rank(w):
        return (w.get("zone2_min") is not None, w.get("avg_hr") is not None, w.get("minutes") or 0)
    out = []
    for w in sorted(ws, key=lambda w: (w["date"], w.get("time") or "")):
        dup = next((o for o in out if "manual" not in (w.get("source"), o.get("source"))
                    and o.get("source") != w.get("source")
                    and o["date"] == w["date"] and o.get("category") == w.get("category")
                    and mins(o.get("time")) is not None and mins(w.get("time")) is not None
                    and abs(mins(o["time"]) - mins(w["time"])) <= 20), None)
        if dup is None:
            out.append(w)
        elif rank(w) > rank(dup):
            out[out.index(dup)] = w
    return out


def training_week(day: str = None, store: dict = None, settings: dict = None, today: date = None) -> dict:
    store = _load_workouts_file() if store is None else store
    s = training_settings() if settings is None else settings
    today = today or _nz_now().date()
    d = date.fromisoformat(day) if day else today
    lo, hi = s["zone2_hr"]

    def week(mon):
        sun = mon + timedelta(days=6)
        ws = _dedupe_sessions([_effective(w) for w in store.values() if mon.isoformat() <= w.get("date", "") <= sun.isoformat()
                               and not w.get("ignored")])
        sessions = []
        for w in ws:
            z2, how = _session_zone2(w, lo, hi)
            sessions.append({**w, "kind": KIND_OF_CATEGORY.get(w.get("category"), "other"),
                             "zone2": z2, "zone2_basis": how})
        return {"start": mon.isoformat(), "end": sun.isoformat(), "sessions": sessions,
                "strength": sum(x["kind"] == "strength" for x in sessions),
                "hiit": sum(x["kind"] == "hiit" for x in sessions),
                "zone2_minutes": sum(x["zone2"] for x in sessions)}

    mon = d - timedelta(days=d.weekday())
    cur = week(mon)
    history = []
    for i in range(1, 9):
        w = week(mon - timedelta(weeks=i))
        history.append({k: w[k] for k in ("start", "strength", "hiit", "zone2_minutes")})
    targets = {k: s[k] for k in ("strength", "hiit", "zone2_minutes")}
    return {**cur, "targets": targets, "zone2_hr": s["zone2_hr"], "today": today.isoformat(),
            "oura_sync": workout_sync_status(), "apple_sync": apple_workout_status(),
            "met": {"strength": cur["strength"] >= targets["strength"], "hiit": cur["hiit"] >= targets["hiit"],
                    "zone2": cur["zone2_minutes"] >= targets["zone2_minutes"]},
            "history": history}


# A casual stroll must not complete a Zone 2 session. Endurance matches need to be
# session-length OR show a training heart rate — prescribed Zone 2 is 50-90 min at
# HR 125-135, casual walks run 10-40 min at 70-90 bpm, so either signal separates them.
ENDURANCE_MIN_MINUTES = CONFIG.get("endurance_min_minutes", 45)
ENDURANCE_MIN_HR = CONFIG.get("endurance_min_hr", 105)


def _endurance_quality(w: dict) -> bool:
    mins, hr = w.get("minutes"), w.get("avg_hr")
    if mins is None and hr is None:
        return True   # filename-only (.hae) workouts carry no evidence either way
    return (mins or 0) >= ENDURANCE_MIN_MINUTES or (hr or 0) >= ENDURANCE_MIN_HR


# A cardio workout (row/run/bike/ruck) reports only its modality, so it lands in the
# "endurance" category regardless of whether it was an easy Zone 2 effort or a VO2max
# interval session. But a conditioning (VO2max/HIIT) session is done with those same
# modalities — so let a cardio workout complete a queued conditioning session when it
# shows real training. The bar is intentionally higher than the Zone 2 gate (interval
# sessions push HR up), and unlike endurance a bare filename with no evidence does NOT
# qualify — we won't auto-complete a hard session on no data.
CONDITIONING_MIN_MINUTES = CONFIG.get("conditioning_min_minutes", 20)
CONDITIONING_MIN_HR = CONFIG.get("conditioning_min_hr", 115)


def _conditioning_quality(w: dict) -> bool:
    mins, hr = w.get("minutes"), w.get("avg_hr")
    if mins is None and hr is None:
        return False   # no evidence → don't false-complete a VO2max/HIIT session
    return (hr or 0) >= CONDITIONING_MIN_HR or (mins or 0) >= CONDITIONING_MIN_MINUTES


def _session_match(w: dict, cur_type: str) -> bool:
    """Does this recorded workout satisfy the queued session? Exact category match is the
    base case (endurance also gated by _endurance_quality). A cardio workout additionally
    satisfies a conditioning session when it shows real effort — see _conditioning_quality."""
    cat = w["category"]
    if cat == cur_type:
        return cat != "endurance" or _endurance_quality(w)
    if cat == "endurance" and cur_type == "conditioning":
        return _conditioning_quality(w)
    return False


def apply_workouts() -> int:
    """Advance the queue for each recorded workout (since block_start) that matches
    the current session's category. Idempotent via state['applied_workouts']."""
    wk = import_workouts()
    if not wk or not BLOCK_START:
        return 0
    applied = set(load_state().get("applied_workouts", []))
    items = sorted((w for w in wk.values() if w["date"] >= BLOCK_START), key=lambda w: w["date"])
    advanced, progress = 0, True
    while progress:                      # fixed-point: out-of-order workouts still match later
        progress = False
        for w in items:
            if w["id"] in applied:
                continue
            cur = current_session()
            if cur.get("done_block"):
                break
            if _session_match(w, cur["type"]):
                advance_session(notes=f"auto: {w['type']} on {w['date']}",
                                workout={"type": w["type"], "date": w["date"], "category": w["category"],
                                         "time": w.get("time"), "minutes": w.get("minutes"),
                                         "avg_hr": w.get("avg_hr"), "calories": w.get("calories")})
                applied.add(w["id"])
                advanced += 1
                progress = True
    st = load_state()
    st["applied_workouts"] = sorted(applied)
    save_state(st)
    if advanced:
        print(f"[workouts] auto-advanced {advanced} session(s) from recorded workouts")
    return advanced


def set_cursor(i: int) -> dict:
    st = load_state()
    st["cursor"] = max(0, min(i, len(SEQUENCE)))
    st["applied_workouts"] = []   # reset workout-sync tracking when cursor is set manually
    save_state(st)
    push_ha_card()
    return current_session()


def workout_text(w: dict = None) -> str:
    """Plain-text rendering of a session — for the card / Notes / Shortcuts."""
    w = w or current_session()
    head = f"🏋️ {w['focus']}"
    meta = f"{w['block']} · Week {w['week']} · Session {w.get('position', '?')} of {w.get('total', '?')}"
    if w["type"] == "rest":
        return f"{head}\n{meta}\n\n{w['summary']}"
    lines = [head, meta, "", w["summary"], "", w["detail"]]
    if w.get("notion_url"):
        lines += ["", f"Full program: {w['notion_url']}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Workout note (Apple Notes) — build, write, and parse logged weights.
#  This is the full loop: the note shows the session + fill-in weight slots;
#  on completion we read what Mark entered, store it on the session in the plan,
#  advance, and write the next session out. read_note() is defined further down.
# --------------------------------------------------------------------------- #
WORKOUT_NOTE_TITLE = CONFIG.get("workout_note_title", "Today’s Workout")
WK_DATE_TAG = "Date: "
_WK_LOAD_RE = re.compile(r"\d+(?:[\-/]\d+)?\s*(?:kg|lb|#)", re.I)
_WK_MOVE_RE = re.compile(r"\d+\s+([A-Z][A-Za-z&\-]*(?:\s+(?:&\s+)?[A-Z][A-Za-z&\-]*)*)")


def workout_movements(detail: str) -> list:
    """Loaded movements (name, prescribed load) from a session's prose detail.
    Clause-based so 'A + B + C, all @ 18/27kg' assigns the load to each."""
    seen, out = set(), []
    for clause in re.split(r"[.\n;]", detail or ""):
        loads = list(_WK_LOAD_RE.finditer(clause))
        if not loads:
            continue
        last = loads[-1]
        load = re.sub(r"\s+", "", last.group(0))
        cutoff = len(clause) if "all" in clause.lower() else last.start()
        for m in _WK_MOVE_RE.finditer(clause):
            if m.start() > cutoff:
                continue
            name = m.group(1).strip()
            if name.lower() in seen or len(name) > 40:
                continue
            seen.add(name.lower())
            out.append((name, load))
    return out


def workout_note_body(w: dict = None, day: str = None) -> str:
    """Note body: title line (keeps the note named), session, then a fill-in
    WEIGHTS USED block whose format parse_note_weights() reads back."""
    w = w or current_session()
    day = day or date.today().isoformat()
    lines = [WORKOUT_NOTE_TITLE, "", workout_text(w), "",
             f"{WK_DATE_TAG}{day}", "", "———————————————",
             "✏️ WEIGHTS USED (tap to fill in)"]
    movs = workout_movements(w.get("detail", ""))
    if movs:
        lines += [f"• {name} (prescribed {load}):  ____" for name, load in movs]
    else:
        lines += ["• ____________:  ____"] * 3
    lines += ["", "RPE (1-10):  ____", "Notes:  "]
    return "\n".join(lines)


def write_note_body(name: str, text: str):
    """Write/overwrite an Apple Note by title (global lookup, Mac only)."""
    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')
    body = esc(text.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;").replace("\n", "<br>"))
    script = ('tell application "Notes"\n'
              f'set theTitle to "{esc(name)}"\n'
              f'set theBody to "{body}"\n'
              'if (exists note theTitle) then\n'
              'set body of note theTitle to theBody\n'
              'else\n'
              'make new note with properties {name:theTitle, body:theBody}\n'
              'end if\nend tell')
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.SubprocessError):
        pass


def write_workout_note(w: dict = None, day: str = None):
    body = workout_note_body(w, day)
    if NOTION_WORKOUT_PAGE:
        notion_write_text(NOTION_WORKOUT_PAGE, body)
    else:
        write_note_body(WORKOUT_NOTE_TITLE, body)


def read_workout_note() -> str:
    if NOTION_WORKOUT_PAGE:
        return notion_page_text(NOTION_WORKOUT_PAGE)
    return read_note(WORKOUT_NOTE_TITLE)


def parse_note_weights(text: str) -> dict:
    """Pull the weights / RPE / notes Mark filled into the note's WEIGHTS block."""
    def filled(v):
        v = (v or "").strip()
        return v if (v and set(v) != {"_"}) else None
    movements, rpe, notes = {}, None, None
    for line in (text or "").splitlines():
        line = line.strip()
        m = re.match(r"[•·\-*]\s*(.+?)\s*\(prescribed[^)]*\):\s*(.*)$", line)
        if m:
            val = filled(m.group(2))
            if val:
                movements[m.group(1).strip()] = val
            continue
        mr = re.match(r"RPE[^:]*:\s*(.*)$", line, re.I)
        if mr:
            rpe = filled(mr.group(1)) or rpe
            continue
        mn = re.match(r"Notes?:\s*(.*)$", line, re.I)
        if mn and (mn.group(1) or "").strip():
            notes = mn.group(1).strip()
    return {"movements": movements, "rpe": rpe, "notes": notes}


# --- In-app weight logging (log sets as you complete them, no round-trip via the note) ------ #
# Feeds the SAME weights model the note loop uses ({movements:{name:val}, rpe, notes}), stored as
# a draft on the plan state keyed to the current cursor so it survives reloads and flows into
# complete_workout. Per-movement value is the set list joined "60/62.5/65".
_WK_SETS_RE = re.compile(r"(\d+)\s*(?:rounds?|sets?|x\b)", re.I)


def loggable_movements(detail: str) -> list:
    """Movements Mark should record a working weight for: the main strength/hypertrophy lifts and
    the power move (which carry NO prescribed load in the prose — he picks the weight), plus any
    explicitly-loaded finisher movements. Matched by line role so finisher prose isn't mis-parsed."""
    out, seen = [], set()
    def add(name, load=""):
        name = re.sub(r"\s+", " ", name).strip(" .+")
        if not name or name.lower() in seen or len(name) > 40:
            return
        seen.add(name.lower())
        out.append({"name": name, "prescribed": load})
    for line in (detail or "").splitlines():
        s = line.strip(); low = s.lower()
        if low.startswith("strength"):
            m = re.search(r"rounds?:\s*(.+?)\s*@", s, re.I)
            if m: add(m.group(1))
        elif low.startswith("hypertrophy"):
            m = re.search(r"x\s*[\d\-]+\s+(.+?)\s*@", s, re.I)
            if m: add(m.group(1))
        elif low.startswith("power"):
            for m in re.finditer(r"\d+\s*x\s*\d+\s+(.+?)\s*[:(]", s):
                add(m.group(1))
    for name, load in workout_movements(detail):   # loaded finishers (with a prescribed kg)
        add(name, load)
    return out


def workout_set_count(detail: str, wtype: str = None) -> int:
    """Best-guess number of work sets per movement from the prose (e.g. '5 rounds', '6x4')."""
    m = _WK_SETS_RE.search(detail or "")
    if m:
        n = int(m.group(1))
        if 1 <= n <= 10:
            return n
    return 5 if (wtype or "").startswith("strength") else 3


def workout_log_draft() -> dict:
    """The saved in-app log for the CURRENT session, or {} if none/stale."""
    st = load_state()
    draft = st.get("workout_log") or {}
    return draft if draft.get("cursor") == st.get("cursor", 0) else {}


def workout_log_view() -> dict:
    """Current session's loggable movements (prescribed load + any weights already entered),
    for the in-app 'log weights as you go' panel."""
    w = current_session()
    draft = workout_log_draft()
    logged = draft.get("movements") or {}
    movs = []
    for mv in loggable_movements(w.get("detail", "")):
        val = logged.get(mv["name"]) or ""
        movs.append({"name": mv["name"], "prescribed": mv["prescribed"],
                     "sets": [s for s in re.split(r"[/,\s]+", val) if s]})
    return {"cursor": load_state().get("cursor", 0), "focus": w.get("focus"),
            "type": w.get("type"), "done_block": w.get("done_block", False),
            "set_count": workout_set_count(w.get("detail", ""), w.get("type")),
            "movements": movs, "rpe": draft.get("rpe"), "notes": draft.get("notes")}


def save_workout_log(payload: dict) -> dict:
    """Persist the in-app weight log for the current session (auto-saved as Mark logs each set)."""
    st = load_state()
    movements = {k: str(v).strip() for k, v in (payload.get("movements") or {}).items()
                 if v is not None and str(v).strip()}
    rpe = (str(payload.get("rpe")).strip() or None) if payload.get("rpe") is not None else None
    notes = (str(payload.get("notes")).strip() or None) if payload.get("notes") else None
    st["workout_log"] = {"cursor": st.get("cursor", 0), "date": date.today().isoformat(),
                         "movements": movements, "rpe": rpe, "notes": notes}
    save_state(st)
    return {"ok": True, "saved": len(movements)}


def complete_workout(notes: str = None) -> dict:
    """Full cycle: gather the weights Mark logged (in-app draft wins over the Apple Note),
    store them on that session in the plan, advance, and write the next session into the note."""
    logged = parse_note_weights(read_workout_note())
    draft = workout_log_draft()
    if draft:
        # App-logged weights take precedence over anything parsed from the note.
        logged["movements"] = {**logged.get("movements", {}), **(draft.get("movements") or {})}
        logged["rpe"] = draft.get("rpe") or logged.get("rpe")
        if draft.get("notes"):
            logged["notes"] = draft.get("notes")
    has = bool(logged["movements"] or logged["rpe"] or logged["notes"])
    nxt = advance_session(notes=notes, weights=logged if has else None)
    # Clear the now-consumed draft so it doesn't bleed into the next session.
    st = load_state()
    if st.pop("workout_log", None) is not None:
        save_state(st)
    write_workout_note(nxt)
    return {"logged": logged, "next": {"focus": nxt.get("focus"),
            "position": nxt.get("position"), "total": nxt.get("total")}}


# --------------------------------------------------------------------------- #
#  Metrics — parse Health Auto Export payloads, split Oura vs Apple Health.
# --------------------------------------------------------------------------- #
# Health Auto Export metric name -> friendly key we surface to the app.
METRIC_MAP = {
    "heart_rate_variability": "hrv",
    "resting_heart_rate": "resting_hr",
    "step_count": "steps",
    "active_energy": "active_energy",
    "apple_exercise_time": "exercise_minutes",
    "respiratory_rate": "respiratory_rate",
    "blood_oxygen_saturation": "blood_oxygen",
    "weight_body_mass": "weight",
    "body_mass": "weight",
    "sleep_analysis": "sleep",
    "vo2_max": "vo2_max",
    "mindful_minutes": "mindful_minutes",
}


def _load_metrics_store() -> dict:
    if not os.path.exists(METRICS_FILE):
        return {}
    with open(METRICS_FILE) as f:
        raw = f.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # A shared tmp filename let two concurrent writers (e.g. an add-on restart racing the
        # previous process's in-flight Oura sync) interleave, leaving two JSON documents
        # concatenated in the file. Recover the first complete document — the same data a
        # normal write would have produced — and heal the file so this doesn't repeat forever.
        if e.msg == "Extra data":
            try:
                recovered, _ = json.JSONDecoder().raw_decode(raw)
                _save_metrics_store(recovered)
                print(f"[metrics] recovered store from corruption ({e})")
                return recovered
            except json.JSONDecodeError:
                pass
        print(f"[metrics] metrics.json is corrupted and unrecoverable: {e}")
        return {}


def _save_metrics_store(store: dict):
    # Unique per-writer tmp filename: two concurrent writers (see _load_metrics_store) must
    # never share one tmp path, or their writes can interleave before either os.replace() runs.
    tmp = f"{METRICS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, METRICS_FILE)


def ingest_payload(payload: dict):
    """
    Accept a Health Auto Export JSON payload and fold the metrics into the
    per-date store keyed as store[date][friendly_key] = {value, unit, source}.
    Tolerant of shape differences between export configs.
    """
    store = _load_metrics_store()
    data = payload.get("data", payload)
    metrics = data.get("metrics", []) or []
    oura_hint = CONFIG.get("oura_source_hint", "Oura").lower()

    for m in metrics:
        name = (m.get("name") or "").lower()
        key = METRIC_MAP.get(name)
        if not key:
            continue
        unit = m.get("units", "")
        for point in m.get("data", []) or []:
            raw_date = point.get("date") or point.get("sleepStart") or ""
            day = _day_of(raw_date)
            if not day:
                continue
            if key in OURA_ONLY:
                break
            source = point.get("source", "")
            bucket = store.setdefault(day, {})
            # Oura API sync owns these keys on days it has covered — HAE is the fallback only
            existing = bucket.get(key)
            if _device_owned(key, existing):
                continue
            if key == "sleep":
                bucket[key] = {
                    "asleep_hours": _sleep_hours(point),
                    "deep": point.get("deep"),
                    "rem": point.get("rem"),
                    "core": point.get("core"),
                    "awake": point.get("awake"),
                    "source": source,
                }
            else:
                val = point.get("qty", point.get("value"))
                if key == "weight" and not _weight_plausible(val, day):
                    print(f"[ingest] rejected implausible weight {val}kg on {day} (source={source})")
                    continue
                bucket[key] = {
                    "value": val,
                    "unit": unit,
                    "source": source,
                    "from_oura": oura_hint in source.lower(),
                }
    _save_metrics_store(store)

    # Workouts (Health Auto Export REST export) → match to the plan.
    if ingest_workouts(data.get("workouts") or []):
        apply_workouts()


# --------------------------------------------------------------------------- #
#  Oura API (OAuth2, v2). Personal access tokens were retired Dec 2025, so this
#  uses an Oura "application" (client id + secret from the add-on Configuration
#  tab). One-time connect: user opens the authorize URL, approves, then pastes
#  the redirect URL (or just the ?code=) into Settings -> the hub exchanges it.
#  Refresh tokens are SINGLE-USE (rotating) — every refresh must be persisted
#  immediately or the connection is lost.
# --------------------------------------------------------------------------- #
OURA_CLIENT_ID = os.environ.get("OURA_CLIENT_ID", "") or CONFIG.get("oura_client_id", "")
OURA_CLIENT_SECRET = os.environ.get("OURA_CLIENT_SECRET", "") or CONFIG.get("oura_client_secret", "")


def _oura_creds():
    """App credentials — Settings-saved (tokens file) wins, else add-on options/env."""
    tok = _oura_tokens_load()
    return (tok.get("client_id") or OURA_CLIENT_ID,
            tok.get("client_secret") or OURA_CLIENT_SECRET)


# --------------------------------------------------------------------------- #
#  Withings: weight and body fat straight from the scale's cloud (OAuth, like
#  Oura). Once connected, Withings owns `weight` and `body_fat`: Apple Health
#  and Home Assistant values for them stop overwriting it. One reading a day —
#  the first weigh-in, so days compare like with like.
# --------------------------------------------------------------------------- #
WITHINGS_FILE = os.path.join(DATA, "withings.json")
WITHINGS_KEYS = ("weight", "body_fat")
WITHINGS_TYPES = {1: "weight", 6: "body_fat"}        # Withings meastype → metric key
WITHINGS_SYNC_SECS = 1800
_WITHINGS_LOCK = threading.Lock()


def _withings_load() -> dict:
    try:
        with open(WITHINGS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _withings_save(w: dict):
    tmp = f"{WITHINGS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(w, f, indent=2)
    os.replace(tmp, WITHINGS_FILE)


def withings_status() -> dict:
    w = _withings_load()
    configured = bool(w.get("client_id") and w.get("client_secret") and w.get("redirect_uri"))
    out = {"configured": configured, "connected": bool(w.get("refresh_token")) and w.get("error") != "reauthorize",
           "last_sync": w.get("last_sync"), "last_weight": w.get("last_weight"), "error": w.get("error")}
    if configured:
        from urllib.parse import urlencode
        out["authorize_url"] = "https://account.withings.com/oauth2_user/authorize2?" + urlencode({
            "response_type": "code", "client_id": w["client_id"], "scope": "user.metrics",
            "redirect_uri": w["redirect_uri"], "state": "health-hub"})
    return out


def withings_save_creds(client_id: str, client_secret: str, redirect_uri: str) -> dict:
    cid, sec, red = (client_id or "").strip(), (client_secret or "").strip(), (redirect_uri or "").strip()
    if not (cid and sec and red.startswith("https://")):
        raise ValueError("client id, client secret and the https callback URL you registered are all needed")
    with _WITHINGS_LOCK:
        w = _withings_load()
        w.update({"client_id": cid, "client_secret": sec, "redirect_uri": red, "error": None})
        _withings_save(w)
    return withings_status()


def _withings_post(url: str, params: dict, token: str = None) -> dict:
    from urllib.parse import urlencode
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=urlencode(params).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    if data.get("status") != 0:                     # Withings answers 200 with a status code
        raise RuntimeError(f"Withings refused the request (status {data.get('status')}): {data.get('error', '')}"[:200])
    return data.get("body") or {}


def _withings_store_grant(w: dict, body: dict):
    w.update({"access_token": body["access_token"], "refresh_token": body.get("refresh_token") or w.get("refresh_token"),
              "expires_at": time.time() + int(body.get("expires_in") or 10800), "error": None})
    _withings_save(w)


def withings_exchange_code(code_or_url: str) -> dict:
    """The pasted redirect URL (or bare code) → tokens, then a year of history."""
    raw = (code_or_url or "").strip()
    code = raw
    if "code=" in raw:
        from urllib.parse import urlparse, parse_qs
        code = (parse_qs(urlparse(raw).query).get("code") or [""])[0]
    if not code:
        raise ValueError("no authorization code found in that paste")
    with _WITHINGS_LOCK:
        w = _withings_load()
        body = _withings_post("https://wbsapi.withings.net/v2/oauth2", {
            "action": "requesttoken", "grant_type": "authorization_code", "client_id": w["client_id"],
            "client_secret": w["client_secret"], "code": code, "redirect_uri": w["redirect_uri"]})
        _withings_store_grant(w, body)
    threading.Thread(target=lambda: withings_sync(365), daemon=True).start()
    return {"connected": True}


def _withings_token() -> str:
    """A valid access token, refreshed when it's within 5 minutes of expiry. Withings rotates
    the refresh token on every refresh, so the new one is saved straight away."""
    with _WITHINGS_LOCK:
        w = _withings_load()
        if not w.get("refresh_token"):
            raise RuntimeError("Withings not connected")
        if w.get("access_token") and time.time() < w.get("expires_at", 0) - 300:
            return w["access_token"]
        try:
            body = _withings_post("https://wbsapi.withings.net/v2/oauth2", {
                "action": "requesttoken", "grant_type": "refresh_token", "client_id": w["client_id"],
                "client_secret": w["client_secret"], "refresh_token": w["refresh_token"]})
        except RuntimeError:
            w["error"] = "reauthorize"
            _withings_save(w)
            raise
        _withings_store_grant(w, body)
        return w["access_token"]


def withings_daily(groups: list) -> dict:
    """Withings measure groups → {local day: {metric: value}}, keeping each day's first weigh-in."""
    tz = weekly_digest.NZ
    first = {}
    for g in sorted(groups, key=lambda g: g.get("date") or 0):
        if g.get("category") != 1:                       # 2 = user objectives, not measurements
            continue
        when = datetime.fromtimestamp(g["date"], tz) if tz else datetime.fromtimestamp(g["date"])
        day = when.date().isoformat()
        for m in g.get("measures") or []:
            key = WITHINGS_TYPES.get(m.get("type"))
            if key and key not in first.get(day, {}):
                first.setdefault(day, {})[key] = round(m["value"] * 10 ** m["unit"], 2)
                first[day][key + "_time"] = when.strftime("%H:%M")
    return first


def withings_sync(days: int = 14) -> dict:
    token = _withings_token()
    end = int(time.time())
    groups, offset = [], None
    while True:
        params = {"action": "getmeas", "meastypes": "1,6", "category": 1,
                  "startdate": end - days * 86400, "enddate": end}
        if offset:
            params["offset"] = offset
        body = _withings_post("https://wbsapi.withings.net/measure", params, token)
        groups += body.get("measuregrps") or []
        if not body.get("more"):
            break
        offset = body.get("offset")
    daily = withings_daily(groups)
    store = _load_metrics_store()
    for day, vals in daily.items():
        bucket = store.setdefault(day, {})
        if "weight" in vals and _weight_plausible(vals["weight"], day):
            bucket["weight"] = {"value": vals["weight"], "unit": "kg", "source": "Withings", "time": vals["weight_time"]}
        if "body_fat" in vals:
            bucket["body_fat"] = {"value": vals["body_fat"], "unit": "%", "source": "Withings", "time": vals["body_fat_time"]}
    _save_metrics_store(store)
    with _WITHINGS_LOCK:
        w = _withings_load()
        w["last_sync"] = datetime.now().isoformat(timespec="seconds")
        latest = max(daily) if daily else None
        if latest and "weight" in daily[latest]:
            w["last_weight"] = {"date": latest, "kg": daily[latest]["weight"]}
        _withings_save(w)
    return {"days": len(daily), "readings": len(groups)}


def _device_owned(key: str, existing) -> bool:
    """Values a connected device owns, which Apple Health / HA must not overwrite."""
    if not isinstance(existing, dict):
        return False
    return ((key in OURA_KEYS and existing.get("source") == "Oura API")
            or (key in WITHINGS_KEYS and existing.get("source") == "Withings"))


def _withings_loop():
    while True:
        time.sleep(WITHINGS_SYNC_SECS)
        if withings_status()["connected"]:
            try:
                withings_sync(7)
            except Exception as e:
                print(f"[withings] sync failed: {e}")


def oura_save_creds(client_id: str, client_secret: str) -> dict:
    cid, sec = (client_id or "").strip(), (client_secret or "").strip()
    if not cid or not sec:
        raise RuntimeError("both client id and client secret are required")
    tok = _oura_tokens_load()
    tok["client_id"], tok["client_secret"] = cid, sec
    tok.pop("error", None)
    _oura_tokens_save(tok)
    return oura_status()
# If unset, redirect_uri is OMITTED from both the authorize URL and the token exchange —
# Oura then uses whichever redirect URI is registered on the app, so no exact-match games.
OURA_REDIRECT = CONFIG.get("oura_redirect_uri", "")
OURA_TOKENS_FILE = os.path.join(DATA, "oura_tokens.json")
OURA_SCOPES = "email personal daily heartrate workout session spo2"
# metric keys Oura owns once connected — HAE ingest must not clobber these
OURA_KEYS = ("sleep", "hrv", "resting_hr", "respiratory_rate", "blood_oxygen")
# Mark doesn't wear an Apple Watch: steps and active calories come from the ring only, and
# Apple Health / Home Assistant values for them are ignored.
OURA_ONLY = ("steps", "active_energy")
_OURA_LOCK = threading.Lock()


def _oura_tokens_load() -> dict:
    if os.path.exists(OURA_TOKENS_FILE):
        try:
            with open(OURA_TOKENS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _oura_tokens_save(tok: dict):
    tmp = f"{OURA_TOKENS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(tok, f, indent=2)
    os.replace(tmp, OURA_TOKENS_FILE)


def oura_authorize_url() -> str:
    from urllib.parse import urlencode
    q = {"response_type": "code", "client_id": _oura_creds()[0], "scope": OURA_SCOPES}
    if OURA_REDIRECT:
        q["redirect_uri"] = OURA_REDIRECT
    return "https://cloud.ouraring.com/oauth/authorize?" + urlencode(q)


def _oura_token_request(params: dict) -> dict:
    from urllib.parse import urlencode
    cid, sec = _oura_creds()
    body = urlencode({**params, "client_id": cid, "client_secret": sec}).encode()
    # Oura's OAuth moved to moi.ouraring.com (Curity) — the legacy api.ouraring.com/oauth/token
    # endpoint 400s with a generic invalid_request for every exchange.
    req = urllib.request.Request("https://moi.ouraring.com/oauth/v2/ext/oauth-token", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        if e.code in (400, 401) and params.get("grant_type") == "refresh_token":
            raise                                   # handled by _oura_access_token
        raise RuntimeError(f"Oura rejected the request ({e.code}): {detail}")


def _oura_store_grant(grant: dict, tok: dict = None):
    tok = tok or _oura_tokens_load()
    tok.update({
        "access_token": grant["access_token"],
        "refresh_token": grant.get("refresh_token") or tok.get("refresh_token"),
        "expires_at": time.time() + int(grant.get("expires_in") or 86400),
        "error": None,
    })
    _oura_tokens_save(tok)
    return tok


def oura_exchange_code(code_or_url: str) -> dict:
    """Turn the pasted redirect URL (or bare code) into a token pair. When a full URL is
    pasted, its scheme://host/path IS the registered redirect URI (Oura just sent the
    browser there) — echo it in the exchange so the match is exact, quirks and all."""
    raw = code_or_url.strip()
    code = raw
    if "code=" in raw:
        from urllib.parse import urlparse, parse_qs
        code = (parse_qs(urlparse(raw).query).get("code") or [""])[0]
    if not code:
        raise RuntimeError("no authorization code found in that paste")
    # redirect_uri in the exchange must mirror the authorize request: our authorize URL only
    # includes it when oura_redirect_uri is configured. Oura's server (Curity) rejects the
    # exchange with invalid_grant if the two requests disagree.
    params = {"grant_type": "authorization_code", "code": code}
    if OURA_REDIRECT:
        params["redirect_uri"] = OURA_REDIRECT
    grant = _oura_token_request(params)
    tok = _oura_store_grant(grant)
    threading.Thread(target=lambda: oura_sync(90), daemon=True).start()   # initial backfill
    return {"connected": True}


def _oura_access_token():
    with _OURA_LOCK:
        tok = _oura_tokens_load()
        if not tok.get("refresh_token") and not tok.get("access_token"):
            return None
        if time.time() < (tok.get("expires_at") or 0) - 300:
            return tok["access_token"]
        try:
            grant = _oura_token_request({"grant_type": "refresh_token",
                                         "refresh_token": tok.get("refresh_token", "")})
            return _oura_store_grant(grant, tok)["access_token"]
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):          # revoked / rotation lost — needs re-connect
                tok["error"] = "reauthorize"
                tok.pop("access_token", None)
                _oura_tokens_save(tok)
                return None
            raise


def _oura_get(path: str, params: dict) -> list:
    from urllib.parse import urlencode
    token = _oura_access_token()
    if not token:
        raise RuntimeError("Oura not connected")
    out, next_token = [], None
    while True:
        q = dict(params)
        if next_token:
            q["next_token"] = next_token
        req = urllib.request.Request(
            f"https://api.ouraring.com/v2/usercollection/{path}?{urlencode(q)}",
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            page = json.loads(resp.read())
        out.extend(page.get("data") or [])
        next_token = page.get("next_token")
        if not next_token:
            return out


def _series_points(series: dict) -> list:
    """Oura 5-minute series {interval, items, timestamp} → [(datetime, value)], gaps dropped."""
    if not isinstance(series, dict) or not series.get("timestamp"):
        return []
    try:
        t0 = datetime.fromisoformat(series["timestamp"])
    except (TypeError, ValueError):
        return []
    step = series.get("interval") or 300
    return [(t0 + timedelta(seconds=i * step), v) for i, v in enumerate(series.get("items") or [])
            if isinstance(v, (int, float))]


def night_detail(s: dict) -> dict:
    """What a night's heart rate and HRV curves say: when heart rate bottomed out (late = the
    body was still working on food or alcohol), whether HRV recovered in the second half, how
    long it took to fall asleep and time awake. Missing pieces are simply left out."""
    out = {}
    if isinstance(s.get("latency"), (int, float)):
        out["sleep_latency"] = round(s["latency"] / 60, 1)                 # minutes
    if isinstance(s.get("awake_time"), (int, float)):
        out["awake_time"] = round(s["awake_time"] / 60, 1)                 # minutes
    hr = _series_points(s.get("heart_rate"))
    if hr:
        t_low, _ = min(hr, key=lambda p: p[1])
        out["hr_low_hour"] = round(((t_low.hour + t_low.minute / 60) - 12) % 24, 2)  # h from noon
        try:
            bed = datetime.fromisoformat(s.get("bedtime_start"))
            out["hr_low_after_bed"] = round((t_low - bed).total_seconds() / 3600, 2)
        except (TypeError, ValueError):
            pass
    hrv = [v for _, v in _series_points(s.get("hrv"))]
    if len(hrv) >= 8:
        half = len(hrv) // 2
        out["hrv_late_vs_early"] = round(statistics.mean(hrv[half:]) - statistics.mean(hrv[:half]), 1)
    return out


def backfill_night_detail():
    """One-off: nights synced before night detail existed lack it, and routine syncs only
    cover the last few days, so re-pull 90 days once when no night has it yet."""
    try:
        ms = _load_metrics_store().values()
        if oura_status()["connected"] and (not any("hr_low_hour" in m for m in ms)
                                           or not any(_oura_owned(m.get("steps")) for m in ms)):
            print(f"[oura] backfilling night detail: {oura_sync(90)}")
        elif oura_status()["connected"] and not any(k.startswith("oura_") for k in _load_workouts_file()):
            print(f"[oura] backfilling workouts: {sync_oura_workouts(56)}")
    except Exception as e:
        print(f"[oura] night-detail backfill failed: {e}")


NIGHT_UNITS = {"sleep_latency": "min", "awake_time": "min", "hr_low_hour": "h-from-noon",
               "hr_low_after_bed": "h", "hrv_late_vs_early": "ms"}
READINESS_PARTS = ("hrv_balance", "recovery_index", "resting_heart_rate", "body_temperature",
                   "sleep_balance", "previous_day_activity")


def oura_sync(days: int = 7) -> dict:
    """Pull the last `days` from Oura and fold into the metrics store. Oura becomes the
    source of truth for sleep / HRV / RHR / SpO2 / respiratory rate on days it covers."""
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=days)).isoformat()
    rng = {"start_date": start, "end_date": end}
    sleeps = _oura_get("sleep", rng)
    readiness = _oura_get("daily_readiness", rng)
    dsleep = _oura_get("daily_sleep", rng)
    extras = {}
    for name in ("daily_spo2", "daily_stress", "daily_resilience", "daily_activity"):
        try:
            extras[name] = _oura_get(name, rng)
        except (urllib.error.HTTPError, RuntimeError):
            extras[name] = []
    spo2, stress, resilience = extras["daily_spo2"], extras["daily_stress"], extras["daily_resilience"]
    store = _load_metrics_store()

    def put(day, key, entry):
        store.setdefault(day, {})[key] = {**entry, "source": "Oura API", "from_oura": True}

    # detailed sleep: keep the main (longest) session per day
    by_day = {}
    for s in sleeps:
        d = s.get("day")
        if d and (d not in by_day or (s.get("total_sleep_duration") or 0)
                  > (by_day[d].get("total_sleep_duration") or 0)):
            by_day[d] = s
    hrs = lambda sec: round(sec / 3600, 2) if isinstance(sec, (int, float)) else None
    for d, s in by_day.items():
        put(d, "sleep", {"asleep_hours": hrs(s.get("total_sleep_duration")),
                         "deep": hrs(s.get("deep_sleep_duration")),
                         "rem": hrs(s.get("rem_sleep_duration")),
                         "core": hrs(s.get("light_sleep_duration")),
                         "efficiency": s.get("efficiency")})
        # Bedtime/wake as "hours since the preceding noon" (0=noon, 8=8pm, 15=3am, 22=10am) —
        # a continuous scale so an evening bedtime and a small-hours wake time both sort onto
        # one increasing axis instead of wrapping across midnight.
        def hour_from_noon(iso):
            if not iso:
                return None
            try:
                t = datetime.fromisoformat(iso)
            except (TypeError, ValueError):
                return None
            return round(((t.hour + t.minute / 60 + t.second / 3600) - 12) % 24, 3)
        bh, wh = hour_from_noon(s.get("bedtime_start")), hour_from_noon(s.get("bedtime_end"))
        if bh is not None:
            put(d, "bedtime_hour", {"value": bh, "unit": "h-from-noon"})
        if wh is not None:
            put(d, "wake_hour", {"value": wh, "unit": "h-from-noon"})
        tib = hrs(s.get("time_in_bed"))
        if tib is not None:
            put(d, "time_in_bed", {"value": tib, "unit": "h"})
        if s.get("average_hrv") is not None:
            put(d, "hrv", {"value": s["average_hrv"], "unit": "ms", "averaged": True})
        if s.get("lowest_heart_rate") is not None:
            put(d, "resting_hr", {"value": s["lowest_heart_rate"], "unit": "count/min",
                                  "averaged": True})
        if s.get("average_breath") is not None:
            put(d, "respiratory_rate", {"value": s["average_breath"], "unit": "count/min"})
        for k, v in night_detail(s).items():
            put(d, k, {"value": v, "unit": NIGHT_UNITS[k]})
    for r in readiness:
        d = r.get("day")
        if not d:
            continue
        if r.get("score") is not None:
            put(d, "readiness_score", {"value": r["score"], "unit": "score"})
        if r.get("temperature_deviation") is not None:
            put(d, "temp_deviation", {"value": round(r["temperature_deviation"], 2),
                                      "unit": "°C"})
        for k in READINESS_PARTS:                 # 1-100 contributor scores behind readiness
            v = (r.get("contributors") or {}).get(k)
            if isinstance(v, (int, float)):
                put(d, "readiness_" + k, {"value": v, "unit": "score"})
    for s in dsleep:
        d, sc = s.get("day"), s.get("score")
        if d and sc is not None:
            put(d, "sleep_score", {"value": sc, "unit": "score"})
    for a in extras["daily_activity"]:
        d, sc = a.get("day"), a.get("score")
        if d and sc is not None:
            put(d, "activity_score", {"value": sc, "unit": "score"})
        if d and isinstance(a.get("steps"), (int, float)):
            put(d, "steps", {"value": a["steps"], "unit": "count"})
        if d and isinstance(a.get("active_calories"), (int, float)):
            put(d, "active_energy", {"value": a["active_calories"], "unit": "kcal"})
    for s in spo2:
        d = s.get("day")
        avg = ((s.get("spo2_percentage") or {}).get("average"))
        if d and avg is not None:
            put(d, "blood_oxygen", {"value": round(avg, 1), "unit": "%"})
        # breathing disturbance index — the sleep-apnoea-adjacent signal (higher = worse)
        bdi = s.get("breathing_disturbance_index")
        if d and bdi is not None:
            put(d, "breathing_disturbance", {"value": round(bdi, 1), "unit": "idx"})
    for s in stress:
        d = s.get("day")
        if d and s.get("stress_high") is not None:
            put(d, "daytime_stress", {"value": round(s["stress_high"] / 3600, 2), "unit": "h"})
    RES_LEVELS = {"limited": 1, "adequate": 2, "solid": 3, "strong": 4, "exceptional": 5}
    for s in resilience:
        d, lvl = s.get("day"), RES_LEVELS.get(s.get("level"))
        if d and lvl:
            put(d, "resilience", {"value": lvl, "unit": "/5", "level": s.get("level")})
    _save_metrics_store(store)
    try:
        sync_oura_workouts(days)
    except Exception as e:                            # workouts are extra; never fail the sync
        print(f"[oura] workout sync failed: {e}")
        _workout_sync_status(error=str(e))
    tok = _oura_tokens_load()
    # Oura's optimal-bedtime recommendation (sleep_time endpoint) — latest one wins
    try:
        stimes = _oura_get("sleep_time", rng)
    except (urllib.error.HTTPError, RuntimeError):
        stimes = []
    for st_ in sorted(stimes, key=lambda x: x.get("day") or ""):
        ob = st_.get("optimal_bedtime") or {}
        if ob.get("start_offset") is not None and ob.get("end_offset") is not None:
            hhmm = lambda off: f"{(int(off) % 86400) // 3600:02d}:{(int(off) % 3600) // 60:02d}"
            tok["bedtime"] = {"day": st_.get("day"),
                              "window": f"{hhmm(ob['start_offset'])}–{hhmm(ob['end_offset'])}",
                              "recommendation": st_.get("recommendation")}
    # Illness early-warning: night temperature ≥ +0.5 °C over baseline → one push per day
    latest_temp = None
    for r in sorted(readiness, key=lambda x: x.get("day") or ""):
        if r.get("temperature_deviation") is not None:
            latest_temp = (r["day"], r["temperature_deviation"])
    if latest_temp and latest_temp[1] >= 0.5 and tok.get("temp_alert_day") != latest_temp[0]:
        service = CONFIG.get("notify_service", "").strip("/")
        if service:
            _ha_request(f"/api/services/{service}", "POST", {
                "title": "🌡 Body temperature elevated",
                "message": (f"Night temp {latest_temp[1]:+.1f} °C vs your baseline — possible "
                            f"incoming illness or heavy strain. Consider an easy day and an "
                            f"early night.")})
            tok["temp_alert_day"] = latest_temp[0]
    tok["last_sync"] = datetime.now().isoformat(timespec="seconds")
    tok["last_sync_days"] = len(by_day)
    _oura_tokens_save(tok)
    print(f"[oura] synced {len(by_day)} sleep days, {len(readiness)} readiness")
    return {"days": len(by_day)}


def oura_status() -> dict:
    tok = _oura_tokens_load()
    cid, sec = _oura_creds()
    return {"configured": bool(cid and sec),
            "connected": bool(tok.get("refresh_token")) and tok.get("error") != "reauthorize",
            "error": tok.get("error"),
            "last_sync": tok.get("last_sync"),
            "authorize_url": oura_authorize_url() if cid else None}


def _day_of(raw: str) -> str:
    """Extract YYYY-MM-DD from Health Auto Export date strings."""
    if not raw:
        return ""
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:len(datetime.now().strftime(fmt))], fmt).date().isoformat()
        except (ValueError, TypeError):
            continue
    # last-ditch: first 10 chars look like a date?
    return raw[:10] if raw[:4].isdigit() else ""


def _sleep_hours(point: dict):
    for k in ("asleep", "totalSleep", "qty", "value"):
        v = point.get(k)
        if isinstance(v, (int, float)):
            return round(v, 2)
    # try sleepStart/sleepEnd delta
    try:
        s = datetime.fromisoformat(point["sleepStart"][:19])
        e = datetime.fromisoformat(point["sleepEnd"][:19])
        return round((e - s).total_seconds() / 3600, 2)
    except Exception:
        return None


# Match Health Auto Export entity_ids by substring. HAE publishes under the `hae.`
# domain (e.g. hae.ha_heart_rate_variability); we also accept sensor.* just in case.
HA_METRIC_PATTERNS = [
    ("hrv", ["heart_rate_variability"]),
    ("resting_hr", ["resting_heart_rate"]),
    ("heart_rate", ["heart_rate_avg"]),
    ("sleep", ["sleep_analysis", "time_asleep", "sleep_asleep"]),
    ("steps", ["step_count"]),
    ("active_energy", ["active_energy"]),
    ("exercise_minutes", ["exercise_time"]),
    ("respiratory_rate", ["respiratory_rate"]),
    ("blood_oxygen", ["oxygen_saturation", "blood_oxygen"]),
    ("weight", ["weight_body_mass", "body_mass"]),
    ("vo2_max", ["vo2_max"]),
    ("mindful_minutes", ["mindful_minutes"]),
]

# Substrings that disqualify an entity for a metric — "body_mass" would otherwise
# also match hae.ha_body_mass_index (BMI ~28) and hae.ha_lean_body_mass (~88).
HA_METRIC_EXCLUDE = {"weight": ["index", "lean", "fat"]}


# Sanity caps — daily values above these are HAE sync glitches; drop them.
SANE_MAX = {"mindful_minutes": 240, "exercise_minutes": 300, "active_energy": 3500, "weight": 250}
SANE_MIN = {"weight": 60}


def _weight_plausible(value, day: str = None, series: dict = None,
                       window_days: int = 30, max_pct: float = 0.15) -> bool:
    """Guard against a single bad scale reading (dropped connection, partial measurement,
    someone else's device) silently overwriting a good one and corrupting every 'current
    weight' display. No global sane-max works here (everyone's baseline differs), so this
    rejects readings that are wildly off the person's OWN recent trend instead."""
    if not isinstance(value, (int, float)) or not (30 <= value <= 300):
        return False
    day = day or date.today().isoformat()
    series = series if series is not None else _series_map().get("weight", {})
    cutoff = (date.fromisoformat(day) - timedelta(days=window_days)).isoformat()
    recent = [v for d, v in series.items() if cutoff <= d < day]
    if not recent:
        return True   # no baseline yet — the absolute sanity band above is all we can check
    baseline = statistics.median(recent)
    return abs(value - baseline) <= max(baseline * max_pct, 5)


def metrics_from_ha(states=None) -> dict:
    """Read today's health metrics from HA (Health Auto Export's native export, `hae.` domain)."""
    if states is None:
        states = _ha_request("/api/states", quiet=True)
    if not isinstance(states, list):
        return {}
    out = {}
    for s in states:
        eid = s.get("entity_id", "").lower()
        if not (eid.startswith("hae.") or eid.startswith("sensor.")):
            continue
        for key, pats in HA_METRIC_PATTERNS:
            if key in OURA_ONLY or key in out or not any(p in eid for p in pats):
                continue
            if any(x in eid for x in HA_METRIC_EXCLUDE.get(key, ())):
                continue
            attrs = s.get("attributes", {})
            try:
                val = round(float(s.get("state")), 2)
            except (TypeError, ValueError):
                val = s.get("state")
            src = attrs.get("source") or "HA"
            if key == "sleep":
                out[key] = {"asleep_hours": val, "source": src, "entity_id": s["entity_id"]}
            else:
                unit = attrs.get("unit_of_measurement", "")
                if key == "active_energy" and isinstance(val, (int, float)) and "cal" not in unit.lower():
                    val = round(val / KJ_PER_KCAL)   # kJ → kcal
                    unit = "kcal"
                if isinstance(val, (int, float)) and (
                        val > SANE_MAX.get(key, float("inf")) or val < SANE_MIN.get(key, float("-inf"))):
                    break    # spike / sync glitch — skip this metric for now
                if key == "weight" and not _weight_plausible(val):
                    print(f"[metrics] rejected implausible weight reading {val}kg from HA")
                    break
                out[key] = {"value": val, "unit": unit,
                            "source": src, "entity_id": s["entity_id"],
                            "from_oura": "oura" in json.dumps(attrs).lower()}
            break
    return out


# Metrics shown as a daily AVERAGE (they fluctuate intraday) rather than the latest reading.
AVG_METRICS = {"hrv", "resting_hr"}
INTRADAY_FILE = os.path.join(DATA, "intraday.json")
_intraday_lock = threading.Lock()


def _load_intraday() -> dict:
    if os.path.exists(INTRADAY_FILE):
        with open(INTRADAY_FILE) as f:
            return json.load(f)
    return {}


def accumulate_intraday(cur: dict):
    """Fold each new reading of an AVG metric into today's running mean."""
    with _intraday_lock:
        store = _load_intraday()
        day = store.setdefault(date.today().isoformat(), {})
        changed = False
        for k in AVG_METRICS:
            v = (cur.get(k) or {}).get("value")
            if v is None:
                continue
            acc = day.setdefault(k, {"sum": 0.0, "count": 0, "last": None})
            if acc["last"] != v:                      # only count genuinely new readings
                acc["sum"] += v; acc["count"] += 1; acc["last"] = v; changed = True
        if changed:
            tmp = f"{INTRADAY_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w") as f:
                json.dump(store, f)
            os.replace(tmp, INTRADAY_FILE)


def daily_avg(metric: str):
    acc = _load_intraday().get(date.today().isoformat(), {}).get(metric)
    return round(acc["sum"] / acc["count"], 2) if (acc and acc["count"]) else None


def todays_metrics(states=None) -> dict:
    """Live metrics with AVG_METRICS replaced by the day's running average."""
    cur = metrics_from_ha(states)
    accumulate_intraday(cur)
    for k in AVG_METRICS:
        avg = daily_avg(k)
        if avg is not None and k in cur:
            cur[k] = {**cur[k], "value": avg, "averaged": True}
    return cur


def _oura_owned(existing) -> bool:
    return isinstance(existing, dict) and existing.get("source") == "Oura API"


def metrics_for(day: str) -> dict:
    out = dict(_load_metrics_store().get(day, {}))
    if day == date.today().isoformat():
        # Live values take precedence for today — EXCEPT keys Oura owns: its one stable
        # overnight value must not be replaced by the Watch's jittery intraday spot average.
        for k, v in todays_metrics().items():
            if _device_owned(k, out.get(k)):
                continue
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
#  Analytics — readiness vs baseline, training load, patterns/correlations.
# --------------------------------------------------------------------------- #
# +1 = higher is better, -1 = lower is better, 0 = neutral.
GOOD_DIR = {"hrv": 1, "resting_hr": -1, "sleep": 1, "steps": 1, "active_energy": 1,
            "exercise_minutes": 1, "blood_oxygen": 1, "respiratory_rate": 0,
            "weight": 0, "heart_rate": 0, "vo2_max": 1, "mindful_minutes": 1}
METRIC_LABEL = {"hrv": "HRV", "resting_hr": "Resting HR", "sleep": "Sleep",
                "steps": "Steps", "active_energy": "Active energy",
                "exercise_minutes": "Exercise", "blood_oxygen": "Blood O₂",
                "respiratory_rate": "Respiratory", "weight": "Weight", "heart_rate": "Heart rate",
                "vo2_max": "VO₂ max", "mindful_minutes": "Mindful min"}

# Age/sex reference bands (Mark: 50yo male). Each: ordered (threshold, rating) where the
# rating applies at/above threshold for "higher is better" metrics, or at/below for "lower".
# rating scale: 4 excellent, 3 good, 2 average, 1 below, 0 poor.
AGE_REF = {
    "vo2_max":   {"dir": 1,  "bands": [(45, "excellent"), (39, "good"), (34, "average"), (30, "below"), (0, "poor")]},
    "resting_hr": {"dir": -1, "bands": [(58, "excellent"), (63, "good"), (70, "average"), (80, "below"), (999, "poor")]},
    "hrv":       {"dir": 1,  "bands": [(60, "excellent"), (45, "good"), (33, "average"), (25, "below"), (0, "poor")], "approx": True},
}


def age_rating(metric: str, value):
    ref = AGE_REF.get(metric)
    if ref is None or value is None:
        return None
    if ref["dir"] == 1:
        rating = next((r for t, r in ref["bands"] if value >= t), "poor")
    else:
        rating = next((r for t, r in ref["bands"] if value <= t), "poor")
    return {"rating": rating, "approx": ref.get("approx", False)}


def _series_map() -> dict:
    """metric -> {date: value} across the whole store."""
    out = {}
    for day, ms in _load_metrics_store().items():
        for k, m in ms.items():
            if isinstance(m, dict):
                v = _metric_value(k, m)
                if v is not None:
                    out.setdefault(k, {})[day] = v
    return out


def _window_mean(series: dict, days: int):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    vals = [v for d, v in series.items() if d >= cutoff]
    return statistics.mean(vals) if vals else None


def metric_baselines(days: int = 90, sm: dict = None) -> dict:
    """Per metric: today vs trailing-`days` baseline (mean/SD/z-score)."""
    sm = sm if sm is not None else _series_map()
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    out = {}
    for k, series in sm.items():
        hist = [v for d, v in series.items() if cutoff <= d < today]
        cur = series.get(today)
        if len(hist) < 5:
            continue
        mean = statistics.mean(hist)
        sd = statistics.pstdev(hist)
        z = round((cur - mean) / sd, 2) if (cur is not None and sd) else None
        out[k] = {"label": METRIC_LABEL.get(k, k), "today": cur, "mean": round(mean, 2),
                  "sd": round(sd, 2), "z": z, "n": len(hist), "dir": GOOD_DIR.get(k, 0),
                  "age": age_rating(k, cur if cur is not None else mean)}
    return out


# Readiness is recovery only — overnight-stable metrics, so the score doesn't drift as the day's
# activity accumulates. Activity (the goal metrics) is scored separately and is plan-aware.
RECOVERY_METRICS = ["hrv", "resting_hr", "sleep"]
ACTIVITY_METRICS = ["steps", "active_energy"]
SCORE_METRICS = RECOVERY_METRICS + ACTIVITY_METRICS
# Activity score = how close to target you are: average of each metric's share of its goal
# (capped at 100%). Keep in sync with the frontend GOALS map.
GOALS = {"steps": 8000, "active_energy": 1000}


def _score_stats(sm: dict) -> dict:
    """Per-metric mean/sd baseline for the recovery z-scores."""
    out = {}
    for k in RECOVERY_METRICS:
        vals = list(sm.get(k, {}).values())
        if len(vals) >= 5:
            out[k] = {"mean": statistics.mean(vals), "sd": statistics.pstdev(vals)}
    return out


def recovery_score(day: str, sm: dict, stats: dict):
    """0-100 readiness for one day from HRV/resting HR/sleep, z-scored vs baseline.
    Overnight-stable — does not change as the day goes on."""
    zs, comps = [], {}
    for k in RECOVERY_METRICS:
        v = sm.get(k, {}).get(day)
        st = stats.get(k)
        if v is None or not st or not st["sd"]:
            continue
        o = max(-2.0, min(2.0, (v - st["mean"]) / st["sd"] * (GOOD_DIR.get(k, 1) or 1)))
        comps[k] = round(o, 2)
        zs.append(o)
    if not zs:
        return None, comps
    return round(max(10, min(98, 55 + statistics.mean(zs) * 16))), comps


def _workout_days() -> set:
    """Dates the planned workout was completed → full activity credit. Two sources:
      1. a watch/Health-recorded workout in workouts.json (≥10 min), and
      2. a session marked done — via the HA 'done' toggle or auto-advance — which
         advance_session() records in state['log'] with a completed_at timestamp.
    (2) is what makes a workout you ticked done in HA count even with no watch on."""
    days = set()
    for w in _load_workouts_file().values():
        d = w.get("date")
        mins = w.get("minutes")
        if d and (mins is None or mins >= 10):
            days.add(d)
    for e in load_state().get("log", []):
        ca = e.get("completed_at")
        if ca:
            days.add(ca[:10])
    return days


def activity_score(day: str, sm: dict, workout_days: set = None) -> dict:
    """Goal attainment for steps / active energy. Missing = 0 (didn't do it).
    Completing the planned workout scores 100 regardless of step/energy goals."""
    if workout_days is None:
        workout_days = _workout_days()
    did_workout = day in workout_days
    comps, ratios, met = {}, [], 0
    for k in ACTIVITY_METRICS:
        v = sm.get(k, {}).get(day) or 0
        goal = GOALS[k]
        r = min(v / goal, 1.0) if goal else 0.0
        hit = v >= goal
        met += 1 if hit else 0
        ratios.append(r)
        comps[k] = {"label": METRIC_LABEL.get(k, k), "v": round(v, 1), "goal": goal,
                    "pct": round(r * 100), "met": hit}
    score = round(100 * sum(ratios) / len(ratios)) if ratios else None
    if did_workout:
        score = 100
    return {"score": score, "met": met, "total": len(ACTIVITY_METRICS),
            "workout_done": did_workout, "components": comps}


def activity_series(days: int = 35, sm: dict = None) -> list:
    """Activity (goal) score per day for the heatmap, with a per-goal breakdown."""
    sm = sm if sm is not None else _series_map()
    wd = _workout_days()
    out = []
    start = date.today() - timedelta(days=days - 1)
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        if not (any(d in sm.get(k, {}) for k in ACTIVITY_METRICS) or d in wd):
            continue
        a = activity_score(d, sm, wd)
        parts = [{"k": k, "label": c["label"], "v": c["v"], "goal": c["goal"],
                  "pct": c["pct"], "ok": c["met"]} for k, c in a["components"].items()]
        out.append({"t": d, "v": a["score"], "c": parts,
                    "workout": a["workout_done"]})
    return out


def score_series(days: int = 35, sm: dict = None) -> list:
    """Recovery (readiness) score per day for the heatmap/trend, with a per-metric breakdown."""
    sm = sm if sm is not None else _series_map()
    stats = _score_stats(sm)
    out = []
    start = date.today() - timedelta(days=days - 1)
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        s, comps = recovery_score(d, sm, stats)
        if s is None:
            continue
        parts = [{"k": k, "label": METRIC_LABEL.get(k, k), "v": round(sm[k][d], 1),
                  "z": z, "ok": z >= -0.5} for k, z in comps.items()]
        out.append({"t": d, "v": s, "c": parts})
    return out


def readiness(sm: dict = None) -> dict:
    """Today's recovery/readiness score (HRV, resting HR, sleep)."""
    sm = sm if sm is not None else _series_map()
    stats = _score_stats(sm)
    score, comps = recovery_score(date.today().isoformat(), sm, stats)
    if score is None:
        return {"score": None, "label": "no recovery data today", "components": {}}
    avg = statistics.mean(comps.values()) if comps else 0
    label = "High" if avg > 0.5 else "Low" if avg < -0.5 else "Normal"
    return {"score": score, "label": label, "components": comps}


def training_load(sm: dict = None) -> dict:
    """Acute (7d) vs chronic (28d) load + recovery direction."""
    sm = sm if sm is not None else _series_map()
    load = sm.get("active_energy") or sm.get("exercise_minutes") or {}
    acute, chronic = _window_mean(load, 7), _window_mean(load, 28)
    ratio = round(acute / chronic, 2) if (acute and chronic) else None
    flag = "balanced"
    if ratio is not None:
        flag = "ramping hard — watch recovery" if ratio > 1.3 else \
               "easing / detraining" if ratio < 0.8 else "balanced"
    def rec(metric):
        return _window_mean(sm.get(metric, {}), 7), _window_mean(sm.get(metric, {}), 28)
    hrv7, hrv28 = rec("hrv")
    rhr7, rhr28 = rec("resting_hr")
    return {
        "metric": "active_energy" if sm.get("active_energy") else "exercise_minutes",
        "acute_7d": round(acute, 1) if acute else None,
        "chronic_28d": round(chronic, 1) if chronic else None,
        "ratio": ratio, "flag": flag,
        "hrv_7d": round(hrv7, 1) if hrv7 else None, "hrv_28d": round(hrv28, 1) if hrv28 else None,
        "resting_hr_7d": round(rhr7, 1) if rhr7 else None,
        "resting_hr_28d": round(rhr28, 1) if rhr28 else None,
    }


# Recovery metrics for the live early-warning trend, with the "strain" direction:
# resting HR up = strain, HRV down = strain, respiratory rate up = strain.
TREND_METRICS = [("resting_hr", -1), ("hrv", 1), ("respiratory_rate", -1)]


def _trailing_mean(series: dict, day: str, days: int):
    """Mean of values in [day-`days`, day) — causal, excludes `day` itself."""
    cutoff = (date.fromisoformat(day) - timedelta(days=days)).isoformat()
    vals = [v for d, v in series.items() if cutoff <= d < day]
    return statistics.mean(vals) if vals else None


def readiness_trend(sm: dict = None) -> dict:
    """Today vs personal 14/30-day baseline per recovery metric, with
    consecutive-day drift and a plain-language strain read. This is the live
    early-warning signal — deviation from your own baseline, not absolutes.
    (Phase-4 model swaps in here; the contract stays the same.)"""
    sm = sm if sm is not None else _series_map()
    today = date.today()
    rows, strain_hits = [], 0

    for k, good in TREND_METRICS:
        s = sm.get(k, {})
        # today, else the most recent reading within 2 days
        cur, used = None, None
        for back in range(0, 3):
            d = (today - timedelta(days=back)).isoformat()
            if d in s:
                cur, used = s[d], d
                break
        if cur is None:
            continue

        b14 = _trailing_mean(s, used, 14)
        b30 = _trailing_mean(s, used, 30)
        if b14 is None:
            continue
        dev = cur - b14

        # robust SD from the trailing 30d for the z-score
        win = [v for d, v in s.items()
               if (date.fromisoformat(used) - timedelta(days=30)).isoformat() <= d < used]
        sd = statistics.pstdev(win) if len(win) >= 5 else None
        z = round(dev / sd, 1) if sd else None

        # consecutive days on the "strain" side of the day's own 14d baseline
        margin = 0.3 * (sd or max(abs(b14) * 0.02, 0.5))
        run = 0
        for back in range(0, 21):
            d = (date.fromisoformat(used) - timedelta(days=back)).isoformat()
            if d not in s:
                continue
            bl = _trailing_mean(s, d, 14)
            if bl is None:
                break
            if (s[d] - bl) * good < -margin:
                run += 1
            else:
                break

        strained = (dev * good) < 0 and z is not None and abs(z) >= 0.8
        if strained:
            strain_hits += 1
        rows.append({
            "k": k, "label": METRIC_LABEL.get(k, k), "today": round(cur, 1),
            "base14": round(b14, 1), "base30": round(b30, 1) if b30 else None,
            "dev": round(dev, 1), "z": z, "good": good, "run": run,
            "strained": strained, "day": used,
        })

    max_run = max((r["run"] for r in rows if r["strained"]), default=0)
    if strain_hits >= 2:
        verdict, tone = "autonomic strain building", "bad"
    elif strain_hits == 1:
        verdict, tone = "mild drift from baseline", "warn"
    else:
        verdict, tone = "in your normal range", "good"
    note = verdict
    if strain_hits and max_run >= 2:
        note = f"{verdict} · day {max_run}"
    return {"rows": rows, "verdict": verdict, "tone": tone,
            "strain_count": strain_hits, "max_run": max_run, "note": note}


# Metrics scanned for a statistically significant recent trend.
CHANGE_METRICS = ["resting_hr", "hrv", "respiratory_rate", "sleep", "steps",
                  "active_energy", "weight", "vo2_max", "blood_oxygen",
                  "exercise_minutes", "mindful_minutes"]
CHANGE_UNIT = {"resting_hr": "bpm", "hrv": "ms", "respiratory_rate": "br/m",
               "sleep": "h", "steps": "", "active_energy": "cal", "weight": "kg",
               "vo2_max": "", "blood_oxygen": "%", "exercise_minutes": "min",
               "mindful_minutes": "min"}
CHANGE_WINDOW_DAYS = 90    # "recent data" window the trend is fitted over
CHANGE_MIN_POINTS = 8      # need at least this many readings to fit a trend
NEW_TREND_DAYS = 7         # a trend stays flagged "new" for this many days


def significant_changes(sm: dict = None) -> list:
    """Tracked metrics with a statistically significant linear trend in recent
    data (last CHANGE_WINDOW_DAYS). Significance = the value-vs-time correlation
    is significant (p<0.05) — magnitude isn't gated, the trend either is or isn't
    real. Each entry carries the recent daily series for a chart, plus an
    is_new flag (trend not seen before, within NEW_TREND_DAYS of first appearing)."""
    sm = sm if sm is not None else _series_map()
    lo = (date.today() - timedelta(days=CHANGE_WINDOW_DAYS)).isoformat()
    out = []
    for k in CHANGE_METRICS:
        s = sm.get(k, {})
        pts = sorted((d, v) for d, v in s.items() if d >= lo)
        if len(pts) < CHANGE_MIN_POINTS:
            continue
        xs = [date.fromisoformat(d).toordinal() for d, _ in pts]
        ys = [v for _, v in pts]
        r = _pearson(xs, ys, minn=CHANGE_MIN_POINTS)
        if r is None:
            continue
        p = _corr_p(r, len(pts))
        if p is None or p >= 0.05:        # not a significant trend → skip
            continue
        # least-squares slope → total change across the observed window
        mx, my = statistics.mean(xs), statistics.mean(ys)
        den = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0
        start_fit = my + slope * (xs[0] - mx)
        end_fit = my + slope * (xs[-1] - mx)
        change = end_fit - start_fit
        gd = GOOD_DIR.get(k, 0)
        out.append({
            "k": k, "label": METRIC_LABEL.get(k, k), "unit": CHANGE_UNIT.get(k, ""),
            "start": round(start_fit, 1), "end": round(end_fit, 1),
            "change": round(change, 1),
            "pct": round(change / start_fit * 100) if start_fit else 0,
            "per_year": round(slope * 365, 2),
            "favourable": (None if not gd else change * gd > 0),
            "r": round(r, 2), "p": round(p, 4), "n": len(pts),
            "dir": "up" if change > 0 else "down",
            "series": [{"t": d, "v": v} for d, v in pts],
        })
    out.sort(key=lambda x: x["p"])        # most significant first
    _flag_new_trends(out)
    return out


def _flag_new_trends(trends: list):
    """Tag each trend is_new by tracking first-seen dates in state. A trend
    (metric+direction) is new until NEW_TREND_DAYS pass or the user dismisses it;
    trends that vanish are forgotten so a re-emergence counts as new again."""
    today = date.today()
    st = load_state()
    track = st.get("trend_tracking", {})
    current = set()
    for t in trends:
        key = f"{t['k']}:{t['dir']}"
        current.add(key)
        rec = track.get(key) or {"first_seen": today.isoformat(), "acked": False}
        track[key] = rec
        age = (today - date.fromisoformat(rec["first_seen"])).days
        t["is_new"] = age <= NEW_TREND_DAYS and not rec.get("acked")
    for key in [k for k in track if k not in current]:
        del track[key]                    # forget trends that are no longer significant
    st["trend_tracking"] = track
    save_state(st)


def ack_trends() -> dict:
    """Dismiss the 'new' highlight on all currently-significant trends."""
    st = load_state()
    track = st.get("trend_tracking", {})
    for k in track:
        track[k]["acked"] = True
    st["trend_tracking"] = track
    save_state(st)
    return {"ok": True}


def _pearson(xs, ys, minn=20):
    n = len(xs)
    if n < minn:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if not sx or not sy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def _corr_p(r, n):
    """Two-tailed p-value for a Pearson r via the Fisher z-transform (normal approx)."""
    if r is None or n is None or n < 4:
        return None
    rr = max(-0.999999, min(0.999999, r))
    z = math.atanh(rr) * math.sqrt(n - 3)
    return math.erfc(abs(z) / math.sqrt(2))


def _sig_fields(r, n):
    """sig/p annotation shared by every correlation entry. Significant = p < 0.05."""
    p = _corr_p(r, n)
    return {"p": round(p, 4) if p is not None else None, "sig": bool(p is not None and p < 0.05)}


def correlations(sm: dict = None) -> dict:
    sm = sm if sm is not None else _series_map()

    # Make the composite recovery score available as a correlation outcome (one value per day).
    rstats = _score_stats(sm)
    rec_days = set().union(*[set(sm.get(k, {})) for k in RECOVERY_METRICS]) if any(sm.get(k) for k in RECOVERY_METRICS) else set()
    rec_by_day = {}
    for d in rec_days:
        s, _ = recovery_score(d, sm, rstats)
        if s is not None:
            rec_by_day[d] = s
    sm = {**sm, "recovery": rec_by_day}

    # weekday HRV averages
    byday = {}
    for d, v in sm.get("hrv", {}).items():
        byday.setdefault(date.fromisoformat(d).strftime("%a"), []).append(v)
    weekday_hrv = {k: round(statistics.mean(v), 1) for k, v in byday.items() if len(v) >= 5}

    def drift_per_year(metric):
        s = sm.get(metric, {})
        if len(s) < 30:
            return None
        ds = sorted(s)
        xs = [date.fromisoformat(d).toordinal() for d in ds]
        ys = [s[d] for d in ds]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        den = sum((x - mx) ** 2 for x in xs)
        if not den:
            return None
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
        return round(slope * 365, 2)

    drift = {m: drift_per_year(m) for m in ("hrv", "resting_hr", "weight", "vo2_max")}

    # Predictors → next-day recovery score. Check-in behaviours + a few auto metrics, all
    # correlated against the composite recovery score (recovery's own components are excluded —
    # that would be circular). This is the single outcome Mark cares about.
    bh = _load_behav()
    bseries = {}
    for d, rec in bh.items():
        for k, v in rec.items():
            if k in ("drink", "clean", "late_meal", "stress", "eczema") and isinstance(v, (int, float)):
                bseries.setdefault(k, {})[d] = v

    def behav_series(bk):
        return bseries[bk] if bk in bseries else sm.get(bk, {})

    def lag_pair(A, B, lag):
        xs, ys = [], []
        for d, va in A.items():
            d2 = (date.fromisoformat(d) + timedelta(days=lag)).isoformat()
            if d2 in B:
                xs.append(va); ys.append(B[d2])
        return xs, ys

    # (label, series key) — each correlated against the NEXT day's recovery score.
    PREDICTORS = [
        ("Alcohol", "drink"),
        ("Ate clean", "clean"),
        ("Stress", "stress"),
        ("Eczema", "eczema"),
        ("Late meal", "late_meal"),
        ("Meditation", "mindful_minutes"),
        ("Training", "active_energy"),
        ("Steps", "steps"),
    ]
    rec = sm.get("recovery", {})
    behaviour = []
    for plab, pk in PREDICTORS:
        A = behav_series(pk)
        if not A:
            continue
        xs, ys = lag_pair(A, rec, 1)
        r = _pearson(xs, ys, minn=10)
        if r is None:
            continue
        behaviour.append({
            "behaviour": plab, "outcome": "Recovery", "outcome_key": "recovery",
            "label": f"{plab} → next-day recovery",
            "r": round(r, 2), "n": len(xs),
            "good_dir": 1, "kind": "behaviour",
            **_sig_fields(r, len(xs)),
        })
    # Eczema drivers — objective signals vs his nightly eczema score (0=clear..3=bad).
    # good_dir -1: a positive r means the predictor comes with WORSE skin. Rows appear
    # automatically once each pairing clears the minn=10 sample guard.
    ecz = bseries.get("eczema", {})
    if ecz:
        ECZ_PREDICTORS = [
            ("Night temp deviation", sm.get("temp_deviation", {}), 0),
            ("Daytime stress (Oura)", sm.get("daytime_stress", {}), 0),
            ("Stress (check-in)", behav_series("stress"), 1),
            ("Sleep hours", sm.get("sleep", {}), 1),
            ("Breathing disturbance", sm.get("breathing_disturbance", {}), 0),
        ]
        for plab, A, lag in ECZ_PREDICTORS:
            if not A:
                continue
            xs, ys = lag_pair(A, ecz, lag)
            r = _pearson(xs, ys, minn=10)
            if r is None:
                continue
            behaviour.append({
                "behaviour": plab, "outcome": "Eczema", "outcome_key": "eczema",
                "label": f"{plab} → {'next-day ' if lag else ''}eczema",
                "r": round(r, 2), "n": len(xs),
                "good_dir": -1, "kind": "behaviour",
                **_sig_fields(r, len(xs)),
            })
    behaviour.sort(key=lambda x: -abs(x["r"]))

    # Recent check-in log — the raw values he recorded (shown in Insights).
    checkins = []
    for d in sorted(bh)[-10:]:
        r = bh[d]
        if not any(k in r for k in ("drink", "clean", "stress", "eczema", "late_meal")):
            continue
        checkins.append({"t": d, "drink": r.get("drink"), "clean": r.get("clean"),
                         "stress": r.get("stress"), "eczema": r.get("eczema"),
                         "late_meal": r.get("late_meal")})
    checkins.reverse()

    return {"correlations": [], "weekday_hrv": weekday_hrv, "drift_per_year": drift,
            "behaviour": behaviour, "checkins": checkins}


def age_assessment(sm: dict = None) -> dict:
    """Latest value for each age-referenced metric + its rating for a 50yo male."""
    sm = sm if sm is not None else _series_map()
    out = {}
    for k in AGE_REF:
        s = sm.get(k, {})
        if not s:
            continue
        latest_day = max(s)
        val = s[latest_day]
        out[k] = {"label": METRIC_LABEL.get(k, k), "value": val, "as_of": latest_day,
                  **(age_rating(k, val) or {})}
    return out


def insights() -> dict:
    sm = _series_map()
    return {
        "bedtime": _oura_tokens_load().get("bedtime"),
        "readiness": readiness(sm),
        "readiness_trend": readiness_trend(sm),
        "activity": activity_score(date.today().isoformat(), sm),
        "baselines": metric_baselines(90, sm),
        "training_load": training_load(sm),
        "patterns": correlations(sm),
        "significant_changes": significant_changes(sm),
        "age_assessment": age_assessment(sm),
        "compliance": compliance(),
        "scores": score_series(35, sm),
        "activity_scores": activity_series(35, sm),
        "food_sleep": _safe(food_sleep_effects),
    }


def _safe(fn):
    """One failing insight must not take the whole Insights payload down."""
    try:
        return fn()
    except Exception as e:
        print(f"[insights] {fn.__name__} failed: {e}")
        return None


BEHAV_FILE = os.path.join(DATA, "behaviours.json")
BEHAV_HELPERS = {
    "drink": "input_boolean.had_a_drink",
    "clean": "input_boolean.ate_clean",
    "stress": "input_number.work_stress",
}


def _load_behav() -> dict:
    if os.path.exists(BEHAV_FILE):
        with open(BEHAV_FILE) as f:
            return json.load(f)
    return {}


CHECKIN_HELPER = "input_button.checkin_done"


def _save_behav(store):
    tmp = f"{BEHAV_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, BEHAV_FILE)


def snapshot_behaviours(states=None):
    """Record today's behaviour-helper values — unless tonight's entry is already locked."""
    if states is None:
        states = _ha_request("/api/states", quiet=True)
    if not isinstance(states, list):
        return
    store = _load_behav()
    today = date.today().isoformat()
    if store.get(today, {}).get("locked"):
        return                                  # check-in submitted; don't overwrite
    byid = {s.get("entity_id"): s for s in states}
    rec = dict(store.get(today, {}))
    for key, eid in BEHAV_HELPERS.items():
        s = byid.get(eid)
        if not s:
            continue
        st = s.get("state")
        if key == "drink":
            # Off is the default, not an answer: a day only counts as dry once Mark says so
            # (check-in saved or "none today"), so only record the toggle when it's on.
            if st == "on" or rec.get("drinks"):
                rec[key] = 1
            elif not rec.get("dry_confirmed"):
                rec.pop(key, None)
        elif key == "clean":
            rec[key] = 1 if st == "on" else 0
        elif st not in (None, "unknown", "unavailable"):
            try:
                rec[key] = float(st)
            except (TypeError, ValueError):
                pass
    if rec:
        store[today] = rec
        _save_behav(store)


def reset_behaviour_toggles():
    """Turn the toggles back to default so each evening starts fresh."""
    _ha_request("/api/services/input_boolean/turn_off", "POST", {"entity_id": BEHAV_HELPERS["drink"]})
    _ha_request("/api/services/input_boolean/turn_off", "POST", {"entity_id": BEHAV_HELPERS["clean"]})


def finalize_checkin():
    """Lock tonight's behaviour entry and reset the toggles for tomorrow."""
    snapshot_behaviours()                        # capture the current toggle values
    store = _load_behav()
    today = date.today().isoformat()
    rec = store.setdefault(today, {})
    rec["locked"] = True
    rec["checked_at"] = datetime.now().isoformat(timespec="seconds")
    _save_behav(store)
    reset_behaviour_toggles()
    push_ha_extras()
    print(f"[checkin] locked {today}: {rec}")


# Check-in fields: binary behaviours + scaled fields (stress 0-3, eczema 0-2).
CHECKIN_BOOLS = ("drink", "clean", "late_meal")
CHECKIN_SCALES = {"stress": 3, "eczema": 2}


def get_checkin(day: str = None) -> dict:
    """Today's behaviour check-in record (drink/clean/late_meal/late_caffeine + stress + lock)."""
    day = day or date.today().isoformat()
    return _load_behav().get(day, {})


def save_checkin(fields: dict) -> dict:
    """Save the in-app check-in (for the given date, default today) and lock it so the HA snapshot
    won't overwrite it. Best-effort syncs the HA toggles too."""
    day = fields.get("date") or date.today().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(day)):
        day = date.today().isoformat()
    today = day
    store = _load_behav()
    rec = dict(store.get(today, {}))
    for k in CHECKIN_BOOLS:
        if k in fields:
            rec[k] = 1 if fields[k] else 0
    if rec.get("drinks"):
        rec["drink"] = 1                              # logged drinks beat the check-in toggle
    for k, hi in CHECKIN_SCALES.items():
        if k in fields and fields[k] is not None:
            try:
                rec[k] = max(0, min(hi, int(fields[k])))
            except (TypeError, ValueError):
                pass
    rec["locked"] = True
    rec["source"] = "app"
    rec["checked_at"] = datetime.now().isoformat(timespec="seconds")
    store[today] = rec
    _save_behav(store)
    for k in CHECKIN_BOOLS:                           # keep HA toggles consistent (best-effort)
        if k in fields and k in BEHAV_HELPERS:
            svc = "turn_on" if rec[k] else "turn_off"
            _ha_request(f"/api/services/input_boolean/{svc}", "POST",
                        {"entity_id": BEHAV_HELPERS[k]}, quiet=True)
    return rec


# --------------------------------------------------------------------------- #
#  Alcohol — per-drink log, the 30-day reset and the drinking rules.
#  Drinks live on the day's behaviours record (rec["drinks"]); rec["drink"] stays the 0/1
#  flag the correlations and the HA toggle use. A day only counts as dry once Mark says so
#  (check-in saved or "none today") — a blank day is unknown, never dry. The drinking day
#  rolls over at 05:00, so a 00:30 glass belongs to the night before.
# --------------------------------------------------------------------------- #
ALCOHOL_FILE = os.path.join(DATA, "alcohol.json")
ALCOHOL_DEFAULTS = {"reset_start": None, "reset_days": 30, "max_std": 2, "cutoff": "20:00",
                    "dry_weekdays": [0, 1, 2, 3]}       # Mon–Thu
# NZ standard drink = 10 g alcohol; 150 ml of 13% wine ≈ 1.5.
DRINK_KINDS = {"wine": {"label": "Wine 150 ml", "std": 1.5}}
DAY_ROLLOVER = "05:00"
_BEHAV_LOCK = threading.Lock()


def _nz_now() -> datetime:
    return datetime.now(weekly_digest.NZ) if weekly_digest.NZ else datetime.now()


def _drinking_day(now: datetime = None) -> date:
    now = now or _nz_now()
    d = now.date()
    return d - timedelta(days=1) if now.strftime("%H:%M") < DAY_ROLLOVER else d


def alcohol_settings() -> dict:
    s = dict(ALCOHOL_DEFAULTS)
    s.update(CONFIG.get("alcohol") or {})
    if os.path.exists(ALCOHOL_FILE):
        try:
            with open(ALCOHOL_FILE) as f:
                s.update(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass
    return s


def save_alcohol_settings(fields: dict) -> dict:
    start = fields.get("reset_start")
    if start is not None and start != "":
        date.fromisoformat(start)                       # ValueError → 400
    cur = {}
    if os.path.exists(ALCOHOL_FILE):
        with open(ALCOHOL_FILE) as f:
            cur = json.load(f)
    cur["reset_start"] = start or None
    with open(ALCOHOL_FILE, "w") as f:
        json.dump(cur, f, indent=2)
    return alcohol_status()


def _drink_day_info(rec: dict, d: date, s: dict) -> dict:
    """One day's drinking status + which rules it broke."""
    rec = rec or {}
    drinks = rec.get("drinks") or []
    std = round(sum(float(x.get("std") or 0) for x in drinks), 1)
    if drinks or rec.get("drink") == 1:
        status = "drink"
    elif rec.get("drink") == 0 and (rec.get("locked") or rec.get("dry_confirmed")):
        status = "dry"
    else:
        status = "unknown"
    broken = []
    if status == "drink":
        if d.weekday() in s["dry_weekdays"]:
            broken.append("weekday")
        if std > s["max_std"]:
            broken.append("over_limit")
        if any(x.get("time") and (x["time"] >= s["cutoff"] or x["time"] < DAY_ROLLOVER)
               for x in drinks):
            broken.append("late")
        rs = s.get("reset_start")
        if rs and 0 <= (d - date.fromisoformat(rs)).days < s["reset_days"]:
            broken.append("reset")
    return {"date": d.isoformat(), "weekday": d.strftime("%a"), "status": status,
            "drinks": drinks, "std": std, "broken": broken}


RULE_LABELS = {"weekday": "Drink on Mon–Thu", "over_limit": "Over 2 standard drinks",
               "late": "Drink after 8pm", "reset": "Drink during the reset"}


def _reset_info(store: dict, s: dict, today: date) -> dict:
    rs = s.get("reset_start")
    if not rs:
        return {"active": False, "start": None}
    start = date.fromisoformat(rs)
    n = (today - start).days + 1
    end = start + timedelta(days=s["reset_days"] - 1)
    drink_days = [d.isoformat() for d in (start + timedelta(i) for i in range(max(0, min(n, s["reset_days"]))))
                  if _drink_day_info(store.get(d.isoformat()), d, s)["status"] == "drink"]
    # Streak of confirmed dry days ending today (or yesterday while today is still open).
    streak, d = 0, today
    if _drink_day_info(store.get(d.isoformat()), d, s)["status"] == "unknown":
        d -= timedelta(days=1)
    while _drink_day_info(store.get(d.isoformat()), d, s)["status"] == "dry":
        streak += 1
        d -= timedelta(days=1)
    return {"active": 1 <= n <= s["reset_days"], "start": rs, "end": end.isoformat(),
            "day": max(0, min(n, s["reset_days"])), "days": s["reset_days"],
            "complete": n > s["reset_days"], "drink_days": drink_days, "streak": streak}


def alcohol_status(day: str = None, store: dict = None, settings: dict = None,
                   today: date = None) -> dict:
    """The day's drinks, its Mon–Sun week and the reset — everything the card and review need."""
    store = _load_behav() if store is None else store
    s = alcohol_settings() if settings is None else settings
    today = today or _drinking_day()
    d = date.fromisoformat(day) if day else today
    mon = d - timedelta(days=d.weekday())
    week = []
    for i in range(7):
        x = mon + timedelta(days=i)
        info = _drink_day_info(store.get(x.isoformat()), x, s)
        if x > today:
            info["status"] = "future"
        week.append(info)
    seen = [w for w in week if w["status"] != "future"]
    broken = [{"date": w["date"], "rule": r, "label": RULE_LABELS[r]} for w in seen for r in w["broken"]]
    return {"date": d.isoformat(), "today": today.isoformat(),
            "day": _drink_day_info(store.get(d.isoformat()), d, s),
            "week": {"start": mon.isoformat(), "days": week,
                     "std": round(sum(w["std"] for w in seen), 1),
                     "drink_days": sum(w["status"] == "drink" for w in seen),
                     "dry_days": sum(w["status"] == "dry" for w in seen),
                     "unknown_days": sum(w["status"] == "unknown" for w in seen),
                     "broken": broken},
            "reset": _reset_info(store, s, today),
            "rules": {k: s[k] for k in ("max_std", "cutoff", "dry_weekdays", "reset_days")},
            "kinds": DRINK_KINDS}


def _backup_behav_once():
    """Keep the pre-drink-log behaviours.json before the first new-format write."""
    dst = os.path.join(BACKUPS, "behaviours-before-drink-log.json")
    if os.path.exists(BEHAV_FILE) and not os.path.exists(dst):
        with open(BEHAV_FILE) as f, open(dst, "w") as g:
            g.write(f.read())


def _sync_drink_toggle(day: str, on: bool):
    if day == date.today().isoformat():
        svc = "turn_on" if on else "turn_off"
        _ha_request(f"/api/services/input_boolean/{svc}", "POST",
                    {"entity_id": BEHAV_HELPERS["drink"]}, quiet=True)


def add_drink(fields: dict) -> dict:
    """Log one drink. `id` (optional) makes retries idempotent; `dry_run` returns the status the
    day would have without saving (used by the connector's preview)."""
    now = _nz_now()
    t = str(fields.get("time") or now.strftime("%H:%M"))
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", t):
        raise ValueError("time must be HH:MM")
    if fields.get("date"):
        day = date.fromisoformat(fields["date"])
    else:
        day, nowt = _drinking_day(now), now.strftime("%H:%M")
        if nowt >= DAY_ROLLOVER and (t < DAY_ROLLOVER or t > nowt):
            day = now.date() - timedelta(days=1)  # a time not reached yet today was last night
    kind = fields.get("kind") or "wine"
    std = fields.get("std")
    if std is None:
        if kind not in DRINK_KINDS:
            raise ValueError(f"unknown drink kind {kind!r}; pass std")
        std = DRINK_KINDS[kind]["std"]
    std = float(std)
    if not 0 <= std <= 10:
        raise ValueError("std must be 0–10")
    did = str(fields.get("id") or uuid.uuid4().hex[:8])
    if not re.fullmatch(r"[a-f0-9]{8,32}", did):
        raise ValueError("id must be 8–32 hex characters")
    drink = {"id": did, "time": t, "kind": kind, "std": round(std, 1)}
    if fields.get("note"):
        drink["note"] = str(fields["note"])[:200]
    if fields.get("dry_run"):
        store = json.loads(json.dumps(_load_behav()))
        rec = store.setdefault(day.isoformat(), {})
        rec.setdefault("drinks", []).append(drink)
        rec["drink"] = 1
        return {**alcohol_status(day.isoformat(), store), "drink": drink, "dry_run": True}
    with _BEHAV_LOCK:
        _backup_behav_once()
        store = _load_behav()
        rec = store.setdefault(day.isoformat(), {})
        if any(x.get("id") == did for x in rec.get("drinks") or []):
            return {**alcohol_status(day.isoformat(), store), "drink": drink, "already_applied": True}
        rec.setdefault("drinks", []).append(drink)
        rec["drinks"].sort(key=lambda x: (x["time"] < DAY_ROLLOVER, x["time"]))
        rec["drink"] = 1
        rec.pop("dry_confirmed", None)
        _save_behav(store)
    _sync_drink_toggle(day.isoformat(), True)
    return {**alcohol_status(day.isoformat()), "drink": drink}


def remove_drink(day: str, drink_id: str) -> dict:
    date.fromisoformat(day)
    with _BEHAV_LOCK:
        store = _load_behav()
        rec = store.get(day) or {}
        drinks = [x for x in rec.get("drinks") or [] if x.get("id") != drink_id]
        if len(drinks) == len(rec.get("drinks") or []):
            raise ValueError("no such drink")
        if drinks:
            rec["drinks"] = drinks
        else:
            rec.pop("drinks", None)
            rec.pop("drink", None)                # back to unknown until Mark answers again
        store[day] = rec
        _save_behav(store)
    if not drinks:
        _sync_drink_toggle(day, False)
    return alcohol_status(day)


def mark_no_drinks(day: str = None) -> dict:
    day = day or _drinking_day().isoformat()
    date.fromisoformat(day)
    with _BEHAV_LOCK:
        _backup_behav_once()
        store = _load_behav()
        rec = store.setdefault(day, {})
        if rec.get("drinks"):
            raise ValueError("drinks are logged for that day — remove them first")
        rec["drink"] = 0
        rec["dry_confirmed"] = True
        _save_behav(store)
    _sync_drink_toggle(day, False)
    return alcohol_status(day)


def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.mean(xs), 1) if xs else None


def weekly_review(end: str = None, store: dict = None, food: dict = None, sm: dict = None,
                  settings: dict = None, today: date = None) -> dict:
    """The Sunday review for the Mon–Sun week containing `end` (default: this week so far)."""
    store = _load_behav() if store is None else store
    food = _load_food() if food is None else food
    sm = _series_map() if sm is None else sm
    s = alcohol_settings() if settings is None else settings
    today = today or _drinking_day()
    e = date.fromisoformat(end) if end else today
    alc = alcohol_status(e.isoformat(), store, s, today)
    mon = date.fromisoformat(alc["week"]["start"])
    days = [mon + timedelta(days=i) for i in range(7) if mon + timedelta(days=i) <= today]

    # ZOE rules on the days food was logged.
    logged = [zoe_day(d.isoformat(), food, {}) for d in days
              if (food.get(d.isoformat()) or {}).get("items")]

    # Weight: this week's mean vs last week's.
    wt = sm.get("weight", {})
    this_w = _mean([wt.get(d.isoformat()) for d in days])
    last_w = _mean([wt.get((mon - timedelta(days=7 - i)).isoformat()) for i in range(7)])

    # Sleep/HRV the morning after drinking vs dry nights, over the 30 days to `e`.
    after = {"drink": [], "dry": []}
    for i in range(30):
        d = e - timedelta(days=i)
        st = _drink_day_info(store.get(d.isoformat()), d, s)["status"]
        nxt = (d + timedelta(days=1)).isoformat()
        hrv, slp = sm.get("hrv", {}).get(nxt), sm.get("sleep", {}).get(nxt)
        if st in after and (hrv is not None or slp is not None):
            after[st].append((hrv, slp))
    sleep_cmp = {st: {"nights": len(v), "hrv": _mean([x[0] for x in v]),
                      "sleep_h": _mean([x[1] for x in v])} for st, v in after.items()}

    wk = alc["week"]
    broken_by_rule = {}
    for b in wk["broken"]:
        broken_by_rule.setdefault(b["rule"], []).append(b["date"])
    reset = alc["reset"]
    rules = []
    if reset.get("active"):
        rules.append({"rule": "No drinks (30-day reset)", "kept": "reset" not in broken_by_rule,
                      "dates": broken_by_rule.get("reset", [])})
    for r in ("weekday", "over_limit", "late"):
        rules.append({"rule": {"weekday": "Mon–Thu dry", "over_limit": "Max 2 standard drinks",
                               "late": "Nothing after 8pm"}[r],
                      "kept": r not in broken_by_rule, "dates": broken_by_rule.get(r, [])})
    rules.append({"rule": "Every day logged (drinks)", "kept": wk["unknown_days"] == 0,
                  "dates": [w["date"] for w in wk["days"] if w["status"] == "unknown"]})
    return {"week_start": mon.isoformat(), "week_end": (mon + timedelta(days=6)).isoformat(),
            "complete": today >= mon + timedelta(days=6),
            "rules": rules, "reset": reset,
            "alcohol": {k: wk[k] for k in ("std", "drink_days", "dry_days", "unknown_days")},
            "zoe": {"days_logged": len(logged), "avg_score": _mean([z["score"] for z in logged]),
                    "plants_week": logged[-1]["plants_week"] if logged else 0,
                    "avg_fibre": _mean([z["fibre"] for z in logged]),
                    "days_3_ferments": sum(z["ferments"] >= zoe_rules.FERMENTS_DAY for z in logged),
                    "avg_processing": _mean([z["processing_score"] for z in logged]),
                    "days_processed": sum(bool(z["risky_items"]) for z in logged)},
            "weight": {"this_week": this_w, "last_week": last_w,
                       "change": round(this_w - last_w, 1) if this_w and last_w else None},
            "sleep_after": sleep_cmp}


# --------------------------------------------------------------------------- #
#  Food & drink → that night. Each evening's food and drinks against the next night's
#  Oura numbers (Oura files a night under the morning it ends). Only exposures with at
#  least FS_MIN_NIGHTS nights on each side are called "enough".
# --------------------------------------------------------------------------- #
FS_MIN_NIGHTS = 3
FOOD_SLEEP_EXPOSURES = [("drinks", "Drinks"), ("late_meal", "Ate within 3 h of bed"),
                        ("high_carb", "Carbs over target"), ("over_cal", "Calories 10%+ over target"),
                        ("processed", "Medium/high-risk processed food"), ("fibre_30", "30 g+ fibre")]
# (key, label, good direction, unit)
FOOD_SLEEP_OUTCOMES = [("sleep_score", "Sleep score", 1, ""), ("hrv", "HRV", 1, "ms"),
                       ("resting_hr", "Lowest heart rate", -1, "bpm"),
                       ("hr_low_after_bed", "Heart rate bottomed out", -1, "h after bed"),
                       ("hrv_late_vs_early", "HRV recovery (2nd half vs 1st)", 1, "ms"),
                       ("sleep_latency", "Time to fall asleep", -1, "min"),
                       ("deep", "Deep sleep", 1, "h")]


def _night_value(metrics: dict, day: str, key: str):
    m = (metrics.get(day) or {}).get("sleep" if key == "deep" else key)
    if not isinstance(m, dict):
        return None
    v = m.get("deep") if key == "deep" else _metric_value(key, m)
    return v if isinstance(v, (int, float)) else None


def _evening_exposures(day: str, behav: dict, food: dict, s: dict, metrics: dict = None) -> dict:
    """What happened on evening `day`: True / False, or None when it wasn't logged."""
    d = date.fromisoformat(day)
    st = _drink_day_info(behav.get(day), d, s)["status"]
    out = {"drinks": {"drink": True, "dry": False}.get(st)}
    rec = behav.get(day) or {}
    out["late_meal"] = bool(rec["late_meal"]) if rec.get("locked") and "late_meal" in rec else None
    items = (food.get(day) or {}).get("items") or []
    timing = meal_timing(day, food, metrics) if any(i.get("at") for i in items) else {}
    if "late" in timing:                               # logged times beat the check-in toggle
        out["late_meal"] = timing["late"]
    if items:
        t = (food.get(day) or {}).get("totals") or {}
        z = zoe_rules.day_eval(items, items, 0)
        out["high_carb"] = (t.get("carbs") or 0) > FOOD_TARGETS["carbs"]
        out["over_cal"] = (t.get("cal") or 0) > FOOD_TARGETS["cal"] * 1.1
        out["processed"] = bool(z["risky_items"]) if z["processing_score"] is not None else None
        out["fibre_30"] = z["fibre"] >= zoe_rules.FIBRE_DAY if "fibre" in z["parts"] else None
    else:
        out.update({k: None for k in ("high_carb", "over_cal", "processed", "fibre_30")})
    return out


def food_sleep_effects(days: int = 90, behav: dict = None, food: dict = None, metrics: dict = None,
                       settings: dict = None, today: date = None) -> dict:
    behav = _load_behav() if behav is None else behav
    food = _load_food() if food is None else food
    metrics = _load_metrics_store() if metrics is None else metrics
    s = alcohol_settings() if settings is None else settings
    today = today or _nz_now().date()
    rows = []                                          # (exposures, outcomes) per evening
    for i in range(1, days + 1):
        ev = today - timedelta(days=i)
        night = (ev + timedelta(days=1)).isoformat()
        outs = {k: _night_value(metrics, night, k) for k, *_ in FOOD_SLEEP_OUTCOMES}
        if any(v is not None for v in outs.values()):
            rows.append((_evening_exposures(ev.isoformat(), behav, food, s, metrics), outs))
    exposures = []
    for ek, elabel in FOOD_SLEEP_EXPOSURES:
        outcomes = []
        for ok, olabel, good, unit in FOOD_SLEEP_OUTCOMES:
            yes = [o[ok] for e, o in rows if e[ek] is True and o[ok] is not None]
            no = [o[ok] for e, o in rows if e[ek] is False and o[ok] is not None]
            enough = len(yes) >= FS_MIN_NIGHTS and len(no) >= FS_MIN_NIGHTS
            entry = {"key": ok, "label": olabel, "unit": unit, "good_dir": good,
                     "n_yes": len(yes), "n_no": len(no), "enough": enough}
            if yes and no:
                my, mn = statistics.mean(yes), statistics.mean(no)
                sd = statistics.pstdev(yes + no)
                entry.update({"yes": round(my, 2), "no": round(mn, 2), "diff": round(my - mn, 2),
                              "effect": round((my - mn) / sd * good, 2) if sd else 0.0})
            outcomes.append(entry)
        n_yes = sum(e[ek] is True for e, _ in rows)
        n_no = sum(e[ek] is False for e, _ in rows)
        exposures.append({"key": ek, "label": elabel, "nights_yes": n_yes, "nights_no": n_no,
                          "enough": n_yes >= FS_MIN_NIGHTS and n_no >= FS_MIN_NIGHTS,
                          "outcomes": outcomes})
    last = next((d for d in sorted(metrics, reverse=True) if _night_value(metrics, d, "hr_low_hour") is not None), None)
    last_night = ({"date": last, **{k: _night_value(metrics, last, k) for k in
                  ("hr_low_hour", "hr_low_after_bed", "hrv_late_vs_early", "sleep_latency", "awake_time")}}
                  if last else None)
    return {"window_days": days, "nights": len(rows), "min_nights": FS_MIN_NIGHTS,
            "exposures": exposures, "last_night": last_night}


# --------------------------------------------------------------------------- #
#  Habits: the few things Mark already knows matter. Each is detected from data
#  where possible (workouts, food log, drinks, Oura) and is one tap otherwise.
#  Only the habit slipping most this week gets attention, with a concrete
#  recovery action instead of a broken streak.
# --------------------------------------------------------------------------- #
HABITS_FILE = os.path.join(DATA, "habits.json")
DEFAULT_HABITS = ["zone2", "eat_clean", "no_alcohol"]
# id → (name, period, how it's detected)
HABIT_LIBRARY = {
    "zone2": ("Zone 2", "week", "workout heart rate"),
    "eat_clean": ("Eat clean", "day", "ZOE day score 70+ from your food log"),
    "no_alcohol": ("No alcohol", "day", "drinks log, against your alcohol rules"),
    "kitchen_3h": ("Kitchen 3h", "day", "last meal 3 h before bed"),
    "strength": ("Strength", "week", "workouts"),
    "hiit": ("HIIT", "week", "workouts"),
    "steps": ("Steps", "day", "Oura steps"),
}
CLEAN_SCORE = 70
STEPS_TARGET = 8000
SLIP_THRESHOLD = 0.2        # a habit needs to be this far behind to be called slipping
ZONE2_MAX_DAY = 90          # never ask for more Zone 2 than this in one day
_HABITS_LOCK = threading.Lock()


def load_habits() -> dict:
    h = {}
    if os.path.exists(HABITS_FILE):
        try:
            with open(HABITS_FILE) as f:
                h = json.load(f)
        except (OSError, json.JSONDecodeError):
            h = {}
    h.setdefault("active", list(DEFAULT_HABITS))
    h.setdefault("custom", [])
    h.setdefault("marks", {})
    h.setdefault("actions_done", {})
    return h


def _save_habits(h: dict):
    tmp = f"{HABITS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(h, f, indent=2)
    os.replace(tmp, HABITS_FILE)


def _habit_name(hid: str, h: dict) -> str:
    if hid in HABIT_LIBRARY:
        return HABIT_LIBRARY[hid][0]
    return next((c["name"] for c in h["custom"] if c["id"] == hid), hid)


def habit_day_status(hid: str, day: date, today: date, ctx: dict) -> str:
    """'done' | 'missed' | 'pending' (today, still open) | 'unknown' (no data, not marked)."""
    key, is_today = day.isoformat(), day == today
    mark = (((ctx.get("habits") or {}).get("marks") or {}).get(key) or {}).get(hid)
    open_ = "pending" if is_today else "unknown"
    if hid == "eat_clean":
        score = food_day_score(key, ctx["food"], ctx.get("metrics"))
        if score is None:
            return open_ if mark is None else ("done" if mark else "missed")
        return "done" if score >= CLEAN_SCORE else ("pending" if is_today else "missed")
    if hid == "no_alcohol":
        info = _drink_day_info(ctx["behav"].get(key), day, ctx["alcohol"])
        if info["status"] == "unknown":
            return open_
        return "missed" if info["broken"] else ("done" if info["status"] == "dry" or not is_today else "pending")
    if hid == "kitchen_3h":
        t = meal_timing(key, ctx["food"], ctx["metrics"])
        if "late" not in t:
            return open_
        return "missed" if t["late"] else ("pending" if is_today else "done")
    if hid == "steps":
        v = _night_value(ctx["metrics"], key, "steps")
        if v is None:
            return open_
        return "done" if v >= STEPS_TARGET else ("pending" if is_today else "missed")
    return open_ if mark is None else ("done" if mark else "missed")     # custom: one tap


def _weekly_progress(hid: str, week: dict):
    if hid == "zone2":
        return week["zone2_minutes"], week["targets"]["zone2_minutes"]
    return week[hid], week["targets"][hid]


def habit_gap(statuses: list = None, done=None, target=None, weekday: int = 0) -> float:
    """How far behind a habit is this week, 0 (on pace) to 1. Weekly targets are judged
    against even pace through the week (today half-counted); daily habits by the share of
    known days missed."""
    if target is not None:
        if not target:
            return 0.0
        expected = target * (weekday + 0.5) / 7
        return round(max(0.0, expected - done) / target, 3)
    known = [s for s in statuses if s in ("done", "missed")]
    return round(sum(s == "missed" for s in known) / len(known), 3) if known else 0.0


def recovery_action(hid: str, hb: dict, ctx: dict, today: date) -> dict:
    """A concrete thing to do today for a slipping habit: what, and why it closes the gap."""
    days_left = 7 - today.weekday()
    if hb["period"] == "week":
        remaining = max(0, hb["target"] - hb["done"])
        if hid == "zone2":
            per_day = -(-remaining // days_left)
            plural = "s" if days_left != 1 else ""
            if per_day > ZONE2_MAX_DAY:                 # out of reach: bank what's realistic
                return {"text": f"{ZONE2_MAX_DAY} min easy Zone 2 today", "focus": f"{ZONE2_MAX_DAY} minutes of Zone 2",
                        "why": f"{remaining} min short with {days_left} day{plural} left — the full target is out of "
                               f"reach this week, so bank {ZONE2_MAX_DAY} and start fresh Monday."}
            return {"text": f"{max(per_day, 20)} min easy Zone 2 today",
                    "focus": f"{max(per_day, 20)} minutes of Zone 2",
                    "why": f"{remaining} min to go with {days_left} day{plural} left — {per_day} min/day gets you there."}
        word = "strength session" if hid == "strength" else "HIIT session"
        return {"text": f"Fit in a {word} today", "focus": f"a {word}",
                "why": f"{remaining} left this week with {days_left} day{'s' if days_left != 1 else ''} to go."}
    if hid == "eat_clean":
        f = get_food(today.isoformat())
        gaps = (f.get("zoe") or {}).get("todo") or []
        text = ("Today: " + "; ".join(gaps[:2])) if f.get("items") and gaps else "Build each meal around a mix of plants, plus a fermented food"
        return {"text": text, "focus": "plants, ferments and fibre at every meal",
                "why": f"{hb['missed']} of the last {hb['known']} days fell short of a clean day."}
    if hid == "no_alcohol":
        reset = (alcohol_status(today.isoformat(), ctx["behav"], ctx["alcohol"], today) or {}).get("reset") or {}
        end = date.fromisoformat(reset["end"]) if reset.get("active") else None
        nxt = (f"the reset ends {end:%a} {end.day} {end:%b}" if end
               else "your next drink waits for Friday, before 8pm, two at most")
        return {"text": "Alcohol-free evening tonight", "focus": "an alcohol-free evening",
                "why": f"{hb['missed']} rule-breaking day{'s' if hb['missed'] != 1 else ''} this week; {nxt}."}
    if hid == "kitchen_3h":
        t = meal_timing(today.isoformat(), ctx["food"], ctx["metrics"])
        when = t.get("kitchen_closes") or "3 hours before bed"
        return {"text": f"Kitchen closes {when} tonight", "focus": f"the kitchen closed by {when}",
                "why": f"{hb['missed']} late meal{'s' if hb['missed'] != 1 else ''} this week."}
    if hid == "steps":
        return {"text": "20-minute walk after dinner", "focus": "a 20-minute walk",
                "why": f"Under {STEPS_TARGET:,} steps on {hb['missed']} day{'s' if hb['missed'] != 1 else ''} this week."}
    name = hb["name"]
    return {"text": f"{name} today", "focus": name[0].lower() + name[1:], "why": f"Missed {hb['missed']} day{'s' if hb['missed'] != 1 else ''} this week."}


def _habit_ctx() -> dict:
    return {"habits": load_habits(), "food": _load_food(), "behav": _load_behav(),
            "metrics": _load_metrics_store(), "alcohol": alcohol_settings()}


def habits_week(today: date = None, ctx: dict = None, week: dict = None) -> list:
    """Every active habit for the Mon–Sun week containing today: per-day status, progress,
    how far behind it is, and a recovery action."""
    today = today or _drinking_day()
    ctx = ctx or _habit_ctx()
    week = week or training_week(today.isoformat())
    mon = today - timedelta(days=today.weekday())
    days = [mon + timedelta(days=i) for i in range(7)]
    out = []
    for hid in ctx["habits"]["active"]:
        name = _habit_name(hid, ctx["habits"])
        period = HABIT_LIBRARY.get(hid, (None, "day"))[1]
        if period == "week":
            done, target = _weekly_progress(hid, week)
            per_day = {d.isoformat(): 0 for d in days}
            for s in week["sessions"]:
                if s["date"] in per_day:
                    per_day[s["date"]] += s["zone2"] if hid == "zone2" else (s["kind"] == hid)
            hb = {"id": hid, "name": name, "period": "week", "done": done, "target": target,
                  "days": [{"date": d, "value": v, "status": "future" if d > today.isoformat() else
                            ("done" if v else "none")} for d, v in per_day.items()],
                  "gap": habit_gap(done=done, target=target, weekday=today.weekday()),
                  "today": "done" if per_day[today.isoformat()] else "pending"}
        else:
            sts = [habit_day_status(hid, d, today, ctx) if d <= today else "future" for d in days]
            past = sts[:today.weekday()] + ([sts[today.weekday()]] if sts[today.weekday()] in ("done", "missed") else [])
            known = [s for s in past if s in ("done", "missed")]
            hb = {"id": hid, "name": name, "period": "day", "days": [{"date": d.isoformat(), "status": s} for d, s in zip(days, sts)],
                  "done": sum(s == "done" for s in known), "missed": sum(s == "missed" for s in known), "known": len(known),
                  "gap": habit_gap(statuses=past), "today": sts[today.weekday()]}
        out.append(hb)
    return out


def zone2_progress(week: dict, today: date) -> dict:
    target, done = week["targets"]["zone2_minutes"], week["zone2_minutes"]
    days_left = 7 - today.weekday()
    remaining = max(0, target - done)
    past4 = [min(1.0, h["zone2_minutes"] / target) for h in week["history"][:4]] if target else []
    return {"minutes": done, "target": target, "days_left": days_left, "remaining": remaining,
            "per_day": -(-remaining // days_left) if remaining else 0,
            "adherence_4w": round(100 * statistics.mean(past4)) if past4 else None,
            "range": week["zone2_hr"]}


def readiness_phrase(score) -> str:
    if score is None:
        return ""
    return ("Strong recovery." if score >= 85 else "Good recovery." if score >= 70
            else "Fair recovery — keep it easy." if score >= 60 else "Low recovery — rest counts today.")


def today_plan(today: date = None, ctx: dict = None, week: dict = None) -> dict:
    """Everything the Today screen shows: readiness, one headline, habit row, Zone 2 pace,
    up to three actions, and the evening questions that can't be inferred."""
    today = today or _drinking_day()
    ctx = ctx or _insight_ctx(today)
    week = week or training_week(today.isoformat())
    habits = habits_week(today, ctx, week)
    slipping = sorted((h for h in habits if h["gap"] >= SLIP_THRESHOLD and h["today"] != "done"),
                      key=lambda h: -h["gap"])
    for h in habits:
        h["slipping"] = bool(slipping) and h is slipping[0]
    key = today.isoformat()
    done_keys = set(ctx["habits"]["actions_done"].get(key) or [])
    actions = []
    focus = None
    if slipping:
        top = slipping[0]
        focus = {**recovery_action(top["id"], top, ctx, today), "habit": top["id"], "name": top["name"]}
        actions.append({"key": f"recover:{top['id']}", "text": focus["text"], "why": focus["why"], "habit": top["id"],
                        "done": f"recover:{top['id']}" in done_keys or top["today"] == "done"})
    try:
        exp = experiments_view(today, ctx)["active"]
    except Exception as e:                              # experiments are extra; never break Today
        print(f"[today] experiments failed: {e}")
        exp = None
    if exp and not exp["started"]:
        actions.append({"key": "exp", "text": f"Experiment starts {date.fromisoformat(exp['start']):%A}: {exp['name']}",
                        "why": f"{exp['days']} days, then a before/after against your last 4 weeks.", "done": False})
    elif exp:
        manual = exp["detector"] == "manual"
        actions.append({"key": "exp", "text": f"Experiment · day {exp['day']} of {exp['days']}: {exp['name']}",
                        "why": (f"{exp['days_done']} of {exp['days_elapsed']} days so far" if exp["days_elapsed"]
                                else "Day 1 — counting starts tonight") + ("" if manual else " · tracked automatically"),
                        "done": exp["today"] == "done", "exp": manual})
    for h in habits:                                    # one-tap custom habits still open today
        if h["id"] not in HABIT_LIBRARY and h["today"] == "pending" and len(actions) < 3:
            actions.append({"key": f"habit:{h['id']}", "text": h["name"], "habit": h["id"], "tap": True, "done": False})
    if today.weekday() == 6 and "review" not in done_keys:   # gone once the review has been opened
        actions.append({"key": "review", "text": "Your weekly review is ready", "why": "Two minutes: see the week and choose next week's actions.",
                        "review": True, "done": False})
    readiness = _night_value(ctx["metrics"], key, "readiness_score")
    lead = readiness_phrase(readiness)
    if focus:
        others = [h["name"] for h in slipping[1:]]
        headline = {"lead": lead, "focus": f"One thing matters today: {focus['focus']}.",
                    "detail": focus["why"] + (" Also behind: " + ", ".join(others) + "." if others else
                                              " It's the only habit slipping.")}
    else:
        headline = {"lead": lead, "focus": "Everything's on track — keep today simple.", "detail": ""}
    return {"date": key, "readiness": readiness, "headline": headline, "habits": habits,
            "zone2": zone2_progress(week, today) if "zone2" in ctx["habits"]["active"] else None,
            "actions": actions[:4], "checkin": checkin_questions(today, ctx, habits) + (
                [{"id": "exp", "text": f"{exp['name']} today?"}] if exp and exp["detector"] == "manual" and exp["today"] == "pending" else []),
            "week_plan": week_plan(today)}


def checkin_questions(today: date, ctx: dict, habits: list) -> list:
    """The evening check-in: only what the logs can't answer."""
    qs = []
    if _drink_day_info(ctx["behav"].get(today.isoformat()), today, ctx["alcohol"])["status"] == "unknown":
        qs.append({"id": "drinks", "text": "Any drinks today?"})
    for h in habits:
        if h["today"] != "pending":
            continue
        if h["id"] == "eat_clean" and not (ctx["food"].get(today.isoformat()) or {}).get("items"):
            qs.append({"id": "eat_clean", "text": "Did you eat clean today?"})
        elif h["id"] not in HABIT_LIBRARY:
            qs.append({"id": h["id"], "text": f"{h['name']} today?"})
    return qs


def mark_habit(hid: str, value, day: str = None) -> dict:
    day = day or _drinking_day().isoformat()
    date.fromisoformat(day)
    with _HABITS_LOCK:
        h = load_habits()
        if hid != "eat_clean" and not any(c["id"] == hid for c in h["custom"]):
            raise ValueError("only Eat clean (on days without a food log) and your own habits take a tap")
        marks = h["marks"].setdefault(day, {})
        if value is None:
            marks.pop(hid, None)
        else:
            marks[hid] = bool(value)
        _save_habits(h)
    return today_plan()


def mark_action_done(key: str, day: str = None) -> dict:
    day = day or _drinking_day().isoformat()
    with _HABITS_LOCK:
        h = load_habits()
        done = h["actions_done"].setdefault(day, [])
        if key not in done:
            done.append(key)
        for old in sorted(h["actions_done"])[:-30]:
            h["actions_done"].pop(old)
        _save_habits(h)
    return today_plan()


def configure_habits(fields: dict) -> dict:
    """Choose active habits from the library, add a one-tap habit, or remove one."""
    with _HABITS_LOCK:
        h = load_habits()
        if fields.get("add_custom"):
            name = str(fields["add_custom"]).strip()[:40]
            if not name:
                raise ValueError("name the habit")
            cid = "c_" + uuid.uuid4().hex[:8]
            h["custom"].append({"id": cid, "name": name})
            h["active"].append(cid)
        if fields.get("remove_custom"):
            h["custom"] = [c for c in h["custom"] if c["id"] != fields["remove_custom"]]
            h["active"] = [a for a in h["active"] if a != fields["remove_custom"]]
        if "active" in fields:
            valid = set(HABIT_LIBRARY) | {c["id"] for c in h["custom"]}
            active = [a for a in fields["active"] if a in valid]
            if not active:
                raise ValueError("keep at least one habit active")
            h["active"] = list(dict.fromkeys(active))
        _save_habits(h)
    return {"active": h["active"], "custom": h["custom"],
            "library": [{"id": k, "name": v[0], "period": v[1], "detected": v[2]} for k, v in HABIT_LIBRARY.items()]}


# --------------------------------------------------------------------------- #
#  Ranked insights. Each evening becomes a handful of behaviour signals; each is
#  compared with the next night's Oura numbers. A candidate is kept only with
#  8+ nights on both sides, a non-trivial effect, and a specific action for the
#  direction it points. One headline a week; a known insight only comes back
#  when it has materially changed.
# --------------------------------------------------------------------------- #
INSIGHTS_STATE_FILE = os.path.join(DATA, "insights_state.json")
INSIGHT_MIN_NIGHTS = 8
INSIGHT_MIN_EFFECT = 0.3          # |Cohen's d| below this is too small to act on
INSIGHT_MIN_CONFIDENCE = 0.9      # 1 - p; below this the difference could easily be chance
INSIGHT_CHANGE = 0.3              # a surfaced insight returns only if its effect moved 30%+ or flipped
INSIGHT_WINDOW = 120              # days of history considered

SIGNALS = {"alcohol": "drinks", "late_meal": "a late meal", "heavy_meal": "a heavy day of eating",
           "high_carb": "a high-carb day", "processed": "medium/high-risk processed food", "low_fibre": "a low-fibre day",
           "zone2_day": "a Zone 2 session", "hiit_day": "a HIIT session", "strength_day": "a strength session",
           "late_bed": "a late bedtime"}
# key, label, good direction, unit, decimals
OUTCOMES = [("hrv", "HRV", 1, "ms", 0), ("resting_hr", "lowest heart rate", -1, "bpm", 0),
            ("deep", "deep sleep", 1, "min", 0), ("efficiency", "sleep efficiency", 1, "%", 0),
            ("sleep_score", "sleep score", 1, "", 0), ("hr_low_after_bed", "heart-rate low", -1, "h after bed", 1)]
# (signal, "worse" | "better") → the action that follows. No entry, no insight.
INSIGHT_ACTIONS = {
    ("alcohol", "worse"): "Keep drinks to Friday or Saturday, before 8pm — or skip them on nights before a hard day.",
    ("late_meal", "worse"): "Close the kitchen 3 hours before bed — switch on the Kitchen 3h habit.",
    ("heavy_meal", "worse"): "Make dinner the light meal: protein and veg, no second helping.",
    ("high_carb", "worse"): "Swap the evening starch for another cup of veg.",
    ("processed", "worse"): "Swap the items tagged medium/high risk for whole-food versions — the food log shows which.",
    ("low_fibre", "worse"): "Get to 15 g fibre by lunch — legumes, wholegrains, nuts and veg — so dinner isn't carrying the day.",
    ("zone2_day", "better"): "Put Zone 2 on the days before nights that matter — it's helping your sleep.",
    ("strength_day", "better"): "Keep your strength sessions — your nights after them are better.",
    ("strength_day", "worse"): "Move strength sessions earlier in the day to give your body time to settle.",
    ("hiit_day", "worse"): "Do HIIT before midday so your heart rate settles before bed.",
    ("hiit_day", "better"): "Your body handles HIIT well — keep the weekly session.",
    ("late_bed", "worse"): "Pick a lights-out time and set a phone reminder 30 minutes before.",
}


def _night_metric(metrics: dict, day: str, key: str):
    if key == "deep":
        v = _night_value(metrics, day, "deep")
        return round(v * 60) if v is not None else None
    if key == "efficiency":
        m = (metrics.get(day) or {}).get("sleep")
        v = m.get("efficiency") if isinstance(m, dict) else None
        return v if isinstance(v, (int, float)) else None
    return _night_value(metrics, day, key)


def behaviour_signals(day: str, ctx: dict) -> dict:
    """Collapse one evening into yes/no behaviour signals (None = can't tell)."""
    d = date.fromisoformat(day)
    out = dict.fromkeys(SIGNALS)
    drink = _drink_day_info(ctx["behav"].get(day), d, ctx["alcohol"])["status"]
    out["alcohol"] = {"drink": True, "dry": False}.get(drink)
    items = (ctx["food"].get(day) or {}).get("items") or []
    if items:
        t = (ctx["food"].get(day) or {}).get("totals") or {}
        z = zoe_rules.day_eval(items, items, 0)
        timing = meal_timing(day, ctx["food"], ctx["metrics"])
        out["late_meal"] = timing.get("late")
        out["heavy_meal"] = (t.get("cal") or 0) > ctx["cal_median"] * 1.2 if ctx.get("cal_median") else None
        out["high_carb"] = (t.get("carbs") or 0) > FOOD_TARGETS["carbs"]
        out["processed"] = bool(z["risky_items"]) if z["processing_score"] is not None else None
        out["low_fibre"] = z["fibre"] < 20 if "fibre" in z["parts"] else None
    sess = ctx["sessions"].get(day, [])
    if ctx["has_workouts"]:
        out["zone2_day"] = sum(s["zone2"] for s in sess) >= 30
        out["hiit_day"] = any(s["kind"] == "hiit" for s in sess)
        out["strength_day"] = any(s["kind"] == "strength" for s in sess)
    bed = _night_value(ctx["metrics"], (d + timedelta(days=1)).isoformat(), "bedtime_hour")
    if bed is not None and ctx.get("bed_median") is not None:
        out["late_bed"] = bed > ctx["bed_median"] + 0.75
    return out


def _welch(yes: list, no: list):
    """Cohen's d (pooled SD) and a two-sided p from Welch's t (normal approximation)."""
    my, mn = statistics.mean(yes), statistics.mean(no)
    vy, vn = statistics.variance(yes), statistics.variance(no)
    pooled = math.sqrt(((len(yes) - 1) * vy + (len(no) - 1) * vn) / (len(yes) + len(no) - 2))
    se = math.sqrt(vy / len(yes) + vn / len(no))
    d = (my - mn) / pooled if pooled else 0.0
    p = math.erfc(abs((my - mn) / se) / math.sqrt(2)) if se else (0.0 if my != mn else 1.0)
    return my, mn, d, p


def insight_candidates(rows: list) -> list:
    """rows: [(signals, outcomes)] per evening → ranked candidates that pass the bar."""
    out = []
    for sig, slabel in SIGNALS.items():
        for ok, olabel, good, unit, dec in OUTCOMES:
            yes = [o[ok] for s, o in rows if s.get(sig) is True and o.get(ok) is not None]
            no = [o[ok] for s, o in rows if s.get(sig) is False and o.get(ok) is not None]
            if len(yes) < INSIGHT_MIN_NIGHTS or len(no) < INSIGHT_MIN_NIGHTS:
                continue
            my, mn, d, p = _welch(yes, no)
            effect = d * good                           # + = better after the behaviour
            if abs(effect) < INSIGHT_MIN_EFFECT or 1 - p < INSIGHT_MIN_CONFIDENCE:
                continue
            direction = "better" if effect > 0 else "worse"
            action = INSIGHT_ACTIONS.get((sig, direction))
            if not action:
                continue
            diff = my - mn
            amount = f"{abs(diff):.{dec}f} {unit}".strip()
            if ok == "hr_low_after_bed":
                change = f"your heart rate bottoms out {abs(diff):.1f} h {'later' if diff > 0 else 'earlier'}"
            else:
                change = f"your {olabel} is {amount} {'higher' if diff > 0 else 'lower'}"
            out.append({"key": f"{sig}:{ok}", "signal": sig, "outcome": ok, "direction": direction,
                        "effect": round(effect, 2), "confidence": round(1 - p, 2),
                        "score": round(abs(effect) * (1 - p), 3),
                        "yes": round(my, dec), "no": round(mn, dec), "n_yes": len(yes), "n_no": len(no),
                        "text": f"After {slabel}, {change} ({round(my, dec):g} vs {round(mn, dec):g}{(' ' + unit) if unit and ok != 'hr_low_after_bed' else ''}).",
                        "basis": f"{len(yes)} nights with, {len(no)} without",
                        "action": action})
    return sorted(out, key=lambda c: -c["score"])


def _insight_rows(ctx: dict, days: int, today: date) -> list:
    rows = []
    for i in range(1, days + 1):
        ev = today - timedelta(days=i)
        night = (ev + timedelta(days=1)).isoformat()
        outs = {k: _night_metric(ctx["metrics"], night, k) for k, *_ in OUTCOMES}
        if any(v is not None for v in outs.values()):
            rows.append((behaviour_signals(ev.isoformat(), ctx), outs))
    return rows


def _insight_ctx(today: date) -> dict:
    ctx = _habit_ctx()
    cals = [((ctx["food"].get(d) or {}).get("totals") or {}).get("cal") for d in ctx["food"]]
    cals = [c for c in cals if c]
    beds = [v for v in (_night_value(ctx["metrics"], (today - timedelta(days=i)).isoformat(), "bedtime_hour")
                        for i in range(28)) if v is not None]
    store = _load_workouts_file()
    lo, hi = training_settings()["zone2_hr"]
    sessions = {}
    for w in _dedupe_sessions([_effective(w) for w in store.values() if not w.get("ignored")]):
        z2, _ = _session_zone2(w, lo, hi)
        sessions.setdefault(w["date"], []).append({"kind": KIND_OF_CATEGORY.get(w.get("category"), "other"), "zone2": z2})
    return {**ctx, "cal_median": statistics.median(cals) if cals else None,
            "bed_median": statistics.median(beds) if beds else None,
            "sessions": sessions, "has_workouts": bool(store)}


def _load_insight_state() -> dict:
    try:
        with open(INSIGHTS_STATE_FILE) as f:
            st = json.load(f)
    except (OSError, json.JSONDecodeError):
        st = {}
    st.setdefault("surfaced", {})
    st.setdefault("weekly", {})
    return st


def _materially_changed(then: dict, now: dict) -> bool:
    if (then["effect"] > 0) != (now["effect"] > 0):
        return True
    return abs(now["effect"] - then["effect"]) >= INSIGHT_CHANGE * abs(then["effect"])


def pick_headline(cands: list, state: dict, week: str):
    """This week's headline: keep the one already chosen this week if it still holds; else the
    best candidate that's new, or has materially changed since it was last surfaced."""
    by_key = {c["key"]: c for c in cands}
    chosen = state["weekly"].get(week)
    if chosen in by_key:
        return by_key[chosen], False
    for c in cands:
        then = state["surfaced"].get(c["key"])
        if then is None or _materially_changed(then, c):
            state["weekly"][week] = c["key"]
            state["surfaced"][c["key"]] = {"week": week, "effect": c["effect"]}
            return c, True
    return None, False


def ranked_insights(today: date = None, ctx: dict = None, state: dict = None, save: bool = True) -> dict:
    today = today or _nz_now().date()
    ctx = ctx or _insight_ctx(today)
    cands = insight_candidates(_insight_rows(ctx, INSIGHT_WINDOW, today))
    iso = today.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    state = _load_insight_state() if state is None else state
    head, changed = pick_headline(cands, state, week)
    if changed and save:
        with open(INSIGHTS_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    return {"week": week, "headline": head,
            "others": [c for c in cands if not head or c["key"] != head["key"]],
            "min_nights": INSIGHT_MIN_NIGHTS, "window_days": INSIGHT_WINDOW}


# --------------------------------------------------------------------------- #
#  Experiments: testing what Mark doesn't know yet. One at a time (so its effect
#  can be attributed), 14 days by default, adherence tracked daily (detected where
#  possible, otherwise one tap). At the end: adherence plus the effect on HRV,
#  resting HR, deep sleep, sleep efficiency and the weight trend versus the four
#  weeks before, with a plain-English verdict. History feeds the next suggestion.
# --------------------------------------------------------------------------- #
EXPERIMENTS_FILE = os.path.join(DATA, "experiments.json")
EXP_DEFAULT_DAYS = 14
EXP_BASELINE_DAYS = 28
EXP_MIN_ADHERENCE = 0.6     # below this the result can't be attributed to the experiment
EXP_MIN_NIGHTS = 5
EXP_MIN_EFFECT = 0.3        # |Cohen's d|
EXP_MIN_CONFIDENCE = 0.8    # smaller samples than insights, so a "likely" bar
EXP_WEIGHT_STEP = 0.25      # kg/week change in the weight trend that counts
# preset → (name, detector, habit it can be promoted to)
EXPERIMENT_PRESETS = {
    "dry": ("No alcohol at all", "dry", "no_alcohol"),
    "kitchen_3h": ("Kitchen closes 3 h before bed", "kitchen_3h", "kitchen_3h"),
    "zone2_daily": ("30 min Zone 2 every day", "zone2_daily", "zone2"),
    "bed_2230": ("Lights out by 22:30", "bed_by", None),
    "fibre_30": ("30 g fibre every day", "fibre30", "eat_clean"),
}
# which insight signals point at which experiment
SIGNAL_EXPERIMENT = {"alcohol": "dry", "late_meal": "kitchen_3h", "heavy_meal": "kitchen_3h", "low_fibre": "fibre_30",
                     "high_carb": "fibre_30", "processed": "fibre_30", "late_bed": "bed_2230", "zone2_day": "zone2_daily"}
EXP_METRICS = [("hrv", "HRV", 1, "ms"), ("resting_hr", "resting HR", -1, "bpm"),
               ("deep", "deep sleep", 1, "min"), ("efficiency", "sleep efficiency", 1, "%")]
_EXP_LOCK = threading.Lock()


def load_experiments() -> dict:
    try:
        with open(EXPERIMENTS_FILE) as f:
            e = json.load(f)
    except (OSError, json.JSONDecodeError):
        e = {}
    e.setdefault("active", None)
    e.setdefault("history", [])
    return e


def _save_experiments(e: dict):
    tmp = f"{EXPERIMENTS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(e, f, indent=2)
    os.replace(tmp, EXPERIMENTS_FILE)


def exp_day_status(exp: dict, day: date, today: date, ctx: dict) -> str:
    """'done' | 'missed' | 'pending' (today) | 'unknown' for one experiment day."""
    key, is_today = day.isoformat(), day == today
    open_ = "pending" if is_today else "unknown"
    det = exp["detector"]
    if det == "manual":
        m = (exp.get("marks") or {}).get(key)
        return open_ if m is None else ("done" if m else "missed")
    if det == "dry":
        st = _drink_day_info(ctx["behav"].get(key), day, ctx["alcohol"])["status"]
        return {"dry": "done", "drink": "missed"}.get(st, open_)
    if det == "kitchen_3h":
        return habit_day_status("kitchen_3h", day, today, ctx)
    if det == "zone2_daily":
        mins = sum(s["zone2"] for s in ctx.get("sessions", {}).get(key, []))
        return "done" if mins >= 30 else open_ if is_today else "missed"
    if det == "bed_by":
        bed = _night_value(ctx["metrics"], (day + timedelta(days=1)).isoformat(), "bedtime_hour")
        return open_ if bed is None else ("done" if bed <= 10.5 else "missed")
    if det in ("fibre30", "veg9"):              # veg9: experiments started before 1.21.0
        items = (ctx["food"].get(key) or {}).get("items") or []
        if not any(isinstance(i.get("fibre"), (int, float)) for i in items):
            return open_
        fibre = sum(float(i.get("fibre") or 0) for i in items)
        return "done" if fibre >= zoe_rules.FIBRE_DAY else ("pending" if is_today else "missed")
    return open_


def _slope_per_week(points: list):
    """[(day index, kg)] → least-squares slope in kg/week, or None with under 4 points."""
    if len(points) < 4:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return round(7 * sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den, 2) if den else None


def experiment_result(exp: dict, ctx: dict, today: date) -> dict:
    """Adherence so far and the before/after comparison: nights after adherent days vs the
    nights of the 4 weeks before the start, plus the weight trend in each window."""
    start = date.fromisoformat(exp["start"])
    last = min(today, start + timedelta(days=exp["days"] - 1))
    days = [start + timedelta(days=i) for i in range((last - start).days + 1)]
    statuses = {d.isoformat(): exp_day_status(exp, d, today, ctx) for d in days}
    elapsed = [d for d in days if d < today or statuses[d.isoformat()] in ("done", "missed")]
    done_days = [d for d in elapsed if statuses[d.isoformat()] == "done"]
    adherence = len(done_days) / len(elapsed) if elapsed else 0.0
    metrics = []
    for key, label, good, unit in EXP_METRICS:
        base = [v for v in (_night_metric(ctx["metrics"], (start - timedelta(days=i)).isoformat(), key)
                            for i in range(0, EXP_BASELINE_DAYS)) if v is not None]
        during = [v for v in (_night_metric(ctx["metrics"], (d + timedelta(days=1)).isoformat(), key)
                              for d in done_days) if v is not None]
        m = {"key": key, "label": label, "unit": unit, "n_base": len(base), "n_exp": len(during)}
        if len(base) >= EXP_MIN_NIGHTS and len(during) >= EXP_MIN_NIGHTS:
            my, mb, d, p = _welch(during, base)
            effect = d * good
            m.update({"before": round(mb, 1), "during": round(my, 1), "diff": round(my - mb, 1),
                      "effect": round(effect, 2), "confidence": round(1 - p, 2),
                      "verdict": ("improved" if effect > 0 else "worsened")
                      if abs(effect) >= EXP_MIN_EFFECT and 1 - p >= EXP_MIN_CONFIDENCE else "no clear change"})
        else:
            m["verdict"] = "too few nights"
        metrics.append(m)
    wpts = lambda a, n: [(i, v) for i in range(n) if (v := _night_value(ctx["metrics"], (a + timedelta(days=i)).isoformat(), "weight")) is not None]
    wb = _slope_per_week(wpts(start - timedelta(days=EXP_BASELINE_DAYS), EXP_BASELINE_DAYS))
    we = _slope_per_week(wpts(start, len(days)))
    weight = {"key": "weight", "label": "weight trend", "unit": "kg/week", "before": wb, "during": we,
              "verdict": "too few readings"}
    if wb is not None and we is not None:
        diff = round(we - wb, 2)
        weight.update({"diff": diff, "verdict": "improved" if diff <= -EXP_WEIGHT_STEP else
                       "worsened" if diff >= EXP_WEIGHT_STEP else "no clear change"})
    metrics.append(weight)
    return {"days_elapsed": len(elapsed), "days_done": len(done_days), "adherence": round(adherence, 2),
            "statuses": statuses, "metrics": metrics, **experiment_verdict(adherence, metrics)}


def _fmt_change(m: dict) -> str:
    if m["key"] == "weight":
        return f"weight trend {m['diff']:+.2f} kg/week"
    return f"{m['label']} {m['diff']:+g} {m['unit']}"


def experiment_verdict(adherence: float, metrics: list) -> dict:
    if adherence < EXP_MIN_ADHERENCE:
        return {"verdict": f"Not enough adherence to tell — {round(adherence * 100)}% of days.", "outcome": "inconclusive"}
    up = [m for m in metrics if m["verdict"] == "improved"]
    down = [m for m in metrics if m["verdict"] == "worsened"]
    conf = min((m.get("confidence", 1) for m in up + down), default=1)
    level = "good" if conf >= 0.95 else "moderate"
    if up and not down:
        return {"verdict": f"Probably helped: {', '.join(_fmt_change(m) for m in up)} ({level} confidence).", "outcome": "helped"}
    if down and not up:
        return {"verdict": f"Probably didn't help: {', '.join(_fmt_change(m) for m in down)} ({level} confidence).", "outcome": "hurt"}
    if up and down:
        return {"verdict": f"Mixed: {', '.join(_fmt_change(m) for m in up)}, but {', '.join(_fmt_change(m) for m in down)}.", "outcome": "mixed"}
    if all(m["verdict"].startswith("too few") for m in metrics):
        return {"verdict": "Not enough nights of data to measure an effect.", "outcome": "inconclusive"}
    return {"verdict": "No clear effect on your sleep, heart or weight numbers.", "outcome": "no_effect"}


def _exp_ctx(today: date) -> dict:
    return _insight_ctx(today)


def experiments_view(today: date = None, ctx: dict = None) -> dict:
    """Active experiment (with progress), finished history, and suggestions. Finishes the
    active experiment once its days are up."""
    today = today or _drinking_day()
    with _EXP_LOCK:
        e = load_experiments()
        ctx = ctx or _exp_ctx(today)
        a = e["active"]
        if a and today >= date.fromisoformat(a["start"]) + timedelta(days=a["days"]):
            _finish(e, ctx, today)
            _save_experiments(e)
        a = e["active"]
    active = None
    if a:
        r = experiment_result(a, ctx, today)
        day_n = (today - date.fromisoformat(a["start"])).days + 1
        active = {**a, "day": day_n, "started": day_n >= 1,
                  "today": r["statuses"].get(today.isoformat(), "pending") if day_n >= 1 else "not started", **r}
    return {"active": active, "history": list(reversed(e["history"]))[:20],
            "suggestions": experiment_suggestions(e, today)}


def _finish(e: dict, ctx: dict, today: date):
    a = e["active"]
    r = experiment_result(a, ctx, today)
    e["history"].append({**{k: a[k] for k in ("id", "name", "preset", "detector", "start", "days")},
                         "ended": today.isoformat(), **{k: r[k] for k in ("adherence", "days_done", "metrics", "verdict", "outcome")}})
    e["active"] = None


def experiment_suggestions(e: dict, today: date, insight_signal: str = None) -> list:
    """Presets worth trying next: ones your insights point at first; never one that already
    helped (promote it instead) or one that showed no effect / didn't help in the last 90 days."""
    recent = {h.get("preset"): h for h in e["history"]
              if h.get("preset") and (today - date.fromisoformat(h["ended"])).days <= 90}
    helped = {h.get("preset") for h in e["history"] if h.get("outcome") == "helped"}
    out = []
    for pid, (name, _, _) in EXPERIMENT_PRESETS.items():
        if pid in helped or (pid in recent and recent[pid]["outcome"] in ("no_effect", "hurt")):
            continue
        if e["active"] and e["active"].get("preset") == pid:
            continue
        why = ("Your insights point here." if insight_signal and SIGNAL_EXPERIMENT.get(insight_signal) == pid
               else "Tried before, but adherence was too low to tell." if pid in recent else "Not tried yet.")
        out.append({"preset": pid, "name": name, "why": why})
    out.sort(key=lambda s: (s["why"] != "Your insights point here.", s["why"] != "Not tried yet."))
    return out


def start_experiment(fields: dict, today: date = None) -> dict:
    today = today or _drinking_day()
    days = int(fields.get("days") or EXP_DEFAULT_DAYS)
    if not 7 <= days <= 42:
        raise ValueError("experiments run 7–42 days")
    pid = fields.get("preset")
    if pid:
        if pid not in EXPERIMENT_PRESETS:
            raise ValueError("unknown experiment")
        name, detector, _ = EXPERIMENT_PRESETS[pid]
    else:
        name, detector = str(fields.get("name") or "").strip()[:60], "manual"
        if not name:
            raise ValueError("name the experiment")
    start = fields.get("start") or today.isoformat()
    date.fromisoformat(start)
    with _EXP_LOCK:
        e = load_experiments()
        if e["active"]:
            raise ValueError("one experiment at a time — end the current one first")
        e["active"] = {"id": uuid.uuid4().hex[:8], "name": name, "preset": pid, "detector": detector,
                       "start": start, "days": days, "marks": {}}
        _save_experiments(e)
    return experiments_view(today)


def mark_experiment(value, day: str = None) -> dict:
    day = day or _drinking_day().isoformat()
    date.fromisoformat(day)
    with _EXP_LOCK:
        e = load_experiments()
        if not e["active"] or e["active"]["detector"] != "manual":
            raise ValueError("this experiment is tracked automatically")
        e["active"]["marks"][day] = bool(value)
        _save_experiments(e)
    return experiments_view()


def end_experiment(today: date = None) -> dict:
    today = today or _drinking_day()
    with _EXP_LOCK:
        e = load_experiments()
        if not e["active"]:
            raise ValueError("no experiment running")
        _finish(e, _exp_ctx(today), today)
        _save_experiments(e)
    return experiments_view(today)


def promote_experiment(eid: str) -> dict:
    """Turn a finished experiment into a habit: its matching built-in habit, or a one-tap one."""
    e = load_experiments()
    h = next((x for x in e["history"] if x["id"] == eid), None)
    if not h:
        raise ValueError("no such experiment")
    habit = EXPERIMENT_PRESETS.get(h.get("preset") or "", (None, None, None))[2]
    cfg = load_habits()
    if habit:
        configure_habits({"active": list(dict.fromkeys(cfg["active"] + [habit]))})
    else:
        configure_habits({"add_custom": h["name"]})
    with _EXP_LOCK:
        e = load_experiments()
        for x in e["history"]:
            if x["id"] == eid:
                x["promoted"] = True
        _save_experiments(e)
    return experiments_view()


# --------------------------------------------------------------------------- #
#  Sunday review: the week on one screen, then next week's proposed actions
#  (recovery for slipping habits, an experiment if none is running, the week's
#  insight) — each accepted, edited or skipped. Accepted ones show on Today
#  through the next week.
# --------------------------------------------------------------------------- #
WEEKLY_PLANS_FILE = os.path.join(DATA, "weekly_plans.json")
_PLANS_LOCK = threading.Lock()


def _iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def load_plans() -> dict:
    try:
        with open(WEEKLY_PLANS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_plans(p: dict):
    tmp = f"{WEEKLY_PLANS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(p, f, indent=2)
    os.replace(tmp, WEEKLY_PLANS_FILE)


WEEK_PLAN_TEXT = {
    "zone2": lambda h, t: (f"Book {max(2, -(-t // 60))} × {min(90, -(-t // max(2, -(-t // 60))))} min Zone 2 sessions",
                           f"Last week: {h['done']} of {t} min."),
    "strength": lambda h, t: (f"Put {t} strength sessions in the calendar", f"Last week: {h['done']} of {t}."),
    "hiit": lambda h, t: (f"Put {t} HIIT session{'s' if t != 1 else ''} in the calendar, before midday", f"Last week: {h['done']} of {t}."),
    "eat_clean": lambda h, t: ("Plan a fermented food and a new plant into every meal on weekdays", f"{h['missed']} day{'s' if h['missed'] != 1 else ''} fell short last week."),
    "no_alcohol": lambda h, t: ("Dry Monday to Thursday; drinks only Friday or Saturday, before 8pm", f"{h['missed']} rule-breaking day{'s' if h['missed'] != 1 else ''} last week."),
    "kitchen_3h": lambda h, t: ("Kitchen closes 3 hours before bed every night", f"{h['missed']} late meal{'s' if h['missed'] != 1 else ''} last week."),
    "steps": lambda h, t: ("20-minute walk after dinner on weekdays", f"Short of {STEPS_TARGET:,} steps on {h['missed']} day{'s' if h['missed'] != 1 else ''}."),
}


def propose_next_week(habits: list, experiments: dict, insight) -> list:
    """Up to four concrete proposals: the two habits furthest behind, an experiment if none is
    running (insight-linked first), and the week's insight action."""
    out = []
    behind = sorted((h for h in habits if (h["done"] < h["target"] if h["period"] == "week" else h["missed"] > 0)),
                    key=lambda h: -h["gap"] if h["period"] == "day" else -(1 - h["done"] / h["target"] if h["target"] else 0))
    for h in behind[:2]:
        f = WEEK_PLAN_TEXT.get(h["id"])
        text, why = f(h, h.get("target")) if f else (f"{h['name']} every day", f"Missed {h['missed']} day{'s' if h['missed'] != 1 else ''} last week.")
        out.append({"id": f"habit:{h['id']}", "kind": "habit", "text": text, "why": why})
    if not experiments.get("active"):
        sugg = experiment_suggestions(load_experiments(), _nz_now().date(), insight and insight.get("signal"))
        if sugg:
            s = sugg[0]
            out.append({"id": f"exp:{s['preset']}", "kind": "experiment", "preset": s["preset"],
                        "text": f"Try for 14 days: {s['name']}", "why": s["why"]})
    if insight:
        out.append({"id": f"insight:{insight['key']}", "kind": "insight", "text": insight["action"], "why": insight["text"]})
    for p in out:
        p["status"] = "proposed"
    return out[:4]


def _weekly_trends(metrics: dict, sun: date) -> dict:
    """7-day smoothed weight for six weeks, and weekly HRV / sleep score averages."""
    wt = []
    for i in range(41, -1, -1):
        d = sun - timedelta(days=i)
        win = [v for v in (_night_value(metrics, (d - timedelta(days=j)).isoformat(), "weight") for j in range(7)) if v is not None]
        if win:
            wt.append({"date": d.isoformat(), "kg": round(statistics.mean(win), 2)})
    weeks = []
    for w in range(5, -1, -1):
        mon = sun - timedelta(days=6 + 7 * w)
        vals = lambda k: [v for v in (_night_value(metrics, (mon + timedelta(days=i)).isoformat(), k) for i in range(7)) if v is not None]
        hrv, ss = vals("hrv"), vals("sleep_score")
        weeks.append({"week_start": mon.isoformat(), "hrv": round(statistics.mean(hrv), 1) if hrv else None,
                      "sleep_score": round(statistics.mean(ss)) if ss else None})
    ch = None
    if len(wt) >= 8:
        prev = next((x["kg"] for x in wt if x["date"] == (sun - timedelta(days=7)).isoformat()), None)
        ch = round(wt[-1]["kg"] - prev, 2) if prev is not None else None
    return {"weight": wt, "weight_change": ch, "weeks": weeks}


# metric, label, unit, good direction, "about the same" band, decimals
BODY_ROWS = [("weight", "Weight", "kg", None, 0.2, 1), ("hrv", "HRV", "ms", 1, 2, 0),
             ("sleep_score", "Sleep score", "", 1, 2, 0), ("resting_hr", "Resting heart rate", "bpm", -1, 1, 0)]
NIGHT_WORDS = {"alcohol": "drinks", "late_meal": "late meal", "heavy_meal": "heavy day", "high_carb": "high carbs",
               "processed": "processed food", "low_fibre": "low fibre", "zone2_day": "Zone 2", "strength_day": "strength",
               "hiit_day": "HIIT", "late_bed": "late to bed"}


def review_body(metrics: dict, mon: date, as_of: date) -> list:
    """This week's average for each body number vs the four weeks before, in words."""
    days = [mon + timedelta(days=i) for i in range((as_of - mon).days + 1)]
    prior = [mon - timedelta(days=i) for i in range(1, 29)]
    vals = lambda key, ds: [v for v in (_night_value(metrics, d.isoformat(), key) for d in ds) if v is not None]
    target = CONFIG.get("target_weight")
    out = []
    for key, label, unit, good, band, dec in BODY_ROWS:
        now, before = vals(key, days), vals(key, prior)
        if not now or not before:
            continue
        a, b = statistics.mean(now), statistics.mean(before)
        if key == "weight":                         # losing is better while above the goal
            good = -1 if target and a > target else 0
        diff = a - b
        verdict = "about the same" if abs(diff) < band else (
            "better" if good and diff * good > 0 else "worse" if good else "up" if diff > 0 else "down")
        out.append({"key": key, "label": label, "unit": unit, "value": round(a, dec), "before": round(b, dec),
                    "diff": round(diff, dec if dec else 0), "verdict": verdict})
    return out


def review_nights(ctx: dict, mon: date, as_of: date) -> dict:
    """The week's best and worst night (by sleep score, else HRV) and what the evening before held."""
    nights = []
    for i in range((as_of - mon).days + 1):
        ev = mon + timedelta(days=i) - timedelta(days=1)       # Monday morning's night = Sunday evening
        night = (ev + timedelta(days=1)).isoformat()
        score, hrv = _night_value(ctx["metrics"], night, "sleep_score"), _night_value(ctx["metrics"], night, "hrv")
        if score is None and hrv is None:
            continue
        sig = behaviour_signals(ev.isoformat(), ctx)
        words = (["dry"] if sig.get("alcohol") is False else []) + [NIGHT_WORDS[k] for k, v in sig.items() if v is True]
        nights.append({"evening": ev.isoformat(), "night": night, "sleep_score": score, "hrv": hrv,
                       "rank": score if score is not None else hrv, "evening_was": words})
    if len(nights) < 3:
        return {}
    ranked = sorted(nights, key=lambda n: n["rank"])
    return {"best": ranked[-1], "worst": ranked[0]}


def weekly_review_screen(day: str = None, today: date = None) -> dict:
    today = today or _drinking_day()
    # Default: the week just ending on Sunday, otherwise the last complete week.
    d = date.fromisoformat(day) if day else (today if today.weekday() == 6 else today - timedelta(days=7))
    mon = d - timedelta(days=d.weekday())
    sun = mon + timedelta(days=6)
    as_of = min(sun, today)
    ctx = _insight_ctx(today)
    week = training_week(as_of.isoformat())
    habits = habits_week(as_of, ctx, week)
    exps = experiments_view(today, ctx)
    ranked = ranked_insights(today, ctx)
    key = _iso_week(mon)
    with _PLANS_LOCK:
        plans = load_plans()
        if key not in plans:
            plans[key] = {"for_week": _iso_week(mon + timedelta(days=7)),
                          "proposals": propose_next_week(habits, exps, ranked["headline"])}
            if as_of == sun:                       # only lock proposals in once the week is complete
                _save_plans(plans)
        entry = plans[key]
    this_mon = today - timedelta(days=today.weekday())
    return {"week": key, "start": mon.isoformat(), "end": sun.isoformat(), "complete": today >= sun,
            "is_current": mon == this_mon, "prev": (mon - timedelta(days=7)).isoformat(),
            "next": (mon + timedelta(days=7)).isoformat() if mon < this_mon else None,
            "habits": habits, "zone2": {"minutes": week["zone2_minutes"], "target": week["targets"]["zone2_minutes"]},
            "trends": _weekly_trends(ctx["metrics"], as_of),
            "body": review_body(ctx["metrics"], mon, as_of), "nights": review_nights(ctx, mon, as_of),
            "experiment": exps["active"], "finished": [h for h in exps["history"] if mon.isoformat() <= h["ended"] <= sun.isoformat()],
            "insight": ranked["headline"], "proposals": entry["proposals"], "for_week": entry["for_week"]}


def decide_proposal(fields: dict, today: date = None) -> dict:
    """Accept, edit (accepts with new text) or skip a proposal; accepting an experiment starts it."""
    today = today or _drinking_day()
    week, pid, act = fields.get("week"), fields.get("id"), fields.get("action")
    if act not in ("accept", "edit", "skip"):
        raise ValueError("action must be accept, edit or skip")
    if week not in load_plans():                    # deciding before Sunday: lock in what's on screen
        cur = weekly_review_screen(None, today)
        if cur["week"] != week:
            raise ValueError("that week's review isn't available")
        with _PLANS_LOCK:
            plans = load_plans()
            plans.setdefault(week, {"for_week": cur["for_week"], "proposals": cur["proposals"]})
            _save_plans(plans)
    with _PLANS_LOCK:
        plans = load_plans()
        p = next((x for x in plans[week]["proposals"] if x["id"] == pid), None)
        if not p:
            raise ValueError("no such proposal")
        if act == "skip":
            p["status"] = "skipped"
        else:
            if act == "edit":
                text = str(fields.get("text") or "").strip()[:140]
                if not text:
                    raise ValueError("write the action")
                p["text"] = text
            p["status"] = "accepted"
        _save_plans(plans)
    if act != "skip" and p["kind"] == "experiment" and not load_experiments()["active"]:
        start_experiment({"preset": p["preset"], "start": (today + timedelta(days=1)).isoformat()}, today)
    return weekly_review_screen(None, today)


def week_plan(today: date) -> list:
    """Accepted proposals for the current week, for the Today screen."""
    cur = _iso_week(today)
    out = []
    for k, v in load_plans().items():
        if v.get("for_week") == cur:
            out += [{"week": k, **p} for p in v["proposals"] if p["status"] == "accepted" and p["kind"] != "experiment"]
    return out


def tick_week_plan(week: str, pid: str) -> dict:
    with _PLANS_LOCK:
        plans = load_plans()
        p = next((x for x in (plans.get(week) or {}).get("proposals", []) if x["id"] == pid), None)
        if not p:
            raise ValueError("no such plan item")
        p["done"] = not p.get("done")
        _save_plans(plans)
    return today_plan()


def health_targets() -> dict:
    """Every target and rule the app scores against, in one place (for the connector)."""
    s = alcohol_settings()
    return {
        "food": {"daily_macros": FOOD_TARGETS, "macro_note": "high-protein, lower-carb; carbs is a ceiling",
                 "protocol": "ZOE rules (Tim Spector): 30 different plants a week (herbs/spices 1/4 point), "
                             "3 fermented portions a day, 30 g fibre a day, minimise medium/high-risk processed "
                             "food (ZOE-style processed-food risk scale), 12-hour-or-less eating window",
                 "zoe_targets": {"plants_week": zoe_rules.PLANTS_WEEK, "ferments_day": zoe_rules.FERMENTS_DAY,
                                 "fibre_g_day": zoe_rules.FIBRE_DAY, "eating_window_h": zoe_rules.WINDOW_H},
                 "organ": "no liver or oysters (high free copper)",
                 "never": ["gluten", "dairy", "egg (egg-white allergy)"]},
        "alcohol": {"rules": {k: s[k] for k in ("max_std", "cutoff", "dry_weekdays", "reset_days")},
                    "rules_text": ["30-day alcohol-free reset first", "no drinking Mon-Thu", "max 2 NZ standard drinks (10 g alcohol) per day",
                                   "nothing after 8pm", "no drinking alone", "decide in the morning", "never open a second bottle",
                                   "water before and between drinks", "WSET tastings spit-only", "log every drink the same day"],
                    "standard_drink": "NZ, 10 g alcohol", "drink_kinds": DRINK_KINDS,
                    "reset": _reset_info(_load_behav(), s, _drinking_day()),
                    "day_rollover": DAY_ROLLOVER},
        "weight": {"target_kg": CONFIG.get("target_weight"), "goal_start": CONFIG.get("weight_goal_start")},
    }


def health_today() -> dict:
    """Today's food (NZ date), ZOE rules and the drinking day's alcohol status."""
    day = _nz_now().date().isoformat()
    return {"date": day, "food": get_food(day), "alcohol": alcohol_status(),
            "checkin": get_checkin(_drinking_day().isoformat())}


def range_summary(start: str, end: str) -> dict:
    """Per-day food, ZOE rules, alcohol and recovery numbers for start..end (max 92 days), plus averages."""
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if b < a or (b - a).days > 91:
        raise ValueError("end must be on or after start, at most 92 days")
    store, food, sm, s = _load_behav(), _load_food(), _series_map(), alcohol_settings()
    today = _drinking_day()
    days = []
    for i in range((b - a).days + 1):
        d = a + timedelta(days=i)
        key = d.isoformat()
        items = (food.get(key) or {}).get("items") or []
        z = zoe_day(key, food, {}) if items else None
        al = _drink_day_info(store.get(key), d, s)
        days.append({"date": key, "weekday": d.strftime("%a"),
                     "food_logged": bool(items), "totals": (food.get(key) or {}).get("totals") or {},
                     "zoe_score": z and z["score"], "plants_week": z and z["plants_week"],
                     "fibre": z and z["fibre"], "ferments": z and z["ferments"],
                     "processing_score": z and z["processing_score"], "processed_items": (z or {}).get("risky_items", []),
                     "alcohol": al["status"] if d <= today else "future", "std": al["std"], "rules_broken": al["broken"],
                     **{k: sm.get(k, {}).get(key) for k in ("weight", "hrv", "sleep", "resting_hr", "sleep_score",
                                                             "hr_low_after_bed", "hrv_late_vs_early", "sleep_latency")}})
    logged = [x for x in days if x["food_logged"]]
    seen = [x for x in days if x["alcohol"] != "future"]
    return {"start": start, "end": end, "days": days, "summary": {
        "food_logged_days": len(logged),
        "avg_zoe_score": _mean([x["zoe_score"] for x in logged]), "avg_fibre": _mean([x["fibre"] for x in logged]),
        "avg_macros": {k: _mean([x["totals"].get(k) for x in logged]) for k in ("cal", "protein", "carbs", "fat")},
        "drink_days": sum(x["alcohol"] == "drink" for x in seen), "dry_days": sum(x["alcohol"] == "dry" for x in seen),
        "unlogged_drink_days": sum(x["alcohol"] == "unknown" for x in seen),
        "total_std": round(sum(x["std"] for x in seen), 1),
        "weight_first_last": [next((x["weight"] for x in days if x["weight"]), None),
                              next((x["weight"] for x in reversed(days) if x["weight"]), None)]}}


def compliance() -> dict:
    """Pace vs plan: training sessions due since block_start vs actually done (cursor)."""
    actual = load_state().get("cursor", 0)
    total = len(SEQUENCE)
    try:
        start = datetime.strptime(BLOCK_START, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return {"actual": actual, "expected": None, "total": total, "status": "unknown"}
    expected = 0
    d = start
    while d <= date.today() and expected < total:
        if d.weekday() in TRAINING_WEEKDAYS:
            expected += 1
        d += timedelta(days=1)
    diff = actual - expected
    # being up to 1 session behind (incl. today's not-yet-done) still counts as on track
    status = ("ahead" if diff > 0 else "on track" if diff >= -1 else "behind")
    return {"actual": actual, "expected": expected, "total": total,
            "behind_by": max(0, -diff), "status": status}


def sync_status() -> dict:
    store = _load_metrics_store()
    latest = max(store) if store else None
    states = _ha_request("/api/states", quiet=True)
    hae_last = None
    if isinstance(states, list):
        ts = [s.get("last_changed", "") for s in states if s.get("entity_id", "").startswith("hae.")]
        hae_last = max(ts) if ts else None
    return {"data_through": latest, "last_ha_update": hae_last,
            "now": datetime.now().astimezone().isoformat(timespec="seconds")}


def purge_bogus_weights():
    """One-off repair: the old substring match could store BMI (~28) or lean mass as
    weight. Anything below the sane floor isn't a body-mass reading — drop it."""
    store = _load_metrics_store()
    dropped = []
    for day, ms in store.items():
        w = ms.get("weight")
        if isinstance(w, dict) and isinstance(w.get("value"), (int, float)) \
                and w["value"] < SANE_MIN["weight"]:
            del ms["weight"]
            dropped.append(f"{day}={w['value']}")
    if dropped:
        _save_metrics_store(store)
        print(f"[repair] dropped bogus weight readings: {', '.join(dropped)}")


def snapshot_today():
    """Record today's metrics into the per-date store, building daily history.
    (HA's recorder doesn't track the hae.* domain, so the hub keeps its own history.)"""
    cur = todays_metrics()
    if not cur:
        return
    store = _load_metrics_store()
    bucket = store.setdefault(date.today().isoformat(), {})
    for k, v in cur.items():
        if _device_owned(k, bucket.get(k)):
            continue    # keep Oura's overnight value; don't clobber with the live Watch average
        bucket[k] = v
    _save_metrics_store(store)


def _metric_value(key: str, m: dict):
    v = m.get("asleep_hours") if key == "sleep" else m.get("value")
    return v if isinstance(v, (int, float)) else None


def history_series(days: int = 30) -> dict:
    """Daily time-series per metric from the hub's own store (one value/day)."""
    snapshot_today()
    store = _load_metrics_store()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    out = {}
    for day in sorted(store):
        if day < cutoff:
            continue
        for key, m in store[day].items():
            if not isinstance(m, dict):
                continue
            val = _metric_value(key, m)
            if val is not None:
                out.setdefault(key, []).append({"t": day, "v": val})
    # sleep stages (Oura gives real deep/REM; HAE sometimes provides them too)
    for day in sorted(store):
        if day < cutoff:
            continue
        s = store[day].get("sleep")
        if isinstance(s, dict):
            for skey, okey in (("deep", "sleep_deep"), ("rem", "sleep_rem"),
                               ("core", "sleep_core"), ("awake", "sleep_awake")):
                v = s.get(skey)
                if isinstance(v, (int, float)):
                    out.setdefault(okey, []).append({"t": day, "v": round(v, 2)})
    # surface the combined daily readiness score as its own trend series
    out["score"] = [{"t": p["t"], "v": p["v"]} for p in score_series(days)]
    return out


# --------------------------------------------------------------------------- #
#  State — entry history + rolling AI summary.
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    base = {"entries": [], "summary": "", "cursor": 0, "log": []}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            base.update(json.load(f))
    return base


def save_state(state: dict):
    tmp = f"{STATE_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------- #
#  Home Assistant integration — card, notify, and the "done" watcher.
# --------------------------------------------------------------------------- #
def _ha_request(path: str, method: str = "GET", body: dict = None, quiet: bool = False):
    """One HA call, trying the remote (Nabu Casa) URL then the LAN URL.
    Returns parsed JSON ({} if empty) on success, or None on failure."""
    token = ha_token()
    candidates = ha_bases()
    if not (candidates and token):
        return None
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for base in candidates:
        try:
            req = urllib.request.Request(
                f"{base}{path}", data=data, method=method,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # entity/helper not created yet — expected, stay quiet
            last = e
        except urllib.error.URLError as e:
            last = e
    if not quiet and last is not None:
        print(f"[ha] {method} {path} failed: {last}")
    return None


# --------------------------------------------------------------------------- #
#  Notion integration — read/write a page's text. Works over the normal API
#  (outbound HTTPS), so it isn't affected by the work Mac's blocked iCloud sync.
#  Used for the Workout note (read + write) when the corresponding *_page
#  config key is set; falls back to Apple Notes otherwise.
# --------------------------------------------------------------------------- #
NOTION_TOKEN = CONFIG.get("notion_token", "") or os.environ.get("NOTION_TOKEN", "")
NOTION_WORKOUT_PAGE = CONFIG.get("notion_workout_page", "")
NOTION_VERSION = "2022-06-28"


def _notion_page_id(s: str) -> str:
    """Accept a page URL or id; return the bare 32-hex id Notion's API wants."""
    hexes = re.findall(r"[0-9a-fA-F]{32}", (s or "").replace("-", ""))
    return hexes[-1] if hexes else (s or "").strip()


def notion_api(path: str, method: str = "GET", body: dict = None):
    if not NOTION_TOKEN:
        return None
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"https://api.notion.com/v1{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {NOTION_TOKEN}",
                 "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        print(f"[notion] {method} {path} failed: {e.code} {e.read().decode()[:200]}")
    except urllib.error.URLError as e:
        print(f"[notion] {method} {path} error: {e}")
    return None


_NOTION_TEXT_TYPES = ("paragraph", "heading_1", "heading_2", "heading_3",
                      "bulleted_list_item", "numbered_list_item", "to_do", "quote")


def notion_page_text(page_or_id: str) -> str:
    """Plain text of a Notion page (top-level text blocks, in order)."""
    pid = _notion_page_id(page_or_id)
    lines, cursor = [], None
    for _ in range(10):  # paginate (100 blocks/page; 10 pages is plenty)
        q = f"?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        res = notion_api(f"/blocks/{pid}/children{q}")
        if not res:
            break
        for blk in res.get("results", []):
            t = blk.get("type")
            if t in _NOTION_TEXT_TYPES:
                rich = blk.get(t, {}).get("rich_text", [])
                lines.append("".join(r.get("plain_text", "") for r in rich))
        if res.get("has_more"):
            cursor = res.get("next_cursor")
        else:
            break
    return "\n".join(lines).strip()


def notion_write_text(page_or_id: str, text: str) -> bool:
    """Replace a Notion page's content with `text` (one paragraph per line)."""
    pid = _notion_page_id(page_or_id)
    existing = notion_api(f"/blocks/{pid}/children?page_size=100")
    if existing is None:
        return False
    for blk in existing.get("results", []):
        notion_api(f"/blocks/{blk['id']}", "DELETE")
    children = [{"object": "block", "type": "paragraph",
                 "paragraph": {"rich_text": ([{"type": "text", "text": {"content": ln[:1900]}}]
                                             if ln else [])}}
                for ln in text.split("\n")]
    # Notion caps appends at 100 blocks/call
    ok = True
    for i in range(0, len(children), 100):
        if notion_api(f"/blocks/{pid}/children", "PATCH", {"children": children[i:i + 100]}) is None:
            ok = False
    return ok


def push_ha_card() -> bool:
    if not WORKOUT_QUEUE:
        return False
    return _push_ha_card()


def _push_ha_card() -> bool:
    """Write the CURRENT session into HA as sensor.todays_workout (state + attributes)
    so a Lovelace markdown card can render it. Advances only when you mark done."""
    w = current_session()
    icon = {"strength": "mdi:weight-lifter", "endurance": "mdi:run",
            "conditioning": "mdi:kettlebell-outline", "rest": "mdi:sleep"}.get(w["type"], "mdi:dumbbell")
    prog = f"Session {w.get('position')} of {w.get('total')}"
    meta = f"{w['block']} · Week {w['week']} · {prog}"
    c = compliance()
    cdot = {"ahead": "🟢", "on track": "🟢", "slightly behind": "🟡", "behind": "🔴"}.get(c["status"], "⚪")
    cline = f"\n\n{cdot} **{c['status'].title()}** · {c['actual']}/{c['expected']} sessions vs plan" \
        if c.get("expected") is not None else ""
    if w["type"] == "rest":
        md = f"## 🛌 {w['focus']}\n*{meta}*\n\n{w['summary']}{cline}"
    else:
        detail_md = "\n".join(f"- {ln}" for ln in w["detail"].split("\n") if ln.strip())
        md = (f"## 🏋️ {w['focus']}\n*{meta}*\n\n**{w['summary']}**\n\n{detail_md}{cline}"
              + (f"\n\n[Full program in Notion]({w['notion_url']})" if w.get("notion_url") else ""))
    r = _ha_request("/api/states/sensor.todays_workout", "POST", {
        "state": w["focus"][:255],
        "attributes": {
            "friendly_name": "Next Workout", "icon": icon, "meta": meta,
            "summary": w["summary"], "detail": w["detail"], "markdown": md,
            "week": w["week"], "position": w.get("position"), "total": w.get("total"),
            "type": w["type"],
        },
    })
    ok = r is not None
    print(f"[card] {'pushed: ' + w['focus'] if ok else 'push failed / HA not configured'}")
    push_ha_extras()
    return ok


def push_ha_extras():
    """Surface the latest recorded workout + tonight's check-in as HA sensors."""
    # Latest workout
    wk = import_workouts()
    if wk:
        last = max(wk.values(), key=lambda w: w["date"])
        nice = last["type"].replace("_", " ").title()
        bits = []
        if last.get("minutes") is not None: bits.append(f"{last['minutes']} min")
        if last.get("avg_hr") is not None: bits.append(f"{last['avg_hr']} bpm")
        if last.get("calories") is not None: bits.append(f"{last['calories']} cal")
        detail = " · ".join(bits)
        _ha_request("/api/states/sensor.latest_workout", "POST", {
            "state": nice[:255],
            "attributes": {"friendly_name": "Latest Workout", "icon": "mdi:history",
                           "date": last["date"], "category": last.get("category"),
                           "minutes": last.get("minutes"), "avg_hr": last.get("avg_hr"),
                           "calories": last.get("calories"),
                           "markdown": f"**{nice}** · {last['date']}" + (f"\n{detail}" if detail else "")},
        })
    # Evening check-in (today's behaviour record)
    b = _load_behav().get(date.today().isoformat(), {})
    if b:
        parts = []
        if "drink" in b: parts.append(("🍷 drink" if b["drink"] else "no drink"))
        if "clean" in b: parts.append(("🥗 ate clean" if b["clean"] else "off-plan food"))
        if "stress" in b: parts.append(f"stress {int(b['stress'])}/5")
        state = ("✓ logged" if b.get("locked") else "in progress")
        _ha_request("/api/states/sensor.evening_checkin", "POST", {
            "state": state,
            "attributes": {"friendly_name": "Evening Check-in", "icon": "mdi:clipboard-check",
                           "summary": " · ".join(parts), "locked": bool(b.get("locked")),
                           "drink": b.get("drink"), "ate_clean": b.get("clean"),
                           "work_stress": b.get("stress"),
                           "markdown": f"**Tonight** — {' · '.join(parts) or 'nothing logged yet'}"},
        })


# The MTI session queue is retired (habits track training now): no morning push, no HA card,
# no done-toggle watcher. Set "workout_queue": true in config.json to bring them back.
WORKOUT_QUEUE = bool(CONFIG.get("workout_queue", False))


def notify_current():
    if not WORKOUT_QUEUE:
        return
    w = current_session()
    title = "Next workout" if w["type"] != "rest" else "Rest 🛌"
    msg = f"{w['focus']} · {w['summary']}"
    service = CONFIG.get("notify_service", "").strip("/")
    if service:
        _ha_request(f"/api/services/{service}", "POST", {"title": title, "message": msg})


DONE_HELPER = CONFIG.get("done_helper", "input_boolean.workout_done")
DONE_POLL_SECS = CONFIG.get("done_poll_secs", 20)


OURA_SYNC_SECS = 900        # every 15 minutes: workouts show up soon after Oura has them


def background_tick(st: dict, now: float = None):
    """One pass of the always-on background loop: hourly Oura sync (whether or not Home
    Assistant is reachable), then the Home Assistant work — daily metric snapshot, intraday
    averages, behaviour toggles, workouts the iPhone sent via HA, sensors pushed back, and the
    check-in button. The retired session queue's parts run only with workout_queue on."""
    now = time.time() if now is None else now
    if now - st.get("last_oura", 0.0) > OURA_SYNC_SECS:
        st["last_oura"] = now
        try:
            if oura_status()["connected"]:
                oura_sync(7)
        except Exception as e:   # keep the loop alive whatever Oura does
            print("[oura] sync error:", e)
    if not st.get("ha"):
        return
    states = _ha_request("/api/states", quiet=True)
    if not isinstance(states, list):
        return
    byid = {s.get("entity_id"): s for s in states}
    today = date.today().isoformat()
    if today != st.get("last_snap"):
        snapshot_today()
        if st.get("last_snap") is not None:
            reset_behaviour_toggles()      # new day → fresh toggles
        st["last_snap"] = today
    else:
        accumulate_intraday(metrics_from_ha(states))  # running daily averages
    snapshot_behaviours(states)
    # pull workouts the iPhone sent to HA (sensor.workout_inbox holds the raw payload)
    inbox = byid.get("sensor.workout_inbox")
    if inbox:
        seq = inbox.get("attributes", {}).get("seq")
        if seq and seq != st.get("last_inbox_seq"):
            st["last_inbox_seq"] = seq
            try:
                raw = inbox["attributes"].get("raw")
                # HA returns the attribute as a parsed object, not a JSON string
                payload = raw if isinstance(raw, dict) else json.loads(raw or "{}")
                n = ingest_workouts(payload.get("data", payload).get("workouts") or [])
                print(f"[inbox] ingested {n} new workout(s) from HA")
            except Exception as e:
                print("[inbox] parse error:", e)
    push_ha_extras()        # keep latest-workout + check-in sensors fresh in HA
    if WORKOUT_QUEUE:
        apply_workouts()    # advance the plan for any new matching workouts
        # workout done → read logged weights from the note, store on the session, advance
        done = byid.get(DONE_HELPER)
        if done and done.get("state") == "on":
            domain = DONE_HELPER.partition(".")[0]
            res = complete_workout(notes="marked done in HA")
            _ha_request(f"/api/services/{domain}/turn_off", "POST", {"entity_id": DONE_HELPER})
            print(f"[done] advanced to {res['next']['focus']}")
    # evening check-in done (input_button state is a timestamp that changes on press)
    ci = byid.get(CHECKIN_HELPER)
    if ci:
        cur = ci.get("state")
        if "last_checkin" not in st:
            st["last_checkin"] = cur
        elif cur not in (None, "unknown", "unavailable") and cur != st["last_checkin"]:
            st["last_checkin"] = cur
            finalize_checkin()


def _start_background_loop():
    st = {"ha": bool(ha_token() and (CONFIG.get("ha_url") or CONFIG.get("ha_url_local")))}
    if not st["ha"]:
        print("[loop] HA not configured — Oura sync only.")

    def loop():
        while True:
            time.sleep(DONE_POLL_SECS)
            try:
                background_tick(st)
            except Exception as e:
                print("[loop] error:", e)

    threading.Thread(target=loop, daemon=True).start()
    print(f"[loop] background sync every {DONE_POLL_SECS}s (Oura hourly)")


# --------------------------------------------------------------------------- #
#  AI coaching — proxied through the hub so the key never reaches the browser.
# --------------------------------------------------------------------------- #
COACH_SYSTEM = (
    "You are Mark's health coach. He's 50, training an MTI-style strength+endurance block. "
    "Most data is automatic (Apple Watch + Oura via Apple Health); he also logs an evening "
    "check-in for alcohol, eating clean, stress, late meals and late caffeine, plus a food log "
    "with daily macro targets (high-protein, lower-carb, 2000 kcal is his daily target and already a deficit), and a "
    "100 kg weight goal. "
    "Background: dairy-free / gluten-free / egg-free (no whey; rice protein is his powder), "
    "following ZOE's rules (30 different plants a week, 3 fermented portions a day, 30 g fibre a "
    "day, minimal ultra-processed food, a 12-hour eating window); no liver or oysters (his free "
    "copper runs high); and he's sensitive to "
    "alcohol — even one drink tanks his sleep/HRV that night. Alcohol rules: a 30-day reset "
    "first, then no drinking Mon-Thu, max 2 standard drinks, nothing after 8pm.\n"
    "READINESS is a recovery-only score (HRV, resting HR, sleep) vs his 90-day baseline (NOT "
    "activity). ACTIVITY (steps/energy vs goals) is separate and plan-aware (rest days are fine). "
    "ANSWER MARK'S QUESTION directly, grounded in HIS numbers — cite the actual values and "
    "interpret vs his own baseline, not generic norms (e.g. 'HRV 1.4 SD below your baseline'). "
    "Lean on SIGNIFICANT correlations. Be concise and practical; use short markdown — headings, "
    "bold and bullet lists only, NO tables. If he hasn't asked anything specific, give a brief "
    "read on how today is going."
)


def anthropic_coach(payload: dict) -> dict:
    """payload: {question, today, trends, workout, log, model?}. Returns {answer}."""
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in the hub's environment")
    model = payload.get("model") or DEFAULT_MODEL
    ins = insights()
    question = (payload.get("question") or payload.get("context") or "").strip()
    user = (
        f"MARK'S QUESTION: {question or '(none — give a brief read on today)'}\n\n"
        f"TODAY'S METRICS:\n{json.dumps(payload.get('today', {}), indent=1)}\n\n"
        f"READINESS — recovery only (vs 90-day baseline, oriented so + = better):\n{json.dumps(ins['readiness'])}\n\n"
        f"READINESS TREND (today vs personal 14/30d baseline + strain read — the early-warning signal):\n{json.dumps(ins['readiness_trend'])}\n\n"
        f"ACTIVITY today (goals; 100 = planned workout completed):\n{json.dumps(ins['activity'])}\n\n"
        f"BASELINES (today vs mean/SD + z-score per metric):\n{json.dumps(ins['baselines'])}\n\n"
        f"TRAINING LOAD (acute 7d vs chronic 28d) + recovery dir:\n{json.dumps(ins['training_load'])}\n\n"
        f"TODAY'S CHECK-IN (1=yes):\n{json.dumps(get_checkin())}\n\n"
        f"TODAY'S FOOD (totals vs targets — high-protein, lower-carb):\n{json.dumps(get_food())}\n\n"
        f"WEIGHT (current vs goal + trend):\n{json.dumps(weight_status())}\n\n"
        f"HIS PERSONAL PATTERNS (behaviour→recovery correlations with r/p/significance, drift):\n{json.dumps(ins['patterns'])}\n\n"
        f"CURRENT WORKOUT:\n{json.dumps(payload.get('workout', {}))}\n\n"
        f"RECENTLY COMPLETED SESSIONS:\n{json.dumps(payload.get('log', []))}"
    )
    body = json.dumps({
        "model": model,
        "max_tokens": 1000,
        "system": COACH_SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    text = "".join(b.get("text", "") for b in data.get("content", []))
    return {"answer": text.strip()}


# --------------------------------------------------------------------------- #
#  Food logger — reads an Apple Note (Mac, via AppleScript) and parses macros
#  (protein / carbs / fat / calories) with Claude, so any note format works.
# --------------------------------------------------------------------------- #
import subprocess

FOOD_FILE = os.path.join(DATA, "food.json")
FOOD_CACHE_FILE = os.path.join(DATA, "food_cache.json")
# macro estimation accuracy matters more than latency here, and the cache means each unique
# entry is parsed only once — worth the mid-tier model (config food_parse_model to override)
FOOD_PARSE_MODEL = CONFIG.get("food_parse_model") or "claude-sonnet-4-6"
_FOOD_LOCK = threading.Lock()
STANDARD_MEALS_NOTE = CONFIG.get("standard_meals_note", "Standard Meals")
# Daily macro targets — high-protein, lower-carb. Direction: cal/protein/fat have a target to
# reach; carbs is a ceiling (lower is better).
FOOD_TARGETS = CONFIG.get("food_targets") or {"cal": 2000, "protein": 190, "carbs": 110, "fat": 90}


def _load_food() -> dict:
    if os.path.exists(FOOD_FILE):
        with open(FOOD_FILE) as f:
            return json.load(f)
    return {}


def _save_food(store):
    tmp = f"{FOOD_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, FOOD_FILE)


def read_note(name: str) -> str:
    """Body of an Apple Note as plain text (Mac only, via AppleScript)."""
    script = f'tell application "Notes" to return body of note "{name}"'
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=25)
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    html = out.stdout or ""
    text = re.sub(r"<(div|br|p|li|h\d)[^>]*>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&gt;", ">"), ("&lt;", "<")):
        text = text.replace(a, b)
    return re.sub(r"\n{2,}", "\n", text).strip()


FOOD_REFERENCE_FILE = os.path.join(HERE, "food_reference.md")


def _food_reference() -> str:
    """Bundled verified-nutrition reference (label + USDA data for Mark's staples)."""
    try:
        with open(FOOD_REFERENCE_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def read_standard_meals() -> str:
    """Parsing reference: the bundled verified-macros file, plus the standard-meals
    Apple Note when one exists (Mac dev only — AppleScript is a no-op on the Green)."""
    parts = [_food_reference(), read_note(STANDARD_MEALS_NOTE)]
    return "\n\n".join(p for p in parts if p.strip())


FOOD_SYSTEM = (
    "You convert one short free-text food entry into structured nutrition. It may name foods "
    "(estimate typical macros) and/or give explicit macros (use them). It may describe several "
    "foods at once, separated by commas or 'and' ('200g chicken, cup of rice and broccoli') — "
    "return one item per food. "
    "A NUTRITION REFERENCE may be provided with verified label/USDA values and scoop-size "
    "conventions — when an entry matches a reference item, scale from those values instead of "
    "estimating; the reference always beats your own estimate. "
    "Work portion-first: decide each item's weight in grams, then compute macros from per-100g "
    "data. When the entry gave no amount, assume a typical portion and state it in the name, "
    "e.g. 'Banana (~120g)'. For meat/fish/rice, raw and cooked values differ (raw meat loses "
    "~25% weight cooking) — use values matching the state given, and default to cooked when "
    "unstated. Cross-check every item: cal can never be below 4*protein + 4*carbs + 9*fat "
    "(alcohol, fibre and sugar alcohols may push it moderately above); fix violations before "
    "answering. "
    "Also give the inputs for ZOE's food rules: "
    '"plants" = every distinct plant species in the item, as [{"name": singular common name, '
    '"kind": "plant" | "herb_spice"}] — veg, fruit, wholegrains (brown rice, oats, quinoa, '
    "buckwheat), legumes, nuts, seeds count as \"plant\"; herbs and spices (incl. garlic only "
    'when used as seasoning, ginger, chilli, pepper, cinnamon) as "herb_spice"; also count '
    "extra-virgin olive oil, coffee, tea and dark chocolate 70%+ as plants. Refined products "
    "(white rice, white flour, sugar, refined oils, juice) and protein isolates are NOT plants. "
    "For a mixed dish list every plant ingredient a typical recipe has. [] for meat, fish, etc. "
    '"fibre" = dietary fibre in g. "fermented" = fermented portions with live cultures or '
    "traditional fermentation (kimchi, sauerkraut, kefir, kombucha, live yoghurt incl. coconut, "
    "miso, tempeh, real sourdough, aged cheese, kvass): about 1 per typical serving, 0.5 for a "
    "spoonful; 0 when none. "
    "Also give the inputs for a processed-food risk score: "
    '"grams" = portion weight; "sugar" = total sugars g; "sodium_mg" = sodium mg (not salt); '
    '"nova" = NOVA class 1-4 (1 whole/minimally processed incl. plain cooked meat, fish, veg, '
    "rice, frozen veg, plain nuts; 2 culinary ingredients like butter, oil, honey; 3 simple "
    "processed foods made from 1+2 with salt/sugar/oil, e.g. traditional sourdough, cheese, tinned "
    "fish, wine; 4 industrial formulations, e.g. packaged bread, bars, protein powders, soft "
    'drinks, sausages, nuggets, flavoured yoghurts); "eir" = estimated energy intake rate in '
    "kcal/min — how fast the calories are eaten in this form (liquids, soft refined breads, milk "
    "chocolate, ice cream, crisps, nuggets, fries are typically 70-150; whole fruit, raw/cooked "
    "veg, salads, dense wholegrain breads, tough meats, nuts eaten one by one are typically "
    '10-50; tender meat/fish/rice dishes ~40-70); "additives" = the non-culinary additives the '
    'typical branded version contains, as E-numbers where known (e.g. ["E471","E481"]; for a '
    "named brand use its known label), [] for whole foods and home cooking. Return ONLY JSON: "
    '{"items": [{"name": str, "protein": g, "carbs": g, "fat": g, "cal": kcal, '
    '"plants": [{"name": str, "kind": "plant|herb_spice"}], "fibre": g, "fermented": number, '
    '"grams": g, "sugar": g, "sodium_mg": mg, "nova": 1-4, "eir": kcal_per_min, '
    '"additives": [str]}]}. '
    "Macros as integers, no units; fibre and fermented may be decimals."
)


def parse_food_entry(text: str) -> list:
    """One free-text entry → list of structured items (macros, ZOE-rule inputs, processing risk)."""
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in the hub's environment")
    if not text.strip():
        return []
    standard = read_standard_meals()
    ref = f"NUTRITION REFERENCE:\n{standard[:6000]}\n\n" if standard.strip() else ""
    body = json.dumps({
        "model": FOOD_PARSE_MODEL, "max_tokens": 2000, "system": FOOD_SYSTEM,
        "messages": [{"role": "user", "content": f"{ref}FOOD ENTRY:\n{text[:1000]}"}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json", "x-api-key": ANTHROPIC_KEY,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    out = "".join(b.get("text", "") for b in data.get("content", []))
    m = re.search(r"\{.*\}", out, re.S)
    items = (json.loads(m.group(0)) if m else {}).get("items") or []
    for i in items:  # consistency guard: cal below the macros' own energy (4/4/9) is impossible;
        # above is legitimate (alcohol, fibre, polyols), so only ever correct upward
        atwater = round(4 * (i.get("protein") or 0) + 4 * (i.get("carbs") or 0)
                        + 9 * (i.get("fat") or 0))
        if (i.get("cal") or 0) < 0.75 * atwater:
            i["cal"] = atwater
        _score_processing(i)
    return items


def _score_processing(i: dict):
    """Attach the ZOE-style processed-food risk level (see processing_risk.py). Items parsed
    before the risk fields existed (no "nova") are left unscored."""
    if i.get("nova") is None:
        return
    try:
        p = processing_risk.classify(i)
        i["processing"] = {"level": p["level"], "label": p["label"], "why": p["why"]}
    except Exception as e:  # never let scoring break food logging
        print(f"[food] processing score failed for {i.get('name')!r}: {e}")


PHOTO_TYPES = ("image/jpeg", "image/png", "image/webp")
PHOTO_MAX_B64 = 6 * 1024 * 1024
FOOD_PHOTO_NOTE = (
    "The entry is a PHOTO of food. Identify every distinct food and drink visible and estimate "
    "each portion in grams from visual cues (a dinner plate is ~26 cm across, a fork ~19 cm); "
    "give plants, fibre and fermented the same way as for text. Name items plainly with the portion, e.g. "
    "'Roast salmon (~180g)'. Don't invent hidden ingredients; when a sauce or dressing is visible, "
    "include it as its own item. If the photo shows no food, return {\"items\": []}."
)


def parse_food_photo(image_b64: str, media_type: str, note: str = "") -> list:
    """A meal photo (+ optional note such as 'olive oil dressing') → parsed items, not saved."""
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in the hub's environment")
    if media_type not in PHOTO_TYPES:
        raise ValueError("photo must be JPEG, PNG or WebP")
    if not image_b64 or len(image_b64) > PHOTO_MAX_B64:
        raise ValueError("photo missing or too large")
    standard = read_standard_meals()
    ref = f"NUTRITION REFERENCE:\n{standard[:6000]}\n\n" if standard.strip() else ""
    text = f"{ref}{FOOD_PHOTO_NOTE}" + (f"\nMark's note about this meal: {note.strip()[:300]}" if note and note.strip() else "")
    body = json.dumps({
        "model": FOOD_PARSE_MODEL, "max_tokens": 2500, "system": FOOD_SYSTEM,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": text}]}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json", "x-api-key": ANTHROPIC_KEY,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read())
    out = "".join(b.get("text", "") for b in data.get("content", []))
    m = re.search(r"\{.*\}", out, re.S)
    items = (json.loads(m.group(0)) if m else {}).get("items") or []
    for i in items:                                  # same energy guard as text parsing
        atwater = round(4 * (i.get("protein") or 0) + 4 * (i.get("carbs") or 0) + 9 * (i.get("fat") or 0))
        if (i.get("cal") or 0) < 0.75 * atwater:
            i["cal"] = atwater
        _score_processing(i)
    return items


def _recalc_totals(rec: dict):
    t = {"protein": 0, "carbs": 0, "fat": 0, "cal": 0}
    for i in rec.get("items", []):
        for k in t:
            t[k] += i.get(k) or 0
    rec["totals"] = {k: round(v) for k, v in t.items()}


def _ensure_ids(rec: dict):
    for i in rec.get("items", []):
        if not i.get("id"):
            i["id"] = uuid.uuid4().hex[:8]


def _food_cache_ver() -> str:
    # prompt, model or reference-data changes all invalidate cached parses
    src = FOOD_SYSTEM + FOOD_PARSE_MODEL + _food_reference()
    return hashlib.md5(src.encode()).hexdigest()[:8]


def _food_cache_load() -> dict:
    if os.path.exists(FOOD_CACHE_FILE):
        try:
            with open(FOOD_CACHE_FILE) as f:
                cache = json.load(f)
            if cache.get("_ver") == _food_cache_ver():
                return cache
        except (json.JSONDecodeError, OSError):
            pass
    return {"_ver": _food_cache_ver()}


def _food_cache_save(cache: dict):
    cache["_ver"] = _food_cache_ver()
    while len(cache) > 500:                       # keep the cache bounded, oldest first
        cache.pop(next(k for k in cache if k != "_ver"))
    tmp = f"{FOOD_CACHE_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, FOOD_CACHE_FILE)


def parse_food_text(text: str) -> list:
    """Free-text entry (comma-separated = multiple foods) → parsed items, without saving.
    Repeat foods hit a local cache (instant, no API call); uncached parts parse in parallel."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        raise RuntimeError("empty entry")
    cache = _food_cache_load()
    keyed = [(p, re.sub(r"\s+", " ", p.lower())) for p in parts]
    results = {k: json.loads(json.dumps(cache[k])) for _, k in keyed if k in cache}
    misses = [(p, k) for p, k in keyed if k not in results]
    if misses:
        errs = {}

        def work(p, k):
            try:
                results[k] = parse_food_entry(p)
            except Exception as e:
                errs[k] = str(e)

        threads = [threading.Thread(target=work, args=m, daemon=True) for m in misses]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=75)
        for p, k in misses:
            if not results.get(k):
                raise RuntimeError(f"couldn't parse '{p}'" + (f": {errs[k]}" if k in errs else ""))
            cache[k] = json.loads(json.dumps(results[k]))
        _food_cache_save(cache)
    items = []
    for _, k in keyed:
        items.extend(results[k])
    return items


FOOD_ITEM_KEYS = {"name": str, "protein": (int, float), "carbs": (int, float), "fat": (int, float),
                  "cal": (int, float), "plants": list, "fibre": (int, float), "fermented": (int, float),
                  "grams": (int, float), "sugar": (int, float), "sodium_mg": (int, float),
                  "nova": int, "eir": (int, float), "additives": list, "processing": dict}


def _eaten_at(at: str = None, day: str = None):
    """HH:MM the food was eaten: as given, else now when logging for today, else unknown."""
    if at:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(at)):
            raise ValueError("time eaten must be HH:MM")
        return str(at)
    now = _nz_now()
    return now.strftime("%H:%M") if day in (None, now.date().isoformat()) else None


def add_food_items(items: list, meal: str, day: str = None, entry_id: str = None,
                   at: str = None) -> dict:
    """Append already-parsed items to the day's log. `entry_id` makes retries idempotent;
    `at` is the time eaten (default now, for today)."""
    at = _eaten_at(at, day)
    day = day or _nz_now().date().isoformat()
    date.fromisoformat(day)
    meal = (meal or "snack").lower()
    if meal not in ("breakfast", "lunch", "dinner", "snack"):
        meal = "snack"
    if entry_id is not None and not re.fullmatch(r"[a-f0-9]{8,32}", str(entry_id)):
        raise ValueError("entry_id must be 8–32 hex characters")
    if not isinstance(items, list) or not items or len(items) > 30:
        raise ValueError("items must be a non-empty list (max 30)")
    clean = []
    for i in items:
        if not isinstance(i, dict) or not isinstance(i.get("name"), str):
            raise ValueError("each item needs a name")
        clean.append({k: v for k, v in i.items() if k in FOOD_ITEM_KEYS and isinstance(v, FOOD_ITEM_KEYS[k])})
    with _FOOD_LOCK:
        store = _load_food()
        rec = store.setdefault(day, {"items": [], "totals": {}})
        if entry_id and any(i.get("entry") == entry_id for i in rec["items"]):
            return {**get_food(day), "already_applied": True}
        _ensure_ids(rec)
        for i in clean:
            i["meal"] = meal
            i["id"] = uuid.uuid4().hex[:8]
            if at:
                i["at"] = at
            if entry_id:
                i["entry"] = entry_id
            rec["items"].append(i)
        _recalc_totals(rec)
        rec["synced_at"] = datetime.now().isoformat(timespec="seconds")
        _save_food(store)
    return get_food(day)


def usual_foods(days: int = 30, limit: int = 8, store: dict = None) -> list:
    """The foods logged most often lately (latest version of each), for one-tap repeats."""
    store = _load_food() if store is None else store
    cutoff = (_nz_now().date() - timedelta(days=days)).isoformat()
    seen = {}
    for d in sorted(k for k in store if k >= cutoff):
        for i in store[d].get("items") or []:
            name = (i.get("name") or "").strip()
            if name:
                n = seen.get(name.lower(), (0, None))[0] + 1
                seen[name.lower()] = (n, {k: v for k, v in i.items() if k in FOOD_ITEM_KEYS})
    top = sorted(seen.values(), key=lambda x: -x[0])[:limit]
    return [{"count": n, "item": item} for n, item in top if n >= 2]


def add_food_entry(text: str, meal: str, day: str = None, at: str = None) -> dict:
    """Parse a free-text entry and append it to the day's log (the app's one-step path)."""
    _eaten_at(at, day)                                  # reject a bad time before paying to parse
    return add_food_items(parse_food_text(text), meal, day, at=at)


def remove_food_item(item_id: str, day: str = None) -> dict:
    day = day or _nz_now().date().isoformat()
    with _FOOD_LOCK:
        store = _load_food()
        rec = store.get(day)
        if rec:
            _ensure_ids(rec)
            rec["items"] = [i for i in rec.get("items", []) if i.get("id") != item_id]
            _recalc_totals(rec)
            _save_food(store)
    return get_food(day)


def update_food_item(item_id: str, text: str = None, meal: str = None, day: str = None) -> dict:
    """Re-describe an item (re-parses text) and/or move it to another meal."""
    day = day or _nz_now().date().isoformat()
    store = _load_food()
    rec = store.get(day)
    if not rec:
        return get_food(day)
    _ensure_ids(rec)
    idx = next((n for n, i in enumerate(rec["items"]) if i.get("id") == item_id), None)
    if idx is None:
        return get_food(day)
    old = rec["items"][idx]
    new_meal = (meal or old.get("meal") or "snack").lower()
    if text and text.strip() and text.strip() != old.get("name"):
        items = parse_food_entry(text)
        if not items:
            raise RuntimeError("couldn't parse that entry")
        for i in items:
            i["meal"] = new_meal
            i["id"] = uuid.uuid4().hex[:8]
        with _FOOD_LOCK:
            store = _load_food()
            rec = store.get(day) or {"items": [], "totals": {}}
            _ensure_ids(rec)
            idx = next((n for n, x in enumerate(rec["items"]) if x.get("id") == item_id), None)
            if idx is None:
                return get_food(day)
            rec["items"][idx:idx + 1] = items
            _recalc_totals(rec)
            _save_food(store)
    else:
        with _FOOD_LOCK:
            store = _load_food()
            rec = store.get(day) or {"items": []}
            it = next((x for x in rec.get("items", []) if x.get("id") == item_id), None)
            if it is None:
                return get_food(day)
            it["meal"] = new_meal
            _recalc_totals(rec)
            _save_food(store)
    return get_food(day)


def copy_food_day(from_day: str, to_day: str = None, meal: str = None) -> dict:
    """Copy items from one day (optionally just one meal) into another day's log — appends
    (doesn't replace), so it composes with whatever's already logged there. No re-parsing:
    items are duplicated as-is with fresh ids, so this never needs the Anthropic API."""
    if not from_day:
        raise RuntimeError("no source day given")
    to_day = to_day or _nz_now().date().isoformat()
    with _FOOD_LOCK:
        store = _load_food()
        src = store.get(from_day)
        if not src or not src.get("items"):
            raise RuntimeError(f"nothing logged on {from_day}")
        # Materialize a real copy up front: if to_day == from_day, rec below IS src, so
        # rec["items"] would be the exact same list as items — appending while iterating
        # that same list never terminates.
        items = list(src["items"])
        if meal:
            items = [i for i in items if (i.get("meal") or "snack").lower() == meal.lower()]
        if not items:
            raise RuntimeError(f"no {meal or 'food'} logged on {from_day}")
        rec = store.setdefault(to_day, {"items": [], "totals": {}})
        _ensure_ids(rec)
        for i in items:
            i = dict(i)
            i["id"] = uuid.uuid4().hex[:8]
            rec["items"].append(i)
        _recalc_totals(rec)
        rec["synced_at"] = datetime.now().isoformat(timespec="seconds")
        _save_food(store)
    return get_food(to_day)


def food_history(days: int = 30) -> list:
    """Daily macro totals for the last `days` (for the food history graph)."""
    store = _load_food()
    metrics = _load_metrics_store()
    cutoff = (_nz_now().date() - timedelta(days=days - 1)).isoformat()
    out = []
    for d in sorted(store):
        if d < cutoff:
            continue
        t = store[d].get("totals") or {}
        if not t:
            continue
        out.append({"t": d, "cal": t.get("cal"), "protein": t.get("protein"),
                    "carbs": t.get("carbs"), "fat": t.get("fat"),
                    "zoe_score": food_day_score(d, store, metrics),
                    "items": store[d].get("items", [])})
    return out


def zoe_day(day: str, store: dict, metrics: dict = None, timing: dict = None) -> dict:
    """ZOE rules for one day (zoe_rules.day_eval): plants count across Monday..day."""
    d = date.fromisoformat(day)
    mon = d - timedelta(days=d.weekday())
    get = lambda k: (store.get(k) or {}).get("items") or []
    items = get(day)
    for i in items:
        if "processing" not in i:
            _score_processing(i)
    week = [i for k in range(d.weekday() + 1) for i in get((mon + timedelta(days=k)).isoformat())]
    if timing is None:
        try:
            timing = meal_timing(day, store, metrics if metrics is not None else {})
        except Exception:
            timing = {}
    return zoe_rules.day_eval(items, week, d.weekday(), timing)


def food_day_score(day: str, store: dict, metrics: dict = None):
    """0-100 ZOE day score, or None when nothing logged / nothing scoreable."""
    if not (store.get(day) or {}).get("items"):
        return None
    return zoe_day(day, store, metrics)["score"]


LATE_MEAL_HOURS = 3        # last food within this many hours of bed counts as a late meal


def _day_hours(hhmm: str) -> float:
    """HH:MM → hours on a 05:00→05:00 day, so 00:30 sorts after 23:00 (24.5)."""
    h, m = map(int, hhmm.split(":"))
    t = h + m / 60
    return t + 24 if t < 5 else t


def _clock(day_hours: float) -> str:
    mins = round(day_hours * 60) % 1440
    return f"{mins // 60:02d}:{mins % 60:02d}"


def meal_timing(day: str, food: dict = None, metrics: dict = None) -> dict:
    """Last thing eaten on `day` vs that night's Oura bedtime (or the usual one, 14-night median)."""
    food = _load_food() if food is None else food
    metrics = _load_metrics_store() if metrics is None else metrics
    times = sorted((i["at"] for i in (food.get(day) or {}).get("items") or [] if i.get("at")), key=_day_hours)
    night = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    # Oura bedtimes are stored as hours from the preceding noon; +12 puts them on the day scale.
    actual = _night_value(metrics, night, "bedtime_hour")
    recent = [v for v in (_night_value(metrics, (date.fromisoformat(day) - timedelta(days=i)).isoformat(), "bedtime_hour")
                          for i in range(14)) if v is not None]
    bed = actual if actual is not None else (statistics.median(recent) if recent else None)
    bed = bed + 12 if bed is not None else None
    out = {"first": times[0] if times else None, "last": times[-1] if times else None,
           "bed": _clock(bed) if bed is not None else None, "bed_is_actual": actual is not None}
    if bed is not None:
        out["kitchen_closes"] = _clock(bed - LATE_MEAL_HOURS)
    if times and bed is not None:
        gap = round(bed - _day_hours(times[-1]), 1)
        out.update({"gap_h": gap, "late": gap < LATE_MEAL_HOURS,
                    "eating_window_h": round(_day_hours(times[-1]) - _day_hours(times[0]), 1)})
    return out


FOOD_RATINGS = ((85, "Nutritious"), (70, "Good"), (50, "Fair"), (0, "Limited"))


def food_rating(day: str, store: dict, metrics: dict = None, zoe: dict = None) -> dict:
    items = (store.get(day) or {}).get("items") or []
    sc = (zoe or {}).get("score") if zoe is not None else food_day_score(day, store, metrics)
    return {"score": sc, "label": None if sc is None else next(l for t, l in FOOD_RATINGS if sc >= t),
            "meals": len({i.get("meal") for i in items})}


def food_week(day: str, store: dict, metrics: dict = None) -> list:
    d = date.fromisoformat(day)
    mon = d - timedelta(days=d.weekday())
    return [{"date": (mon + timedelta(days=i)).isoformat(),
             **food_rating((mon + timedelta(days=i)).isoformat(), store, metrics)}
            for i in range(7)]


FOOD_SUMMARY_FILE = os.path.join(DATA, "food_summaries.json")
FOOD_SUMMARY_SYSTEM = (
    "You write the short daily food note in Mark's health app, in the style of a calm, kind wellness "
    "app. He follows ZOE's rules (30 different plants a week, about 3 small fermented portions a day, "
    "30 g fibre a day, keep ultra-processed food to a minimum, a 12-hour eating window), eats gluten-, "
    "dairy- and egg-free with protein at each meal, and aims to finish eating 3 hours before bed. Given today's log, write JSON only: "
    '{"headline": 2-5 words, sentence case, no full stop (e.g. "A steady afternoon"), '
    '"summary": 2-3 sentences, at most 50 words}. Name one or two real foods he ate that helped, gently '
    "note one thing that may work against him (a medium/high-risk processed item, low fibre, no ferments yet, a late meal), and give "
    "one specific, practical suggestion for the rest of today (or for tomorrow if it is after 8pm). Plain "
    "words, no numbers or scores, no lecturing, never invent foods he didn't log."
)


def food_summary(day: str = None) -> dict:
    """Headline + short note for the day, written by Claude and cached until the log changes."""
    day = day or _nz_now().date().isoformat()
    store = _load_food()
    items = (store.get(day) or {}).get("items") or []
    if not items:
        return {"date": day, "headline": None, "summary": None}
    key = hashlib.md5(json.dumps([[i.get("name"), i.get("meal"), i.get("at")] for i in items]).encode()).hexdigest()
    cache = {}
    if os.path.exists(FOOD_SUMMARY_FILE):
        try:
            with open(FOOD_SUMMARY_FILE) as f:
                cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            cache = {}
    if (cache.get(day) or {}).get("key") == key:
        return {"date": day, **{k: cache[day][k] for k in ("headline", "summary")}}
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in the hub's environment")
    food = get_food(day)
    night = metrics_for(day)
    ctx = {"local_time_now": _nz_now().strftime("%H:%M") if day == _nz_now().date().isoformat() else "end of day",
           "items": [{"name": i.get("name"), "meal": i.get("meal"), "at": i.get("at"),
                      "processing": (i.get("processing") or {}).get("label")} for i in items],
           "totals": food.get("totals"), "targets": food.get("targets"),
           "zoe": {k: (food.get("zoe") or {}).get(k) for k in ("plants_week", "plants_pace", "ferments", "fibre",
                                                               "processing_score", "risky_items", "eating_window_h")},
           "timing": food.get("timing"),
           "last_night": {k: _metric_value(k, night[k]) for k in ("sleep_score", "hrv", "readiness_score")
                          if isinstance(night.get(k), dict)}}
    body = json.dumps({"model": DEFAULT_MODEL, "max_tokens": 300, "system": FOOD_SUMMARY_SYSTEM,
                       "messages": [{"role": "user", "content": json.dumps(ctx)}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                                 headers={"content-type": "application/json", "x-api-key": ANTHROPIC_KEY,
                                          "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = "".join(b.get("text", "") for b in json.loads(resp.read()).get("content", []))
    m = re.search(r"\{.*\}", out, re.S)
    got = json.loads(m.group(0)) if m else {}
    res = {"headline": str(got.get("headline") or "")[:60] or None, "summary": str(got.get("summary") or "")[:400] or None}
    cache[day] = {"key": key, **res}
    for old in sorted(cache)[:-60]:                    # keep two months
        cache.pop(old)
    tmp = f"{FOOD_SUMMARY_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, FOOD_SUMMARY_FILE)
    return {"date": day, **res}


def get_food(day: str = None) -> dict:
    day = day or _nz_now().date().isoformat()
    store = _load_food()
    rec = dict(store.get(day, {"items": [], "totals": {}}))
    rec["targets"] = FOOD_TARGETS
    for i in rec.get("items", []):
        if "processing" not in i:
            _score_processing(i)   # older/copied items may carry the inputs but predate scoring
    try:
        rec["timing"] = meal_timing(day, store)
    except Exception as e:                       # timing is extra; never break the food log
        print(f"[food] meal timing failed: {e}")
    metrics = _load_metrics_store()
    rec["zoe"] = zoe_day(day, store, metrics, rec.get("timing") or {})
    rec["rating"] = food_rating(day, store, metrics, rec["zoe"])
    rec["week"] = food_week(day, store, metrics)
    rec["processing"] = processing_risk.day_summary(rec.get("items"))
    return rec


FOOD_SUGGEST_SYSTEM = (
    "You are Mark's nutrition coach. He is dairy-free, gluten-free and egg-free, 50, training an "
    "MTI strength+endurance block, aiming high-protein and lower-carb. Given today's intake so far "
    "and his remaining macro budget, suggest what to eat for the REST of the day to land near his "
    "targets. Be specific and practical — real foods and rough portions he can actually buy/make, "
    "all DF/GF/EF (no whey — rice protein is his powder). Prioritise hitting the protein target "
    "without blowing past calories or carbs.\n"
    "ZOE RULES — HE FOLLOWS ZOE (TIM SPECTOR); MAKE EVERY SUGGESTION ADVANCE THEM: 30 different "
    "plants a week (veg, fruit, wholegrains, legumes, nuts, seeds; herbs/spices count a quarter) — "
    "favour plants he HASN'T had this week; about 3 small portions of fermented food a day "
    "(kimchi, sauerkraut, kefir/coconut kefir, kombucha, coconut yoghurt with live cultures, "
    "miso, tempeh); 30 g fibre a day; minimal ultra-processed food (nothing a ZOE-style "
    "processed-food risk scale would rate medium/high: emulsifiers, sweeteners, soft refined "
    "breads, sweet drinks); an eating window of 12 hours or less. Never liver or oysters (his "
    "labs show high free copper). You are given his ZOE STATUS for today, including the plants "
    "already eaten this week — steer suggestions to fill the gaps while still hitting the macros.\n"
    "Return concise markdown: a one-line summary of what's left (macros AND ZOE gaps), then 2-4 "
    "suggested meals/snacks as bullets, each with rough macros (e.g. '~40g protein, 350 cal'). "
    "No preamble."
)


def suggest_meals(model: str = None, extra: str = None) -> dict:
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in the hub's environment")
    food = get_food()
    tg, tot = food.get("targets", {}), food.get("totals", {})
    remaining = {k: round((tg.get(k, 0) or 0) - (tot.get(k, 0) or 0)) for k in ("cal", "protein", "carbs", "fat")}
    eaten = [i.get("name") for i in food.get("items", [])]
    user = (
        f"TARGETS: {json.dumps(tg)}\nEATEN SO FAR (totals): {json.dumps(tot)}\n"
        f"REMAINING budget: {json.dumps(remaining)}\nALREADY EATEN: {json.dumps(eaten)}\n"
        f"ZOE STATUS today: {json.dumps({k: (food.get('zoe') or {}).get(k) for k in ('plants_week', 'plants_week_list', 'plants_pace', 'ferments', 'fibre', 'processing_score', 'risky_items', 'eating_window_h', 'todo')})}\n"
        f"Local time now: {datetime.now():%H:%M}."
    )
    if extra and extra.strip():
        user += f"\n\nMark's request for these suggestions (honour it): {extra.strip()[:500]}"
    body = json.dumps({
        "model": model or DEFAULT_MODEL, "max_tokens": 700, "system": FOOD_SUGGEST_SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json", "x-api-key": ANTHROPIC_KEY,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    text = "".join(b.get("text", "") for b in data.get("content", []))
    return {"suggestion": text.strip(), "remaining": remaining}


# --------------------------------------------------------------------------- #
#  Weight goal — status, trend and a Claude plan toward the target weight.
# --------------------------------------------------------------------------- #
TARGET_WEIGHT = CONFIG.get("target_weight", 100)


PLAN_RATE_KG_WK = CONFIG.get("weight_plan_rate_kg_wk", 0.6)   # healthy default loss/gain pace


def _slope_kg_wk(w: dict, days: int):
    ds = [d for d in sorted(w) if d >= (date.today() - timedelta(days=days)).isoformat()]
    # Need enough readings over a long enough span — 2 points a day apart is just water-weight noise.
    if len(ds) < 4:
        return None
    xs = [date.fromisoformat(d).toordinal() for d in ds]
    ys = [w[d] for d in ds]
    if xs[-1] - xs[0] < 14:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den * 7, 3) if den else None


def weight_status(sm: dict = None) -> dict:
    sm = sm if sm is not None else _series_map()
    w = sm.get("weight", {})
    ds = sorted(w)
    history = [{"t": d, "v": round(w[d], 1)} for d in ds]
    # Walk back from the latest reading, skipping any that don't look plausible against the
    # trend before them — protects against an outlier already sitting in the store (e.g. from
    # before this check existed) still being shown as "current".
    current = next((round(w[d], 1) for d in reversed(ds) if _weight_plausible(w[d], d, series=w)),
                   round(w[ds[-1]], 1) if ds else None)
    # recent actual trend (try 180d, fall back to a year of readings)
    rate = _slope_kg_wk(w, 180) or _slope_kg_wk(w, 400)
    to_go = round(current - TARGET_WEIGHT, 1) if current is not None else None

    projection = {}
    if current is not None and to_go:
        need_loss = to_go > 0                      # must the weight go DOWN to reach goal?
        converging = rate is not None and ((need_loss and rate < -0.05) or (not need_loss and rate > 0.05))
        proj_rate = rate if converging else (-PLAN_RATE_KG_WK if need_loss else PLAN_RATE_KG_WK)
        proj_rate = max(-1.2, min(1.2, proj_rate))   # cap to a realistic pace (no water-weight blowups)
        weeks = abs(to_go / proj_rate) if proj_rate else None
        eta = (date.today() + timedelta(weeks=weeks)).isoformat() if (weeks and weeks < 520) else None
        projection = {
            "rate_kg_wk": round(proj_rate, 2), "weeks": round(weeks, 1) if weeks else None,
            "eta": eta, "basis": "trend" if converging else "plan",
            "on_track": bool(converging), "start": date.today().isoformat(),
        }
    return {"current": current, "goal": TARGET_WEIGHT, "to_go": to_go,
            "rate_kg_wk": rate, "projection": projection, "history": history, "unit": "kg",
            "goal_start": CONFIG.get("weight_goal_start")}


WEIGHT_PLAN_SYSTEM = (
    "You are Mark's weight coach. He's 50, training an MTI strength+endurance block, eats "
    "dairy-free / gluten-free / egg-free (no whey) and follows ZOE's rules (30 plants a week, "
    "3 fermented portions and 30 g fibre a day, minimal ultra-processed food, 12-hour eating "
    "window; any food advice must fit them), high-protein. His 2000 kcal daily target is ALREADY a "
    "calorie deficit for him, so consistent adherence should itself produce steady loss — the job "
    "is adherence and protein, not cutting further. Given his current weight, goal weight, "
    "recent trend and daily calorie/macro targets, give a realistic plan to reach the goal plus "
    "honest feedback on his trajectory. Be specific: the weekly rate to aim for, the implied daily "
    "calorie balance vs his target, and concrete actions. If the goal or timeframe is unrealistic "
    "(e.g. losing 10kg in a day), say so plainly and give a sane target/date instead. Concise "
    "markdown: a one-line feedback summary, then a line '### PLAN' with 2-4 bullets, then a line "
    "'### TODAY' with 1-2 actions. No preamble."
)


def weight_plan(model: str = None, extra: str = None) -> dict:
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in the hub's environment")
    ws = weight_status()
    food = get_food()
    tg = food.get("targets", {})
    user = (
        f"CURRENT WEIGHT: {ws['current']} kg\nGOAL: {ws['goal']} kg ({ws['to_go']} kg to go)\n"
        f"RECENT RATE: {ws['rate_kg_wk']} kg/week\nRECENT WEIGHTS: {json.dumps(ws['history'][-8:])}\n"
        f"DAILY CALORIE/MACRO TARGETS: {json.dumps(tg)}\n"
        f"TODAY'S INTAKE SO FAR: {json.dumps(food.get('totals', {}))}\n"
        f"Today is {date.today().isoformat()}."
    )
    if extra and extra.strip():
        user += f"\n\nMark's request (honour it): {extra.strip()[:500]}"
    body = json.dumps({
        "model": model or DEFAULT_MODEL, "max_tokens": 900, "system": WEIGHT_PLAN_SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json", "x-api-key": ANTHROPIC_KEY,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    text = "".join(b.get("text", "") for b in data.get("content", []))
    return {"plan": text.strip(), "status": ws}


# --------------------------------------------------------------------------- #
#  HTTP server.
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        # the app is a single self-updating HTML file — never let clients cache a stale UI
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def log_message(self, *args):
        pass  # quiet

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        day = (q.get("date", [date.today().isoformat()])[0])

        if u.path in ("/", "/index.html"):
            # Prefer an override at /share/health/index.html (editable via Samba, no rebuild),
            # else the version baked into the image/source.
            override = "/share/health/index.html"
            path = override if os.path.exists(override) else os.path.join(HERE, "index.html")
            try:
                with open(path, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, {"error": "index.html not found"})
        elif u.path in ("/workout", "/workout/current", "/workout/today"):
            try:
                self._send(200, current_session())
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path in ("/workout.txt", "/workout/current.txt", "/workout/today.txt"):
            self._send(200, workout_text(), "text/plain; charset=utf-8")
        elif u.path == "/workout/log":
            self._send(200, workout_log_view())
        elif u.path == "/sequence":
            self._send(200, {"cursor": load_state().get("cursor", 0), "sequence": SEQUENCE})
        elif u.path == "/digest/state":
            self._send(200, weekly_digest.load_state())
        elif u.path == "/program":
            try:
                st = load_state()
                cur = st.get("cursor", 0)
                logmap = {e["index"]: e for e in st.get("log", []) if "index" in e}
                seq = []
                for s in SEQUENCE:
                    item = dict(s)
                    i = s["index"]
                    item["status"] = "done" if i < cur else ("current" if i == cur else "upcoming")
                    lg = logmap.get(i)
                    item["matched"] = lg.get("workout") if lg else None   # recorded workout linked to this session
                    item["completed_at"] = lg.get("completed_at") if lg else None
                    seq.append(item)
                self._send(200, {
                    "design": PROGRAM_DATA.get("design", {}),
                    "block": {"name": BLOCK_NAME, "start": BLOCK_START, "weeks": BLOCK_WEEKS,
                              "next_replan": PROGRAM_DATA.get("block", {}).get("next_replan")},
                    "notion_url": NOTION_URL,
                    "cursor": cur,
                    "sequence": seq,
                    "compliance": compliance(),
                    "log": st.get("log", []),
                })
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/metrics":
            try:
                self._send(200, metrics_for(day))
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/history":
            try:
                days = int(q.get("days", ["30"])[0])
            except ValueError:
                days = 30
            try:
                self._send(200, history_series(days))
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/oura/status":
            self._send(200, oura_status())
        elif u.path == "/withings/status":
            self._send(200, withings_status())
        elif u.path == "/insights":
            # This chains together every analytics function (correlations, baselines, trends,
            # etc.) — one exception anywhere in that chain must not 500 the whole dashboard load
            # (the frontend's Promise.all rejects if *any* fetch fails, breaking every panel).
            try:
                self._send(200, insights())
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/checkin":
            try:
                self._send(200, get_checkin(q.get("date", [None])[0]))
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/alcohol":
            try:
                self._send(200, alcohol_status(q.get("date", [None])[0]))
            except ValueError as e:
                self._send(400, {"error": str(e)})
        elif u.path == "/food/usual":
            self._send(200, {"usual": usual_foods()})
        elif u.path == "/food/summary":
            try:
                self._send(200, food_summary(q.get("date", [None])[0]))
            except ValueError as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif u.path == "/today/plan":
            try:
                self._send(200, today_plan())
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/experiments":
            self._send(200, experiments_view())
        elif u.path == "/review":
            try:
                if "via" not in q and _drinking_day().weekday() == 6:   # opened in the app on Sunday:
                    mark_action_done("review")                          # clear Today's nudge
                self._send(200, weekly_review_screen(q.get("date", [None])[0]))
            except ValueError as e:
                self._send(400, {"error": str(e)})
        elif u.path == "/habits":
            self._send(200, configure_habits({}))
        elif u.path == "/training":
            try:
                self._send(200, training_week(q.get("date", [None])[0]))
            except ValueError as e:
                self._send(400, {"error": str(e)})
        elif u.path == "/insights/ranked":
            try:
                self._send(200, ranked_insights())
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/insights/food-sleep":
            try:
                self._send(200, food_sleep_effects(max(7, min(365, int(q.get("days", ["90"])[0])))))
            except ValueError as e:
                self._send(400, {"error": str(e)})
        elif u.path in ("/today", "/targets", "/summary/range"):
            try:
                if u.path == "/today":
                    self._send(200, health_today())
                elif u.path == "/targets":
                    self._send(200, health_targets())
                else:
                    self._send(200, range_summary(q.get("start", [""])[0], q.get("end", [""])[0]))
            except ValueError as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/review/weekly":
            try:
                self._send(200, weekly_review(q.get("end", [None])[0]))
            except ValueError as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/food":
            try:
                self._send(200, get_food(q.get("date", [None])[0]))
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/food/history":
            try:
                days = int(q.get("days", ["30"])[0])
            except ValueError:
                days = 30
            try:
                self._send(200, {"history": food_history(days), "targets": FOOD_TARGETS})
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/weight":
            try:
                self._send(200, weight_status())
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/status":
            try:
                self._send(200, sync_status())
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/workouts":
            self._send(200, {"workouts": sorted(import_workouts().values(), key=lambda w: w["date"]),
                             "applied": load_state().get("applied_workouts", [])})
        elif u.path == "/macrocycle":
            if not MTI_OK:
                self._send(503, {"error": "MTI generator unavailable"}); return
            ms = mti_blocks._macro_state()
            self._send(200, {
                "current_index": ms.get("current_index", -1),
                "total": len(mti_blocks.MACROCYCLE),
                "blocks": [{"index": i, **b} for i, b in enumerate(mti_blocks.MACROCYCLE)],
                "history": ms.get("history", []),
                "bodyweight_mode": PROGRAM_DATA.get("design", {}).get("bodyweight_mode", False),
                "current_block": {"name": BLOCK_NAME, "start": BLOCK_START,
                                  "emphasis": PROGRAM_DATA.get("block", {}).get("emphasis")},
            })
        elif u.path == "/state":
            self._send(200, load_state())
        elif u.path == "/health":
            self._send(200, {"ok": True, "current": current_session()["focus"]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._send(400, {"error": "bad json"})
            return

        if u.path == "/ingest":
            ingest_payload(payload)
            self._send(200, {"ok": True})
        elif u.path == "/checkin":
            self._send(200, {"ok": True, "record": save_checkin(payload)})
        elif u.path in ("/experiments/start", "/experiments/mark", "/experiments/end", "/experiments/promote",
                        "/review/decide", "/review/tick"):
            try:
                out = {"/experiments/start": lambda: start_experiment(payload),
                       "/experiments/mark": lambda: mark_experiment(payload.get("value"), payload.get("date")),
                       "/experiments/end": lambda: end_experiment(),
                       "/experiments/promote": lambda: promote_experiment(payload.get("id", "")),
                       "/review/decide": lambda: decide_proposal(payload),
                       "/review/tick": lambda: tick_week_plan(payload.get("week", ""), payload.get("id", ""))}[u.path]()
                self._send(200, out)
            except (ValueError, TypeError) as e:
                self._send(400, {"error": str(e)})
        elif u.path in ("/habits/mark", "/habits/config", "/actions/done"):
            try:
                if u.path == "/habits/mark":
                    out = mark_habit(payload.get("id", ""), payload.get("value"), payload.get("date"))
                elif u.path == "/actions/done":
                    out = mark_action_done(payload.get("key", ""), payload.get("date"))
                else:
                    out = configure_habits(payload)
                self._send(200, out)
            except (ValueError, TypeError) as e:
                self._send(400, {"error": str(e)})
        elif u.path in ("/training/log", "/training/remove", "/training/settings", "/training/edit"):
            try:
                if u.path == "/training/edit":
                    out = edit_session(payload.get("id", ""), payload.get("kind"), payload.get("zone2"))
                elif u.path == "/training/log":
                    out = log_session(payload.get("kind", ""), payload.get("minutes"), payload.get("date"))
                elif u.path == "/training/remove":
                    out = remove_session(payload.get("id", ""))
                else:
                    out = save_training_settings(payload)
                self._send(200, out)
            except (ValueError, TypeError) as e:
                self._send(400, {"error": str(e)})
        elif u.path in ("/alcohol/drink", "/alcohol/remove", "/alcohol/none", "/alcohol/settings"):
            try:
                if u.path == "/alcohol/drink":
                    out = add_drink(payload)
                elif u.path == "/alcohol/remove":
                    out = remove_drink(payload.get("date", ""), payload.get("id", ""))
                elif u.path == "/alcohol/none":
                    out = mark_no_drinks(payload.get("date"))
                else:
                    out = save_alcohol_settings(payload)
                self._send(200, out)
            except (ValueError, TypeError) as e:
                self._send(400, {"error": str(e)})
        elif u.path in ("/withings/creds", "/withings/code", "/withings/sync"):
            try:
                if u.path == "/withings/creds":
                    out = withings_save_creds(payload.get("client_id", ""), payload.get("client_secret", ""),
                                              payload.get("redirect_uri", ""))
                elif u.path == "/withings/code":
                    out = withings_exchange_code(payload.get("code", ""))
                else:
                    out = withings_sync(int(payload.get("days") or 14))
                self._send(200, out)
            except ValueError as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif u.path == "/oura/creds":
            try:
                self._send(200, oura_save_creds(payload.get("client_id", ""),
                                                payload.get("client_secret", "")))
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif u.path == "/oura/code":
            try:
                self._send(200, oura_exchange_code(payload.get("code", "")))
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif u.path == "/oura/sync":
            # throttle_secs: page-refresh callers pass e.g. 600 — skip if synced recently,
            # so opening the app repeatedly doesn't hammer the Oura API.
            try:
                skipped = False
                if not oura_status()["connected"]:
                    skipped = True
                elif payload.get("throttle_secs"):
                    ls = _oura_tokens_load().get("last_sync")
                    if ls and (datetime.now() - datetime.fromisoformat(ls)).total_seconds() \
                            < int(payload["throttle_secs"]):
                        skipped = True
                self._send(200, {"skipped": True} if skipped
                           else oura_sync(int(payload.get("days") or 7)))
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif u.path == "/food/sync":
            # legacy route (Notion note sync removed) — just returns today's record
            self._send(200, get_food())
        elif u.path == "/food/entry":
            try:
                self._send(200, add_food_entry(payload.get("text", ""), payload.get("meal"),
                                               payload.get("day"), payload.get("at")))
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif u.path == "/food/photo":
            try:
                self._send(200, {"items": parse_food_photo(payload.get("image", ""), payload.get("media_type", ""),
                                                           payload.get("note", ""))})
            except ValueError as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif u.path == "/food/parse":
            try:
                self._send(200, {"items": parse_food_text(payload.get("text", ""))})
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif u.path == "/food/items":
            try:
                self._send(200, add_food_items(payload.get("items"), payload.get("meal"),
                                               payload.get("day"), payload.get("entry_id"),
                                               payload.get("at")))
            except ValueError as e:
                self._send(400, {"error": str(e)})
        elif u.path == "/food/remove":
            try:
                self._send(200, remove_food_item(payload.get("id", ""), payload.get("day")))
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif u.path == "/food/update":
            try:
                self._send(200, update_food_item(payload.get("id", ""), payload.get("text"),
                                                 payload.get("meal"), payload.get("day")))
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif u.path == "/food/copy":
            try:
                self._send(200, copy_food_day(payload.get("from", ""), payload.get("to"),
                                              payload.get("meal")))
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif u.path == "/food/suggest":
            try:
                self._send(200, suggest_meals(payload.get("model"), payload.get("extra")))
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif u.path == "/weight/plan":
            try:
                self._send(200, weight_plan(payload.get("model"), payload.get("extra")))
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif u.path == "/state":
            save_state(payload)
            self._send(200, {"ok": True})
        elif u.path == "/backup":
            fname = f"health-backup-{datetime.now():%Y-%m-%d-%H%M%S}.json"
            with open(os.path.join(BACKUPS, fname), "w") as f:
                json.dump(payload, f, indent=2)
            self._send(200, {"ok": True, "file": fname})
        elif u.path == "/notify":
            notify_current()
            self._send(200, {"ok": True})
        elif u.path == "/card":
            self._send(200, {"ok": push_ha_card()})
        elif u.path == "/workout/log":
            self._send(200, save_workout_log(payload))
        elif u.path == "/done":
            self._send(200, complete_workout(notes=payload.get("notes")))
        elif u.path == "/cursor":
            self._send(200, set_cursor(int(payload.get("cursor", 0))))
        elif u.path in ("/block/next", "/block/regenerate", "/block/bodyweight"):
            try:
                self._send(200, generate_block(u.path.split("/")[-1], payload))
            except Exception as e:
                self._send(502, {"error": str(e)})
        elif u.path == "/trends/ack":
            self._send(200, ack_trends())
        elif u.path == "/coach":
            try:
                self._send(200, anthropic_coach(payload))
            except urllib.error.HTTPError as e:
                self._send(502, {"error": f"anthropic {e.code}: {e.read().decode()[:300]}"})
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif u.path == "/digest/run":
            # dry runs respond inline (preview HTML); real sends go to a background thread
            # because refresh_sources() alone takes ~75s.
            if payload.get("dry"):
                try:
                    self._send(200, weekly_digest.run(dry=True,
                                                      skip_refresh=bool(payload.get("skip_refresh", True))))
                except Exception as e:
                    self._send(502, {"error": str(e)})
            else:
                threading.Thread(target=weekly_digest.run, daemon=True).start()
                self._send(200, {"started": True})
        elif u.path == "/digest/state":
            try:
                weekly_digest.save_state(payload)
                self._send(200, {"ok": True, "keys": sorted(payload)})
            except Exception as e:
                self._send(502, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})


def run_server():
    port = CONFIG.get("port", 8767)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Health hub on http://0.0.0.0:{port}  (app at /, ingest at /ingest)")
    # boot steps must never kill the server — a bad data file would crash-loop the add-on
    for step in (purge_bogus_weights,  # drop BMI/lean-mass readings the old matcher stored as weight
                 apply_workouts,       # sync any recorded workouts into the plan on (re)start
                 push_ha_card):        # refresh the card with the current session on (re)start
        try:
            step()
        except Exception as e:
            print(f"[boot] {step.__name__} failed: {e}")
    _start_background_loop()  # hourly Oura sync + Home Assistant snapshots, inbox, check-in
    threading.Thread(target=backfill_night_detail, daemon=True).start()
    threading.Thread(target=_withings_loop, daemon=True).start()   # weigh-ins every 30 min
    weekly_digest.start_scheduler()  # Mondays 07:30 NZ — cross-app digest email
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "notify":
        notify_current()
    elif cmd == "card":
        push_ha_card()
    elif cmd == "done":
        print(json.dumps(advance_session(notes="cli"), indent=2))
    elif cmd == "workout":
        print(json.dumps(current_session(), indent=2))
    elif cmd == "food":
        print(json.dumps(get_food(), indent=2))
    elif cmd == "reset":
        print(json.dumps(set_cursor(0), indent=2))
    else:
        run_server()
