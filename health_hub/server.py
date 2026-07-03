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
    n = (name or "").lower()
    if "strength" in n or "core_training" in n or "core training" in n:
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
    tmp = WORKOUTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, WORKOUTS_FILE)


def ingest_workouts(workouts: list) -> int:
    """Merge workouts from a Health Auto Export REST payload (data.workouts[]) into the store."""
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
        store[wid] = {
            "id": wid, "date": day, "type": name, "category": workout_category(name),
            "time": start[11:16] if len(start) >= 16 else None,   # HH:MM
            "minutes": round(dur / 60) if isinstance(dur, (int, float)) else None,
            "avg_hr": round(hr) if isinstance(hr, (int, float)) else None,
            "calories": round(kj / 4.184) if isinstance(kj, (int, float)) else None,
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
            if w["category"] == cur["type"]:
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
    if os.path.exists(METRICS_FILE):
        with open(METRICS_FILE) as f:
            return json.load(f)
    return {}


def _save_metrics_store(store: dict):
    tmp = METRICS_FILE + ".tmp"
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
            source = point.get("source", "")
            bucket = store.setdefault(day, {})
            # Oura API sync owns these keys on days it has covered — HAE is the fallback only
            existing = bucket.get(key)
            if key in OURA_KEYS and isinstance(existing, dict) and existing.get("source") == "Oura API":
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
                bucket[key] = {
                    "value": point.get("qty", point.get("value")),
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
    tmp = OURA_TOKENS_FILE + ".tmp"
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


def oura_sync(days: int = 7) -> dict:
    """Pull the last `days` from Oura and fold into the metrics store. Oura becomes the
    source of truth for sleep / HRV / RHR / SpO2 / respiratory rate on days it covers."""
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=days)).isoformat()
    rng = {"start_date": start, "end_date": end}
    sleeps = _oura_get("sleep", rng)
    readiness = _oura_get("daily_readiness", rng)
    dsleep = _oura_get("daily_sleep", rng)
    spo2, stress, resilience = [], [], []
    for name, dest in (("daily_spo2", "spo2"), ("daily_stress", "stress"),
                       ("daily_resilience", "resilience")):
        try:
            got = _oura_get(name, rng)
        except (urllib.error.HTTPError, RuntimeError):
            got = []
        if dest == "spo2":
            spo2 = got
        elif dest == "stress":
            stress = got
        else:
            resilience = got
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
        if s.get("average_hrv") is not None:
            put(d, "hrv", {"value": s["average_hrv"], "unit": "ms", "averaged": True})
        if s.get("lowest_heart_rate") is not None:
            put(d, "resting_hr", {"value": s["lowest_heart_rate"], "unit": "count/min",
                                  "averaged": True})
        if s.get("average_breath") is not None:
            put(d, "respiratory_rate", {"value": s["average_breath"], "unit": "count/min"})
    for r in readiness:
        d = r.get("day")
        if not d:
            continue
        if r.get("score") is not None:
            put(d, "readiness_score", {"value": r["score"], "unit": "score"})
        if r.get("temperature_deviation") is not None:
            put(d, "temp_deviation", {"value": round(r["temperature_deviation"], 2),
                                      "unit": "°C"})
    for s in dsleep:
        d, sc = s.get("day"), s.get("score")
        if d and sc is not None:
            put(d, "sleep_score", {"value": sc, "unit": "score"})
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
    tok = _oura_tokens_load()
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


# Sanity caps — daily values above these are HAE sync glitches; drop them.
SANE_MAX = {"mindful_minutes": 240, "exercise_minutes": 300, "active_energy": 3500}


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
            if key in out or not any(p in eid for p in pats):
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
                if key in SANE_MAX and isinstance(val, (int, float)) and val > SANE_MAX[key]:
                    break    # spike / sync glitch — skip this metric for now
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
            tmp = INTRADAY_FILE + ".tmp"
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


def metrics_for(day: str) -> dict:
    out = dict(_load_metrics_store().get(day, {}))
    if day == date.today().isoformat():
        out.update(todays_metrics())  # daily-averaged live values take precedence for today
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
    }


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
    tmp = BEHAV_FILE + ".tmp"
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
        if key in ("drink", "clean"):
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


def snapshot_today():
    """Record today's metrics into the per-date store, building daily history.
    (HA's recorder doesn't track the hae.* domain, so the hub keeps its own history.)"""
    cur = todays_metrics()
    if not cur:
        return
    store = _load_metrics_store()
    store.setdefault(date.today().isoformat(), {}).update(cur)
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
            for skey, okey in (("deep", "sleep_deep"), ("rem", "sleep_rem")):
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
    tmp = STATE_FILE + ".tmp"
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


def notify_current():
    w = current_session()
    title = "Next workout" if w["type"] != "rest" else "Rest 🛌"
    msg = f"{w['focus']} · {w['summary']}"
    service = CONFIG.get("notify_service", "").strip("/")
    if service:
        _ha_request(f"/api/services/{service}", "POST", {"title": title, "message": msg})


DONE_HELPER = CONFIG.get("done_helper", "input_boolean.workout_done")
DONE_POLL_SECS = CONFIG.get("done_poll_secs", 20)


def _start_done_watch():
    """Poll the HA 'done' toggle; when it flips on, advance the queue and reset it.
    No HA config-file edits needed — the toggle is a UI-created helper."""
    if not (ha_token() and (CONFIG.get("ha_url") or CONFIG.get("ha_url_local"))):
        print("[done] HA not configured — done-watch disabled.")
        return
    domain, _, name = DONE_HELPER.partition(".")

    def loop():
        last_snap = None
        last_checkin = None
        last_inbox_seq = None
        last_oura = 0.0
        while True:
            time.sleep(DONE_POLL_SECS)
            now = time.time()
            if now - last_oura > 3600 and oura_status()["connected"]:
                last_oura = now
                try:
                    oura_sync(7)
                except Exception as e:
                    print("[oura] sync error:", e)
            try:
                states = _ha_request("/api/states", quiet=True)
                if not isinstance(states, list):
                    continue
                byid = {s.get("entity_id"): s for s in states}
                today = date.today().isoformat()
                if today != last_snap:
                    snapshot_today()
                    if last_snap is not None:
                        reset_behaviour_toggles()      # new day → fresh toggles
                    last_snap = today
                else:
                    accumulate_intraday(metrics_from_ha(states))  # running daily averages
                snapshot_behaviours(states)
                # pull workouts the iPhone sent to HA (sensor.workout_inbox holds the raw payload)
                inbox = byid.get("sensor.workout_inbox")
                if inbox:
                    seq = inbox.get("attributes", {}).get("seq")
                    if seq and seq != last_inbox_seq:
                        last_inbox_seq = seq
                        try:
                            payload = json.loads(inbox["attributes"].get("raw") or "{}")
                            n = ingest_workouts(payload.get("data", payload).get("workouts") or [])
                            print(f"[inbox] ingested {n} new workout(s) from HA")
                        except Exception as e:
                            print("[inbox] parse error:", e)
                apply_workouts()        # advance the plan for any new matching workouts
                push_ha_extras()        # keep latest-workout + check-in sensors fresh in HA
                # workout done → read logged weights from the note, store on the
                # session, advance, and write the next session into the note
                done = byid.get(DONE_HELPER)
                if done and done.get("state") == "on":
                    res = complete_workout(notes="marked done in HA")
                    _ha_request(f"/api/services/{domain}/turn_off", "POST", {"entity_id": DONE_HELPER})
                    nx = res["next"]
                    nw = len(res["logged"]["movements"])
                    print(f"[done] logged {nw} weight(s) → advanced to "
                          f"{nx['focus']} ({nx.get('position')}/{nx.get('total')})")
                # evening check-in done (input_button state is a timestamp that changes on press)
                ci = byid.get(CHECKIN_HELPER)
                if ci:
                    st = ci.get("state")
                    if last_checkin is None:
                        last_checkin = st
                    elif st not in (None, "unknown", "unavailable") and st != last_checkin:
                        last_checkin = st
                        finalize_checkin()
            except Exception as e:
                print("[loop] error:", e)

    threading.Thread(target=loop, daemon=True).start()
    print(f"[done] watching workout + check-in, every {DONE_POLL_SECS}s")


# --------------------------------------------------------------------------- #
#  AI coaching — proxied through the hub so the key never reaches the browser.
# --------------------------------------------------------------------------- #
COACH_SYSTEM = (
    "You are Mark's health coach. He's 50, training an MTI-style strength+endurance block. "
    "Most data is automatic (Apple Watch + Oura via Apple Health); he also logs an evening "
    "check-in for alcohol, eating clean, stress, late meals and late caffeine, plus a food log "
    "with daily macro targets (high-protein, lower-carb, 2400 kcal is already a deficit), and a "
    "100 kg weight goal. "
    "Background: dairy-free / gluten-free / egg-free (no whey; rice protein is his powder), "
    "following the Wahls Paleo protocol (9 cups veg daily across greens/sulfur/colour, daily "
    "ferment + seaweed, oily fish/mussels 2-3x wk, organ meat — liver capped at 1x/wk for his "
    "high free copper); and he's sensitive to alcohol — even one drink tanks his sleep/HRV "
    "that night.\n"
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
# entry parsing is a simple estimation job — use the fast model (config food_parse_model to override)
FOOD_PARSE_MODEL = CONFIG.get("food_parse_model") or "claude-haiku-4-5-20251001"
_FOOD_LOCK = threading.Lock()
STANDARD_MEALS_NOTE = CONFIG.get("standard_meals_note", "Standard Meals")
# Daily macro targets — high-protein, lower-carb. Direction: cal/protein/fat have a target to
# reach; carbs is a ceiling (lower is better).
FOOD_TARGETS = CONFIG.get("food_targets") or {"cal": 2400, "protein": 190, "carbs": 140, "fat": 120}


def _load_food() -> dict:
    if os.path.exists(FOOD_FILE):
        with open(FOOD_FILE) as f:
            return json.load(f)
    return {}


def _save_food(store):
    tmp = FOOD_FILE + ".tmp"
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


def read_standard_meals() -> str:
    """The standard/named-meals reference note (macros for common meals he reuses)."""
    return read_note(STANDARD_MEALS_NOTE)


FOOD_SYSTEM = (
    "You convert one short free-text food entry into structured nutrition. It may name foods "
    "(estimate typical macros) and/or give explicit macros (use them). It may describe several "
    "foods at once, separated by commas or 'and' ('200g chicken, cup of rice and broccoli') — "
    "return one item per food. "
    "A STANDARD MEALS reference may be provided — "
    "if the entry names or clearly refers to one of those meals, expand it using the reference's "
    "foods/macros instead of re-estimating. Also classify each item for Wahls Protocol tracking: "
    '"cups" = vegetable/fruit volume in cups (0 for meat/fish/protein powders/oils/etc), and '
    '"group" = which Wahls colour group the produce belongs to — "greens" (leafy greens: kale, '
    'lettuce, rocket, spinach, silverbeet, herbs), "sulfur" (broccoli, cauliflower, cabbage, '
    'brussels, onion, garlic, leek, mushrooms, radish, courgette), "color" (coloured through and '
    "through, any colour incl. green: berries, beetroot, carrot, capsicum, pumpkin, kumara, "
    "citrus, stone fruit, avocado, asparagus, green beans, kiwifruit, green grapes — NOT "
    'pale-fleshed produce like cucumber or celery), or "none". '
    'Add "tags": an array of any that apply — "fermented" (kimchi, sauerkraut, coconut/other '
    'yoghurt, kombucha, kefir, miso), "organ" (liver, heart, kidney, pate), "seaweed" (nori, '
    'kelp, wakame, karengo), "omega3" (oily fish: salmon, sardines, mackerel, herring, tuna; '
    'mussels/oysters; fish oil). Judge Wahls Paleo compliance per item: "wahls_ok" = false for '
    "gluten grains, dairy (incl. whey), eggs, legumes/soy/peanuts, refined sugar or sweets, "
    "processed/packaged junk, seed-oil-fried food, beer, and grain servings beyond rice-sized "
    "moderation; true for everything the protocol allows (meat, fish, veg, fruit, nuts, coconut, "
    'olive oil, rice protein, moderate rice/potato/kumara). When false, add "why": a 1-3 word '
    'reason (e.g. "gluten", "dairy", "legume", "refined sugar", "processed"). Return ONLY JSON: '
    '{"items": [{"name": str, "protein": g, "carbs": g, "fat": g, "cal": kcal, "cups": number, '
    '"group": "greens|sulfur|color|none", "tags": [str], "wahls_ok": bool, "why": str?}]}. '
    "Macros as integers, no units; cups may be a decimal (e.g. 0.5)."
)


def parse_food_entry(text: str) -> list:
    """One free-text entry → list of structured items (macros + Wahls classification)."""
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in the hub's environment")
    if not text.strip():
        return []
    standard = read_standard_meals()
    ref = f"STANDARD MEALS REFERENCE:\n{standard[:4000]}\n\n" if standard.strip() else ""
    body = json.dumps({
        "model": FOOD_PARSE_MODEL, "max_tokens": 1000, "system": FOOD_SYSTEM,
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
    return (json.loads(m.group(0)) if m else {}).get("items") or []


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
    return hashlib.md5(FOOD_SYSTEM.encode()).hexdigest()[:8]


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
    tmp = FOOD_CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, FOOD_CACHE_FILE)


def add_food_entry(text: str, meal: str, day: str = None) -> dict:
    """Parse a free-text entry (comma-separated = multiple foods) and append to the day's log.
    Repeat foods hit a local cache (instant, no API call); uncached parts parse in parallel."""
    day = day or date.today().isoformat()
    meal = (meal or "snack").lower()
    if meal not in ("breakfast", "lunch", "dinner", "snack"):
        meal = "snack"
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
    with _FOOD_LOCK:
        store = _load_food()
        rec = store.setdefault(day, {"items": [], "totals": {}})
        _ensure_ids(rec)
        for i in items:
            i["meal"] = meal
            i["id"] = uuid.uuid4().hex[:8]
            rec["items"].append(i)
        _recalc_totals(rec)
        rec["synced_at"] = datetime.now().isoformat(timespec="seconds")
        _save_food(store)
    return get_food(day)


def remove_food_item(item_id: str, day: str = None) -> dict:
    day = day or date.today().isoformat()
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
    day = day or date.today().isoformat()
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


def food_history(days: int = 30) -> list:
    """Daily macro totals for the last `days` (for the food history graph)."""
    store = _load_food()
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    out = []
    for d in sorted(store):
        if d < cutoff:
            continue
        t = store[d].get("totals") or {}
        if not t:
            continue
        out.append({"t": d, "cal": t.get("cal"), "protein": t.get("protein"),
                    "carbs": t.get("carbs"), "fat": t.get("fat"),
                    "items": store[d].get("items", [])})
    return out


WAHLS_CUP_TARGETS = {"greens": 3, "sulfur": 3, "color": 3}


def wahls_summary(items: list) -> dict:
    """Daily Wahls Protocol tally from parsed food items: cups per colour group
    plus which signature foods (ferment/seaweed/organ/omega-3) appeared."""
    cups = {"greens": 0.0, "sulfur": 0.0, "color": 0.0}
    tags = {"fermented": 0, "organ": 0, "seaweed": 0, "omega3": 0}
    for i in items or []:
        g = i.get("group")
        if g in cups:
            cups[g] += float(i.get("cups") or 0)
        for t in i.get("tags") or []:
            if t in tags:
                tags[t] += 1
    return {"cups": {k: round(v, 1) for k, v in cups.items()},
            "cup_targets": WAHLS_CUP_TARGETS,
            "cups_total": round(sum(cups.values()), 1),
            "tags": tags,
            "off_plan": sum(1 for i in items or [] if i.get("wahls_ok") is False)}


def _wahls_week_count(store: dict, day: str, tag: str) -> int:
    """Servings carrying `tag` logged in the 7 days ending `day`."""
    end = date.fromisoformat(day or date.today().isoformat())
    start = (end - timedelta(days=6)).isoformat()
    n = 0
    for d, rec in store.items():
        if start <= d <= end.isoformat():
            n += sum(1 for i in rec.get("items", []) if tag in (i.get("tags") or []))
    return n


def get_food(day: str = None) -> dict:
    day = day or date.today().isoformat()
    store = _load_food()
    rec = dict(store.get(day, {"items": [], "totals": {}}))
    rec["targets"] = FOOD_TARGETS
    rec["wahls"] = wahls_summary(rec.get("items"))
    rec["wahls"]["organ_week"] = _wahls_week_count(store, day, "organ")
    rec["wahls"]["omega3_week"] = _wahls_week_count(store, day, "omega3")
    return rec


FOOD_SUGGEST_SYSTEM = (
    "You are Mark's nutrition coach. He is dairy-free, gluten-free and egg-free, 50, training an "
    "MTI strength+endurance block, aiming high-protein and lower-carb. Given today's intake so far "
    "and his remaining macro budget, suggest what to eat for the REST of the day to land near his "
    "targets. Be specific and practical — real foods and rough portions he can actually buy/make, "
    "all DF/GF/EF (no whey — rice protein is his powder). Prioritise hitting the protein target "
    "without blowing past calories or carbs.\n"
    "WAHLS PROTOCOL — HE FOLLOWS WAHLS PALEO; MAKE EVERY SUGGESTION ADVANCE IT. Daily structure: "
    "9 cups of vegetables/fruit — 3 cups leafy greens, 3 cups sulfur-rich (broccoli, cauliflower, "
    "cabbage, onion, garlic, mushrooms), 3 cups deeply coloured (berries, beetroot, carrot, "
    "capsicum, pumpkin, kumara). Daily: one fermented food (kimchi, sauerkraut, coconut yoghurt) "
    "and a seaweed serving (nori, kelp). Weekly: oily fish or mussels 2-3x for omega-3s, and "
    "organ meat ~2x — but CAP LIVER AT ONE SERVING/WEEK and favour NZ green-lipped mussels "
    "instead (his labs show high free copper; liver and oysters are the most copper-dense foods). "
    "Grass-fed meat where it matters; minimise grains (rice max ~1 serving/day), no legumes. "
    "You are given his WAHLS STATUS for today — steer suggestions to fill whichever cups/signature "
    "foods are still missing, while still hitting the macros.\n"
    "Return concise markdown: a one-line summary of what's left (macros AND Wahls gaps), then 2-4 "
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
        f"WAHLS STATUS today (cups eaten vs 3/3/3 targets, signature-food counts, organ servings "
        f"this week): {json.dumps(food.get('wahls', {}))}\n"
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
    current = round(w[ds[-1]], 1) if ds else None
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
            "rate_kg_wk": rate, "projection": projection, "history": history, "unit": "kg"}


WEIGHT_PLAN_SYSTEM = (
    "You are Mark's weight coach. He's 50, training an MTI strength+endurance block, eats "
    "dairy-free / gluten-free / egg-free (no whey) on the Wahls Paleo protocol (9 cups veg/day, "
    "daily ferment + seaweed, oily fish/mussels; any food advice must fit it), high-protein. His 2400 kcal daily target is ALREADY a "
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
            self._send(200, current_session())
        elif u.path in ("/workout.txt", "/workout/current.txt", "/workout/today.txt"):
            self._send(200, workout_text(), "text/plain; charset=utf-8")
        elif u.path == "/workout/log":
            self._send(200, workout_log_view())
        elif u.path == "/sequence":
            self._send(200, {"cursor": load_state().get("cursor", 0), "sequence": SEQUENCE})
        elif u.path == "/program":
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
        elif u.path == "/metrics":
            self._send(200, metrics_for(day))
        elif u.path == "/history":
            try:
                days = int(q.get("days", ["30"])[0])
            except ValueError:
                days = 30
            self._send(200, history_series(days))
        elif u.path == "/oura/status":
            self._send(200, oura_status())
        elif u.path == "/insights":
            self._send(200, insights())
        elif u.path == "/checkin":
            self._send(200, get_checkin(q.get("date", [None])[0]))
        elif u.path == "/food":
            self._send(200, get_food(q.get("date", [None])[0]))
        elif u.path == "/food/history":
            try:
                days = int(q.get("days", ["30"])[0])
            except ValueError:
                days = 30
            self._send(200, {"history": food_history(days), "targets": FOOD_TARGETS})
        elif u.path == "/weight":
            self._send(200, weight_status())
        elif u.path == "/status":
            self._send(200, sync_status())
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
                                               payload.get("day")))
            except Exception as e:
                self._send(502, {"error": str(e)})
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
        else:
            self._send(404, {"error": "not found"})


def run_server():
    port = CONFIG.get("port", 8767)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Health hub on http://0.0.0.0:{port}  (app at /, ingest at /ingest)")
    apply_workouts()     # sync any recorded workouts into the plan on (re)start
    push_ha_card()       # refresh the card with the current session on (re)start
    _start_done_watch()  # advance the queue when the HA 'done' toggle flips on
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
