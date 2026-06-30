#!/usr/bin/env python3
"""
mti_blocks.py — generate 4-week training blocks for the health app from the MTI database,
following a 12-month macrocycle appropriate for a 50yo masters tactical athlete (SF50 band).

Produces a program.json-compatible block (same schema server.py already consumes), so the
existing queue / cursor / progression engine works unchanged. The MTI DB supplies *movement
variety* per block; the proven weekday structure + rep/duration waves stay.

Each loaded exercise carries a bodyweight substitute, so the whole block can be swapped to
no-gym on demand (detail vs detail_bw; the app toggles).

CLI:
  python3 mti_blocks.py macrocycle              # show the 12-month plan
  python3 mti_blocks.py preview <n>             # preview block n (0-based) without writing
  python3 mti_blocks.py next [--start YYYY-MM-DD] [--bodyweight]   # write next block -> program.json
  python3 mti_blocks.py regenerate              # re-roll the current block (new variety)
"""
import argparse
import json
import os
import random
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = next((p for p in [HERE / "mti" / "mti.sqlite",
                            Path.home() / "mti-programs" / "db" / "mti.sqlite"] if p.exists()), None)
STATE = HERE / "data" / "macro_state.json"      # which block of the macrocycle we're on
PROGRAM_JSON = HERE / "program.json"
BANNED_FILE = HERE / "data" / "banned_exercises.json"   # names never selected by the generator


def load_banned():
    try:
        return [str(x) for x in json.loads(BANNED_FILE.read_text())]
    except Exception:
        return []


def save_banned(names):
    BANNED_FILE.parent.mkdir(exist_ok=True)
    BANNED_FILE.write_text(json.dumps(sorted(set(names)), indent=2))


BANNED = load_banned()

# His available equipment (override via config.json -> mti_equipment). Mark trains with
# barbell/bench, sandbag, kettlebell, box, pull-up bar, ruck.
DEFAULT_EQUIP = ["barbell", "dumbbell", "kettlebell", "sandbag", "box", "bodyweight", "ruck", "cardio", "none"]
BODYWEIGHT_EQUIP = ["bodyweight", "ruck", "cardio", "none"]

# --------------------------------------------------------------------------- #
# 12-month macrocycle: 13 x 4-week blocks. 3:1 loading (every 4th = deload) is
# age-appropriate for 50yo recovery. Each block maps to an MTI SF template (for
# periodization shape) + an emphasis that modulates volume and movement bias.
# --------------------------------------------------------------------------- #
# 12-month plan as four 8-week development PHASES (accumulate -> intensify) each capped by a
# 4-week deload, then a peak. 8-week phases give an adaptation time to actually develop
# (Galpin), while the deloads keep 3:1 loading age-appropriate. 13 blocks = 52 weeks.
MACROCYCLE = [
    {"name": "Strength — Accumulate",  "emphasis": "strength",      "stage": "accumulate", "template": "SF50 Alpha",     "focus": "Strength phase wk1-4: groove the lifts, build volume."},
    {"name": "Strength — Intensify",   "emphasis": "strength",      "stage": "intensify",  "template": "SF50 Bravo",     "focus": "Strength phase wk5-8: heavier, lower reps — peak strength."},
    {"name": "Deload",                 "emphasis": "deload",        "stage": "deload",     "template": "SF45 Hotel",     "focus": "Recover — low volume, easy intervals, mobility."},
    {"name": "Work Cap — Accumulate",  "emphasis": "work_capacity", "stage": "accumulate", "template": "SF50 Charlie",   "focus": "Capacity phase wk1-4: build the engine under fatigue."},
    {"name": "Work Cap — Intensify",   "emphasis": "work_capacity", "stage": "intensify",  "template": "SF45 Charlie V2","focus": "Capacity phase wk5-8: longer, harder grinds."},
    {"name": "Deload",                 "emphasis": "deload",        "stage": "deload",     "template": "SF45 Delta V2",  "focus": "Recover — low volume, easy intervals, mobility."},
    {"name": "Endurance — Accumulate", "emphasis": "endurance",     "stage": "accumulate", "template": "SF45 Bravo V2",  "focus": "Aerobic phase wk1-4: longer Zone 2 base."},
    {"name": "Endurance — Intensify",  "emphasis": "endurance",     "stage": "intensify",  "template": "SF45 Golf",      "focus": "Aerobic phase wk5-8: peak Zone 2 + ruck volume."},
    {"name": "Deload",                 "emphasis": "deload",        "stage": "deload",     "template": "SF45 Echo",      "focus": "Recover — low volume, easy intervals, mobility."},
    {"name": "Power — Accumulate",     "emphasis": "power",         "stage": "accumulate", "template": "SF50 Alpha",     "focus": "Power phase wk1-4: explosive volume, speed-strength."},
    {"name": "Power — Intensify",      "emphasis": "power",         "stage": "intensify",  "template": "SF45 Foxtrot",   "focus": "Power phase wk5-8: max velocity + heavy contrast."},
    {"name": "Deload",                 "emphasis": "deload",        "stage": "deload",     "template": "SF50 Bravo",     "focus": "Recover — low volume, easy intervals, mobility."},
    {"name": "Stamina / Peak",         "emphasis": "stamina",       "stage": "peak",       "template": "SF45 Golf",      "focus": "Peak — sustained top-end output, then reassess."},
]


def _wave(emphasis, stage):
    """Base wave for the emphasis, transformed by phase stage: 'intensify' goes heavier (and a
    touch more aerobic) for the 2nd 4 weeks of an 8-week phase; 'peak' is heaviest; others base."""
    w = {k: list(v) for k, v in EMPHASIS_WAVES[emphasis].items()}
    if stage in ("intensify", "peak"):
        w["reps"] = [max(2, r - 1) for r in w["reps"]]
        w["sat"] = [d + 5 for d in w["sat"]]
    return w

# emphasis -> 4-week waves for the progression placeholders {reps}{wcap}{tue}{sat}.
# Strength waves run 3-5 reps (Galpin). The STRUCTURED sessions stay short (rounds + short
# Fri grind), but Zone 2 is easy aerobic time — restored toward Attia's ~3 hr/wk; Saturday is
# the one long easy day (ruck/spin), peaking in the endurance phase.
EMPHASIS_WAVES = {
    "strength":      {"reps": [5, 4, 3, 3],  "wcap": [10, 12, 12, 10], "tue": [50, 55, 55, 50], "sat": [75, 85, 90, 75]},
    "power":         {"reps": [3, 3, 2, 3],  "wcap": [10, 12, 12, 10], "tue": [50, 50, 55, 55], "sat": [75, 80, 85, 75]},
    "work_capacity": {"reps": [6, 8, 8, 10], "wcap": [16, 18, 20, 22], "tue": [50, 55, 55, 50], "sat": [70, 80, 80, 70]},
    "endurance":     {"reps": [6, 6, 7, 8],  "wcap": [10, 12, 12, 12], "tue": [55, 60, 65, 70], "sat": [90, 100, 110, 90]},
    "stamina":       {"reps": [5, 5, 4, 4],  "wcap": [16, 18, 20, 20], "tue": [50, 55, 60, 55], "sat": [75, 85, 90, 75]},
    "deload":        {"reps": [5, 4, 4, 5],  "wcap": [10, 10, 10, 8],  "tue": [40, 45, 40, 35], "sat": [55, 60, 55, 45]},
}
# per emphasis, how many strength rounds (trimmed to keep strength days < 60 min)
EMPHASIS_ROUNDS = {"strength": 4, "power": 4, "work_capacity": 3, "endurance": 3, "stamina": 4, "deload": 3}

# weekday role -> the movement patterns its main work draws from. Strength days now open with
# a power slot (Galpin: power declines fastest with age — train it first, fresh). Fri is the
# weekly VO2max session (Attia/Galpin zone-5) + a short grind.
DAY_PATTERNS = {
    "Mon": ("Strength — Heavy: Hinge + Push", "strength",     [("hinge", "main"), ("push", "main")]),
    "Tue": ("Endurance — Zone 2",             "endurance",    []),
    "Thu": ("Power + Muscle: Squat + Pull",   "strength",     [("squat", "main"), ("pull", "main")]),
    "Fri": ("VO2max + Work Capacity",         "conditioning", [("push", "circuit"), ("squat", "circuit"), ("hinge", "circuit"), ("core", "circuit")]),
    "Sat": ("Endurance — long Zone 2",        "endurance",    []),
}

# explosive movements for the power slot (canonical; MTI data is too messy here). 40-60% / max
# intent, low reps, full recovery — the Galpin power prescription.
POWER_MOVES = ["Box Jump", "Broad Jump", "Vertical Jump", "Med-Ball Slam", "Med-Ball Chest Pass",
               "Med-Ball Rotational Throw", "Kettlebell Swing", "Squat Jump", "Speed Trap-Bar Pull"]


def _con():
    if not DB_PATH:
        raise SystemExit("MTI database not found — run mti_db.py and mti_backup.sh first.")
    return sqlite3.connect(DB_PATH)


def _pool(con, equip):
    pool = {}
    for nm, eq, pat, fr in con.execute(
            "SELECT name,equipment,pattern,freq FROM exercises WHERE freq>=4"):
        nm = re.sub(r"^(max reps?|max|amrap|sub ?max)\s+", "", nm, flags=re.I).strip()
        if any(b.lower() in nm.lower() for b in BANNED) or "corrective" in nm.lower():
            continue                                   # user-banned or rehab/corrective
        if eq in equip and 2 <= len(nm) <= 32 and not re.search(r"\d{2,}|time$|for time", nm, re.I):
            pool.setdefault(pat, []).append((nm, eq, fr))
    for p in pool:
        pool[p].sort(key=lambda x: -x[2])
    return pool


def _bw_pool(con):
    return _pool(con, BODYWEIGHT_EQUIP)


# soft/rehab/mobility terms that read as "old-man-ish" — never used as a main strength lift
_SOFT = re.compile(r"(?i)corrective|bridge|dead ?bug|bird ?dog|clamshell|band(ed)?|wall sit|"
                   r"isometric|\bhold\b|stretch|mobility|dislocate|instep|foam|pigeon|cossack|"
                   r"scorpion|glute|warm|cat ?cow|\bdip\b|crunch|march|unloaded|heel tap")
# cardio/endurance words that mean a name isn't a strength lift (even if mis-tagged by pattern)
_NOTSTRENGTH = re.compile(r"(?i)\b(run|spin|bike|ruck|sprint|shuttle|skip|jog|swim|elliptical|moderate|"
                          r"jump|jumps|hop|bound|plyo|burpee)\b|jump rope|row,|,\s*or\b")


# PURE strength lifts Mark wants as main work (selected by NAME — equipment tags in the data
# are unreliable). These rank top; ballistic/combo moves (swing, thruster, snatch) fall back.
_PURE = re.compile(r"(?i)back squat|front squat|goblet squat|\bsquat\b|dead ?lift|romanian|"
                   r"bench press|incline|military press|overhead press|\bpush press\b|"
                   r"bent ?over row|\b1-?arm row\b|weighted pull ?up|power clean|\bclean\b")


# Canonical barbell lifts the MTI data lacks (MTI programs hinge via cleans/sandbag). Injected
# at top priority so real deadlifts appear. Trap-bar is the default "basic hinge" per Mark.
CANONICAL = {
    "hinge": [("Trap-Bar Deadlift", "barbell", 9999), ("Romanian Deadlift", "barbell", 400),
              ("Conventional Deadlift", "barbell", 300)],
}


def _strength_pool(con, equip):
    """Main strength lifts = classic barbell / dumbbell work, picked by name. Excludes sandbag
    (Mark's preference), soft/corrective/cardio/plyo. Sourced across the library so real lifts
    (Back Squat, Deadlift, Bench/DB Press, Power Clean, Bent-Over Row, Pull-Up) are available."""
    pool = {}
    rows = con.execute("""
        SELECT p.exercise, p.equipment, p.pattern, COUNT(*) n
        FROM prescriptions p
        WHERE p.section = 'training' AND p.pattern IN ('squat','hinge','push','pull')
        GROUP BY p.exercise, p.equipment, p.pattern""")
    for nm, eq, pat, n in rows:
        nm = re.sub(r"^(max reps?|max|amrap|sub ?max)\s+", "", nm, flags=re.I).strip()
        low = nm.lower()
        if any(b.lower() in low for b in BANNED) or "corrective" in low or "sandbag" in low:
            continue
        if _SOFT.search(nm) or _NOTSTRENGTH.search(nm):
            continue
        if not (3 <= len(nm) <= 28) or re.search(r"\d{2,}|time$|for time|work up|1rm|,\s*or", nm, re.I):
            continue
        # rank: pure strength lifts first; then other loaded/pull-up work; then the rest
        if _PURE.search(nm):
            pref = 0
        elif re.search(r"(?i)barbell|dumbbell|\bdb\b|kettlebell|\bkb\b|pull ?up|chin ?up", nm):
            pref = 1
        else:
            pref = 2
        pool.setdefault(pat, []).append((nm, eq, n, pref))
    out = {}
    for p, lst in pool.items():
        lst.sort(key=lambda x: (x[3], -x[2]))
        out[p] = [(nm, eq, n) for nm, eq, n, pref in lst]
    # inject canonical barbell lifts at the front (trap-bar deadlift etc.)
    for pat, extra in CANONICAL.items():
        have = {nm.lower() for nm, _, _ in out.get(pat, [])}
        out[pat] = [e for e in extra if e[0].lower() not in have] + out.get(pat, [])
    return out


def _pick(pool, pattern, rng, used, top=14):
    cands = [c for c in pool.get(pattern, []) if c[0] not in used] or pool.get(pattern, [])
    return rng.choice(cands[:top]) if cands else None


def _bw_sub(bwpool, pattern, rng, used):
    c = _pick(bwpool, pattern, rng, used)
    return c[0] if c else None


def _strength_detail(con, bwpool, pool, spool, day_role, emphasis, stage, rng):
    """Build a strength day: stability/eccentric prep -> power -> 2 main lifts (@ {reps} wave)
    -> hypertrophy back-off (accumulate only) -> chassis finisher. Each main carries a BW sub."""
    rounds = EMPHASIS_ROUNDS[emphasis]
    slots = DAY_PATTERNS[day_role][2]
    used, subs, names = set(), [], []
    is_muscle = (day_role == "Thu")          # Thu = power + hypertrophy; Mon = heavy strength
    lines = ["(Target ~50 min — keep rest tight.)",
             "Warm-up + stability ~6 min: WGS + band pull-aparts, then 8/side Single-Leg RDL "
             "(slow) + 10/side Pallof Press (anti-rotation)."]
    # POWER: concentrated on Thu (the power day); also on Mon during a power-emphasis block
    if is_muscle or emphasis == "power":
        moves = [m for m in POWER_MOVES if not any(b.lower() in m.lower() for b in BANNED)]
        rng.shuffle(moves)
        if emphasis == "power":
            lines.append(f"Power (first, fresh) — 5 x 2 {moves[0]} (max velocity) + 3 x 3 {moves[1]} (contrast).")
        elif emphasis == "deload":
            lines.append(f"Power — 3 x 3 {moves[0]}: crisp but submaximal (deload).")
        else:
            lines.append(f"Power (first, fresh) — 5 x 3 {moves[0]}: explosive intent, max speed.")
    # MAIN LIFTS — heavy/low-rep on Mon, hypertrophy on Thu
    first = True
    for pat, kind in slots:
        if kind != "main":
            continue
        c = _pick(spool, pat, rng, used, top=5) or _pick(pool, pat, rng, used)
        if not c:
            continue
        used.add(c[0]); names.append(c[0])
        bw = _bw_sub(bwpool, pat, rng, used)
        sub = f"  (no-gym: {bw})" if bw and c[1] != "bodyweight" else ""
        antag = "Chin-Up" if pat == "pull" else "Pull-Up"
        if is_muscle:                        # muscle: moderate reps, tempo, more time under tension
            lines.append(f"Hypertrophy — 3 sets x 8-12 {c[0]} @ ~70% (2-sec lower) + 8-10 {antag}.{sub}")
        else:                                # heavy strength from the wave
            ecc = "  3-sec eccentric on the first set." if first else ""
            lines.append(f"Strength — {rounds} rounds: {c[0]} @ {{reps}} reps + 3-5 {antag}.{sub}{ecc}")
        if bw:
            subs.append({"exercise": c[0], "pattern": pat, "bodyweight": bw})
        first = False
    # one short finisher
    c = _pick(pool, "core", rng, used) or _pick(pool, "carry", rng, used)
    if c:
        lines.append(f"Finisher — 3 rounds: 10 {c[0]}.")
    lines.append("Cool-down: 2 min easy + 30s dead hang.")
    summary = (("Power + " + " + ".join(names) + " (8-12 reps, muscle)") if is_muscle
               else ("Heavy: " + " + ".join(names) + " @ {reps} reps"))
    return "\n".join(lines), subs, summary


def _workcap_detail(con, bwpool, pool, emphasis, rng):
    _, _, slots = DAY_PATTERNS["Fri"]
    used, picks, subs = set(), [], []
    for pat, _ in slots:
        c = _pick(pool, pat, rng, used)
        if c:
            used.add(c[0]); picks.append(c)
            bw = _bw_sub(bwpool, pat, rng, used)
            if bw and c[1] != "bodyweight":
                subs.append({"exercise": c[0], "pattern": pat, "bodyweight": bw})
    circuit = " + ".join(f"{6 if i<2 else 10} {c[0]}" for i, c in enumerate(picks))
    if emphasis == "deload":          # real systemic deload — easier intervals, short grind
        vo2 = "VO2max (eased) — 3 x 3 min @ Zone 4 (moderate-hard, RPE 6-7) + 3 min easy."
    elif emphasis in ("work_capacity", "stamina"):   # grind-led: shorter intervals, longer grind
        vo2 = "VO2max — 3 x 3 min @ Zone 5 (hard) + 3 min easy."
    else:                             # VO2max-led day
        vo2 = ("VO2max (the priority) — 4 x 4 min @ Zone 5 (hard, ~90% max HR) + 3 min easy. "
               "Run / row / bike / uphill ruck — pick one.")
    detail = ("(Target ~50 min.)\n"
              "Warm-up — 6 min easy build to threshold.\n"
              f"{vo2}\n"
              f"Short grind finisher — {{wcap}} min: {circuit}.\n"
              "Cool-down: 3 min easy + hip-flexor stretch.")
    return detail, subs


# static movement -> bodyweight map, for substituting ANY workout text (incl. the
# current hand-tuned block which has no precomputed bodyweight variant).
_BW_MOVES = [
    (r"(?i)\b(box squat|back squat|front squat|goblet squat|barbell squat)\b", "Bulgarian Split Squat"),
    (r"(?i)\b(hinge lift|dead\s*lift|good morning|romanian|rdl)\b", "Single-Leg Hip Bridge"),
    (r"(?i)\b(incline bench|bench press|push press|overhead press|strict press|press)\b", "Hand-Release Push-Up"),
    (r"(?i)\b(sandbag get[ -]?up|turkish get[ -]?up|get[ -]?up)\b", "Get-Up (no weight)"),
    (r"(?i)\b(kb swing|kettlebell swing|swing)\b", "Burpee"),
    (r"(?i)\b(clean (?:\& |and )?press|cross[- ]?clean|clean)\b", "Squat Jump"),
    (r"(?i)\b(farmer carry|suitcase carry|bear[- ]?hug walk|carry)\b", "Walking Lunge"),
    (r"(?i)\b(snatch)\b", "Burpee"),
]


def bodyweight_substitute(text):
    """Convert an arbitrary workout-detail string to a bodyweight version: swap loaded
    movements for bodyweight equivalents and drop external loads."""
    if not text:
        return text
    out = text
    for rx, repl in _BW_MOVES:
        out = re.sub(rx, repl, out)
    out = re.sub(r"\s*\(no-gym:[^)]*\)", "", out)            # already-embedded hints
    out = re.sub(r"@\s*[\d./]+\s*(kg|#|lb|lbs|in)\b", "(bodyweight)", out, flags=re.I)
    out = re.sub(r"@\s*\d+/\d+#?", "(bodyweight)", out)
    out = re.sub(r"\s*@\s*~?\d+%", "", out)                  # drop % loads for bodyweight
    return out


def _bodyweight_variant(detail, subs):
    """Rewrite a detail string to its bodyweight form using the subs map."""
    out = detail
    for s in subs:
        out = out.replace(s["exercise"], s["bodyweight"])
    out = re.sub(r"\s*\(no-gym:[^)]*\)", "", out)
    out = re.sub(r"@ \d+/\d+kg", "(bodyweight)", out)
    return out


def build_block(idx, start_iso, equip=None, bodyweight=False, seed=None):
    """Return a program.json-compatible dict for macrocycle block `idx`."""
    global BANNED
    BANNED = load_banned()                      # always honour the latest ban list
    spec = MACROCYCLE[idx % len(MACROCYCLE)]
    emphasis = spec["emphasis"]
    stage = spec.get("stage", "accumulate")
    equip = equip or DEFAULT_EQUIP
    rng = random.Random(seed if seed is not None else (idx * 101 + 7))
    con = _con()
    pool, bwpool, spool = _pool(con, equip), _bw_pool(con), _strength_pool(con, equip)
    wave = _wave(emphasis, spec.get("stage", "accumulate"))

    sessions = {}
    all_subs = {}
    for wd in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
        if wd in ("Wed", "Sun"):
            sessions[wd] = {"focus": "REST", "type": "rest",
                            "summary": "REST — walk / mobility only (non-negotiable)",
                            "detail": f"Full rest day. {wd} rest is non-negotiable."}
            continue
        focus, typ, _ = DAY_PATTERNS[wd]
        if typ == "endurance":
            dur = "{tue}" if wd == "Tue" else "{sat}"
            long = " long / ruck @ 20kg" if wd == "Sat" else ""
            detail = (f"Zone 2{long} — run / row / bike / spin / ruck / hike. Conversational, HR 125-135.\n"
                      f"Duration: {dur} min.\nCool-down: 5 min easy + calf/hip stretch.")
            sessions[wd] = {"focus": focus, "type": typ,
                            "summary": f"Zone 2 {dur} min · HR 125-135", "detail": detail,
                            "detail_bw": detail}
        elif typ == "conditioning":
            detail, subs = _workcap_detail(con, bwpool, pool, emphasis, rng)
            sessions[wd] = {"focus": focus, "type": typ, "summary": "{wcap}-min grind + KB finisher",
                            "detail": detail, "detail_bw": _bodyweight_variant(detail, subs)}
            if subs:
                all_subs[wd] = subs
        else:  # strength
            detail, subs, summary = _strength_detail(con, bwpool, pool, spool, wd, emphasis, stage, rng)
            sessions[wd] = {"focus": focus, "type": typ, "summary": summary,
                            "detail": detail, "detail_bw": _bodyweight_variant(detail, subs)}
            if subs:
                all_subs[wd] = subs
    con.close()

    start = date.fromisoformat(start_iso)
    week_params = {str(w + 1): {k: wave[k][w] for k in ("reps", "wcap", "tue", "sat")} for w in range(4)}
    return {
        "notion_url": "",
        "design": {
            "athlete": "Mark, 50 — MTI SF50 band, generated from MTI database",
            "style": "Mountain Tactical Institute (MTI) — SF50/SF45",
            "block_weeks": 4,
            "training_weekdays": ["Mon", "Tue", "Thu", "Fri", "Sat"],
            "include_rest_in_queue": False,
            "replan_cadence": "4-week blocks; generate next on demand",
            "macrocycle_index": idx,
            "macrocycle_total": len(MACROCYCLE),
            "emphasis": emphasis,
            "week_shape": {wd: (DAY_PATTERNS[wd][0] if wd in DAY_PATTERNS else "REST")
                           for wd in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]},
            "source_template": spec["template"],
            "bodyweight_mode": bodyweight,
            "progression_rules": [
                f"Emphasis: {emphasis}. Main lifts wave {wave['reps']} reps (heavy, ~3-5 = true "
                "strength per Galpin); add load, hold the reps.",
                "Power first on strength days — 5x3 explosive (Galpin: power fades fastest after 50).",
                "Fri = weekly VO2max session: 4-5 x 4 min @ Zone 5 (Attia/Galpin zone-5 dose).",
                f"Zone 2 toward Attia's ~3 hr/wk: Tue {wave['tue']}, Sat {wave['sat']} min + Fri intervals.",
                "Every 4th block is a deload (lower volume) — age-appropriate 3:1 loading.",
                "If a top set isn't clean, repeat the week instead of progressing.",
                "Wed & Sun rest are non-negotiable.",
            ],
        },
        "block": {
            "name": f"Block {idx+1}/{len(MACROCYCLE)} — {spec['name']}",
            "start": start_iso,
            "next_replan": (start + timedelta(weeks=4)).isoformat(),
            "emphasis": emphasis,
            "bodyweight_substitutions": all_subs,
            "week_params": week_params,
            "sessions": sessions,
        },
    }


# --------------------------------------------------------------------------- #
# macrocycle state + writing program.json
# --------------------------------------------------------------------------- #
def _macro_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"current_index": -1, "history": []}


def _save_macro_state(s):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def _next_monday(d=None):
    d = d or date.today()
    return (d + timedelta(days=(7 - d.weekday()) % 7 or 7)).isoformat()


def write_block(idx, start_iso, bodyweight=False, seed=None):
    """Generate block `idx`, archive the old program.json, write the new one, advance state."""
    block = build_block(idx, start_iso, bodyweight=bodyweight, seed=seed)
    if PROGRAM_JSON.exists():
        arch = HERE / "data" / "program_archive"
        arch.mkdir(parents=True, exist_ok=True)
        prev = json.loads(PROGRAM_JSON.read_text())
        pname = re.sub(r"[^a-z0-9]+", "-", prev.get("block", {}).get("name", "prev").lower()).strip("-")
        (arch / f"{start_iso}_{pname}.json").write_text(json.dumps(prev, indent=2))
    if bodyweight:                                   # bake bodyweight details into detail
        for wd, s in block["block"]["sessions"].items():
            if s.get("detail_bw"):
                s["detail"] = s["detail_bw"]
    PROGRAM_JSON.write_text(json.dumps(block, indent=2, ensure_ascii=False))
    st = _macro_state()
    st["current_index"] = idx
    st.setdefault("history", []).append({"index": idx, "start": start_iso,
                                         "name": block["block"]["name"], "bodyweight": bodyweight})
    _save_macro_state(st)
    return block


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("macrocycle")
    p = sub.add_parser("preview"); p.add_argument("n", type=int)
    n = sub.add_parser("next"); n.add_argument("--start"); n.add_argument("--bodyweight", action="store_true")
    r = sub.add_parser("regenerate"); r.add_argument("--bodyweight", action="store_true")
    bn = sub.add_parser("ban"); bn.add_argument("name", nargs="+")
    ub = sub.add_parser("unban"); ub.add_argument("name", nargs="+")
    sub.add_parser("bans")
    a = ap.parse_args()

    if a.cmd == "ban":
        cur = load_banned(); cur.append(" ".join(a.name)); save_banned(cur)
        print("banned. now:", load_banned()); return
    if a.cmd == "unban":
        tgt = " ".join(a.name).lower()
        save_banned([b for b in load_banned() if b.lower() != tgt])
        print("unbanned. now:", load_banned()); return
    if a.cmd == "bans":
        print("banned exercises:", load_banned() or "(none)"); return

    if a.cmd == "macrocycle":
        st = _macro_state()
        for i, b in enumerate(MACROCYCLE):
            cur = " <= current" if i == st.get("current_index") else ""
            print(f"  {i+1:2}. {b['name']:22} [{b['emphasis']:13}] template={b['template']}{cur}")
    elif a.cmd == "preview":
        b = build_block(a.n, _next_monday())
        print(json.dumps(b["block"]["sessions"], indent=2, ensure_ascii=False))
    elif a.cmd == "next":
        st = _macro_state()
        idx = st.get("current_index", -1) + 1
        start = a.start or _next_monday()
        b = write_block(idx, start, bodyweight=a.bodyweight)
        print(f"wrote block {idx+1}: {b['block']['name']} starting {start} "
              f"(bodyweight={a.bodyweight}) -> program.json")
    elif a.cmd == "regenerate":
        st = _macro_state()
        idx = max(0, st.get("current_index", 0))
        start = json.loads(PROGRAM_JSON.read_text())["block"]["start"] if PROGRAM_JSON.exists() else _next_monday()
        b = write_block(idx, start, bodyweight=a.bodyweight, seed=random.randint(1, 99999))
        print(f"re-rolled block {idx+1}: {b['block']['name']} -> program.json")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
