"""ZOE's food rules, scored from the food log (replaces the Wahls tracker, 1.21.0).

The rules (Tim Spector / ZOE's published advice):
  1. 30 different plants a week. Veg, fruit, wholegrains, legumes, nuts, seeds all count one
     "plant point" each; herbs and spices count a quarter. Each plant counts once per week
     (weeks run Monday-Sunday).
  2. Fermented foods every day, several small portions (target 3).
  3. Fibre: 30 g a day.
  4. Keep ultra-processed food to a minimum: the processed-food risk scale
     (processing_risk.py); the score is the calorie-weighted processing level.
  5. Give your gut a rest: a 12-hour-or-less eating window.

The day score (0-100) is the mean of five parts, each 0-1:
  plants   — this week's plant points so far vs the pace for 30 by Sunday
  ferments — fermented portions today / 3
  fibre    — fibre today / 30 g
  processing — calorie-weighted processing score / 100
  window   — 1 if the eating window is 12 h or less (or not yet known), else scaled down
A part with no data (e.g. no item carries fibre yet) is left out of the mean.
"""

import re

import processing_risk

PLANTS_WEEK = 30
FERMENTS_DAY = 3
FIBRE_DAY = 30
WINDOW_H = 12
HERB_POINT = 0.25

PROC_POINTS = {"unprocessed": 100, "none": 100, "low": 75, "medium": 35, "high": 0}


def _norm(name: str) -> str:
    s = re.sub(r"[^a-z ]", "", str(name or "").lower()).strip()
    s = re.sub(r"\s+", " ", s)
    if len(s) > 3 and s.endswith("ies"):
        s = s[:-3] + "y"
    elif len(s) > 3 and s.endswith("oes"):
        s = s[:-2]
    elif len(s) > 3 and s.endswith("s") and not s.endswith("ss"):
        s = s[:-1]
    return s


def item_plants(item: dict) -> dict:
    """{normalised name: points} for one item's plants (herbs/spices 0.25)."""
    out = {}
    for p in item.get("plants") or []:
        if isinstance(p, str):
            name, kind = p, "plant"
        elif isinstance(p, dict):
            name, kind = p.get("name"), p.get("kind") or "plant"
        else:
            continue
        n = _norm(name)
        if n:
            pts = HERB_POINT if kind in ("herb", "spice", "herb_spice") else 1.0
            out[n] = max(out.get(n, 0), pts)
    return out


def plant_points(items: list) -> dict:
    """Unique plants across `items` → {"points", "plants": sorted names}."""
    seen = {}
    for i in items or []:
        for n, pts in item_plants(i).items():
            seen[n] = max(seen.get(n, 0), pts)
    return {"points": round(sum(seen.values()), 2), "plants": sorted(seen)}


def _level(i: dict):
    lvl = (i.get("processing") or {}).get("level")
    if lvl is None and i.get("nova") is not None:
        lvl = processing_risk.classify(i)["level"]
    return lvl


def processing_score(items: list):
    """Calorie-weighted 0-100 from each item's processing level; None if nothing is scored."""
    num = den = 0.0
    for i in items or []:
        lvl = _level(i)
        if lvl in PROC_POINTS:
            w = max(float(i.get("cal") or 0), 1.0)
            num += PROC_POINTS[lvl] * w
            den += w
    return round(num / den) if den else None


def day_eval(items: list, week_items: list, day_index: int, timing: dict = None) -> dict:
    """Score one day against the ZOE rules.
    items: that day's food; week_items: Monday..that day (inclusive);
    day_index: 0 = Monday … 6 = Sunday; timing: meal_timing() for the day (optional)."""
    items = items or []
    have = lambda k: any(isinstance(i.get(k), (int, float)) or i.get(k) for i in items)
    week = plant_points(week_items)
    today = plant_points(items)
    pace = PLANTS_WEEK * (day_index + 1) / 7
    ferments = round(sum(float(i.get("fermented") or 0) for i in items), 1)
    fibre = round(sum(float(i.get("fibre") or 0) for i in items), 1)
    proc = processing_score(items)
    win = (timing or {}).get("eating_window_h")
    risky = [i.get("name") for i in items if _level(i) in ("medium", "high")]

    parts = {}
    if have("plants") or week["points"]:
        parts["plants"] = min(1.0, week["points"] / pace) if pace else 1.0
    if have("fermented") or have("plants"):          # new-format log: 0 ferments is a real 0
        parts["ferments"] = min(1.0, ferments / FERMENTS_DAY)
    if have("fibre"):
        parts["fibre"] = min(1.0, fibre / FIBRE_DAY)
    if proc is not None:
        parts["processing"] = proc / 100
    if isinstance(win, (int, float)) and items:
        parts["window"] = 1.0 if win <= WINDOW_H else max(0.0, 1 - (win - WINDOW_H) / 4)
    score = round(100 * sum(parts.values()) / len(parts)) if items and parts else None

    todo = []
    if items:
        if week["points"] < PLANTS_WEEK:
            todo.append(f"{_fmt(PLANTS_WEEK - week['points'])} more plant points this week")
        if ferments < FERMENTS_DAY:
            todo.append(f"{_fmt(FERMENTS_DAY - ferments)} fermented portion{'s' if FERMENTS_DAY - ferments > 1 else ''}")
        if fibre < FIBRE_DAY:
            todo.append(f"{_fmt(FIBRE_DAY - fibre)} g fibre")
        if risky:
            todo.append("swap " + ", ".join(risky[:2]) + " for a less processed option")
    return {"score": score, "parts": {k: round(v, 2) for k, v in parts.items()},
            "plants_week": week["points"], "plants_week_list": week["plants"],
            "plants_today": today["plants"], "plants_target": PLANTS_WEEK, "plants_pace": round(pace, 1),
            "ferments": ferments, "ferments_target": FERMENTS_DAY,
            "fibre": fibre, "fibre_target": FIBRE_DAY,
            "processing_score": proc, "risky_items": risky,
            "eating_window_h": win, "window_target": WINDOW_H, "todo": todo}


def _fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")
