"""ZOE-style Processed Food Risk scale, reconstructed from ZOE's public descriptions.

ZOE has not published its algorithm. What it has said publicly (2025):
  * Three drivers: energy intake rate (EIR, kcal/min — a surrogate for the food matrix,
    threshold 72 kcal/min), hyperpalatability (fat/sugar/salt/carb combinations), and
    non-culinary additives graded individually by risk (e.g. pectin, inulin, ascorbic acid
    low; carrageenan, mono-/diglycerides of fatty acids high).
  * Output: Unprocessed / No risk / Low / Medium / High.
  * Examples: unsalted butter, dark chocolate, Weetabix, seeded sourdough → no/low;
    milk chocolate, soft sliced white bread, margarine, chicken nuggets → high.

Everything below the published level (additive tiers beyond ZOE's examples, how the
factors combine) is my reconstruction, tuned so ZOE's published examples land where ZOE
puts them. Treat the output as an approximation of ZOE's scale, not ZOE's own score.

Hyperpalatability uses Fazzino et al. 2019 (Obesity) — the definition ZOE cites:
  fat+sodium:  >25% kcal from fat and >=0.30% sodium by weight
  fat+sugar:   >20% kcal from fat and >20% kcal from simple sugars
  carb+sodium: >40% kcal from carbohydrate and >=0.20% sodium by weight
"""

import re

EIR_FAST = 72  # kcal/min — ZOE's stated threshold

LEVELS = ["unprocessed", "none", "low", "medium", "high"]
LABELS = {"unprocessed": "Unprocessed", "none": "No risk", "low": "Low risk",
          "medium": "Medium risk", "high": "High risk"}

# Additive tiers. Keys are E-numbers (or a pattern); names map onto them below.
# high: emulsifiers / sweeteners / colours / preservatives with the strongest human or
#       animal evidence of gut-barrier, microbiome, metabolic or carcinogenic effects,
#       or that EFSA has withdrawn or flagged.
# medium: plausible concern, weaker evidence or dose-dependent.
# low: everything else that is approved and broadly benign (incl. ZOE's low examples).
_HIGH = {
    "E407", "E407A",                 # carrageenan (ZOE example)
    "E471", "E472E", "E481", "E482",  # mono-/diglycerides (ZOE example), DATEM, lactylates
    "E433", "E466", "E468",          # polysorbate 80, CMC, croscarmellose — emulsifier/gut studies
    "E171",                          # titanium dioxide (EFSA: no longer safe)
    "E249", "E250", "E251", "E252",  # nitrites/nitrates (processed meat)
    "E950", "E951", "E955", "E962",  # acesulfame-K, aspartame, sucralose, aspartame-acesulfame
    "E102", "E104", "E110", "E122", "E124", "E129",  # 'Southampton six' azo colours
    "E320", "E321",                  # BHA, BHT
    "E150C", "E150D",                # ammonia/sulphite caramels (4-MEI)
}
_MEDIUM = {
    "E211", "E212", "E213",          # benzoates
    "E220", "E221", "E222", "E223", "E224", "E226", "E228",  # sulphites
    "E338", "E339", "E340", "E341", "E450", "E451", "E452",  # added phosphates
    "E621", "E627", "E631", "E635",  # MSG & ribonucleotides (hyperpalatability)
    "E476", "E442", "E475", "E477", "E491", "E492", "E432", "E435", "E436",  # PGPR, sorbitan esters
    "E412", "E415", "E414", "E461", "E464", "E460",  # gums/celluloses — emulsifier-class gut data
    "E420", "E421", "E965", "E966", "E967", "E968",  # polyols (gut effects at dose)
    "E954", "E952", "E961",          # saccharin, cyclamate, advantame
    "E1442", "E1422", "E1414", "E1420",  # modified starches
    "E319", "E310",                  # TBHQ, propyl gallate
}
# Anything else with an E-number is treated as low (pectin E440, inulin, ascorbic acid E300,
# citric acid E330, tocopherols E306-9, lecithin E322, calcium carbonate E170, stevia E960...).

_NAMES = {
    "carrageenan": "E407", "mono and diglycerides": "E471", "mono- and diglycerides": "E471",
    "mono-and diglycerides": "E471", "monoglycerides": "E471", "diglycerides": "E471",
    "datem": "E472E", "sodium stearoyl lactylate": "E481", "polysorbate 80": "E433",
    "polysorbate": "E433", "carboxymethylcellulose": "E466", "cellulose gum": "E466",
    "titanium dioxide": "E171", "sodium nitrite": "E250", "potassium nitrate": "E252",
    "sodium nitrate": "E251", "nitrite": "E250", "acesulfame": "E950", "aspartame": "E951",
    "sucralose": "E955", "tartrazine": "E102", "sunset yellow": "E110", "allura red": "E129",
    "carmoisine": "E122", "ponceau": "E124", "quinoline yellow": "E104", "bha": "E320",
    "bht": "E321", "caramel colour iii": "E150C", "caramel colour iv": "E150D",
    "sulphite ammonia caramel": "E150D", "sodium benzoate": "E211", "potassium benzoate": "E212",
    "sulphite": "E220", "sulfite": "E220", "sulphur dioxide": "E220", "metabisulphite": "E223",
    "phosphate": "E450", "diphosphate": "E450", "polyphosphate": "E452",
    "monosodium glutamate": "E621", "msg": "E621", "flavour enhancer": "E621",
    "pgpr": "E476", "polyglycerol polyricinoleate": "E476", "xanthan": "E415", "guar": "E412",
    "methylcellulose": "E461", "sorbitol": "E420", "maltitol": "E965", "xylitol": "E967",
    "erythritol": "E968", "saccharin": "E954", "cyclamate": "E952", "modified starch": "E1422",
    "modified maize starch": "E1422", "tbhq": "E319",
    "pectin": "E440", "inulin": "INULIN", "ascorbic acid": "E300", "citric acid": "E330",
    "lecithin": "E322", "sunflower lecithin": "E322", "soy lecithin": "E322",
    "tocopherol": "E306", "stevia": "E960",
}


def additive_tier(a: str) -> str:
    """'high' | 'medium' | 'low' for one additive given as an E-number or a name."""
    s = (a or "").strip().lower()
    m = re.search(r"\be\s?-?\s?(\d{3,4}[a-z]?)", s)
    code = ("E" + m.group(1).upper()) if m else None
    if not code:
        for name in sorted(_NAMES, key=len, reverse=True):
            if name in s:
                code = _NAMES[name]
                break
    if not code:
        return "low"
    if code in _HIGH:
        return "high"
    if code in _MEDIUM:
        return "medium"
    return "low"


def hyperpalatable(item: dict):
    """Fazzino 2019 cluster that fires, or None. Needs cal, fat, carbs; sugar/sodium/grams
    when present (missing values simply can't trigger their cluster)."""
    cal = item.get("cal") or 0
    if cal <= 0:
        return None
    fat_pct = 9 * (item.get("fat") or 0) / cal
    carb_pct = 4 * (item.get("carbs") or 0) / cal
    sugar = item.get("sugar")
    sugar_pct = 4 * sugar / cal if isinstance(sugar, (int, float)) else None
    grams, na = item.get("grams"), item.get("sodium_mg")
    na_pct = (na / 1000) / grams if isinstance(na, (int, float)) and grams else None
    if fat_pct > 0.25 and na_pct is not None and na_pct >= 0.0030:
        return "fat+salt"
    if fat_pct > 0.20 and sugar_pct is not None and sugar_pct > 0.20:
        return "fat+sugar"
    if carb_pct > 0.40 and na_pct is not None and na_pct >= 0.0020:
        return "carb+salt"
    return None


def classify(item: dict) -> dict:
    """Score one parsed food item. Expects the parser's fields: nova (1-4), eir (kcal/min),
    additives [str], plus macros (and ideally grams, sugar, sodium_mg).
    Returns {"level", "label", "why": [str], "hp", "fast", "additives": {tier: [str]}}."""
    nova = item.get("nova")
    adds = [a for a in (item.get("additives") or []) if str(a).strip()]
    tiers = {"high": [], "medium": [], "low": []}
    for a in adds:
        tiers[additive_tier(str(a))].append(str(a))
    hp = hyperpalatable(item)
    eir = item.get("eir")
    fast = isinstance(eir, (int, float)) and eir >= EIR_FAST

    if nova == 1 and not adds:
        return {"level": "unprocessed", "label": LABELS["unprocessed"], "why": [],
                "hp": hp, "fast": fast, "additives": tiers}

    why = []
    score = 0
    if hp:
        score += 1
        why.append(f"hyperpalatable ({hp})")
    if fast:
        score += 1
        why.append(f"eaten fast (~{round(eir)} kcal/min)")
    if tiers["high"]:
        score += 2 if len(tiers["high"]) == 1 else 3
        why.append("high-risk additives: " + ", ".join(tiers["high"]))
    elif tiers["medium"]:
        score += 1
        why.append("additives of concern: " + ", ".join(tiers["medium"]))

    if score == 0:
        level = "none" if (nova or 4) <= 3 else "low"
    elif score == 1:
        level = "low"
    elif score == 2:
        level = "medium"
    else:
        level = "high"
    return {"level": level, "label": LABELS[level], "why": why, "hp": hp, "fast": fast,
            "additives": tiers}


def day_summary(items: list) -> dict:
    """Share of the day's calories in each level (only items that carry a score)."""
    kcal = {k: 0.0 for k in LEVELS}
    scored = 0
    for i in items or []:
        p = i.get("processing") or {}
        lvl = p.get("level")
        if lvl in kcal:
            kcal[lvl] += float(i.get("cal") or 0)
            scored += 1
    total = sum(kcal.values())
    pct = {k: (round(100 * v / total) if total else 0) for k, v in kcal.items()}
    return {"kcal": {k: round(v) for k, v in kcal.items()}, "pct": pct,
            "scored_items": scored, "risky_pct": pct["medium"] + pct["high"]}
