import unittest
from datetime import date

import server

TODAY = date(2026, 11, 12)                      # Thursday: Mon 9 – Sun 15 Nov
ALC = dict(server.ALCOHOL_DEFAULTS, reset_start=None)


def week(z2=0, strength=0, hiit=0, sessions=(), history=None):
    return {"zone2_minutes": z2, "strength": strength, "hiit": hiit, "sessions": list(sessions),
            "targets": {"zone2_minutes": 180, "strength": 3, "hiit": 1}, "zone2_hr": [120, 135],
            "history": history or [{"zone2_minutes": m} for m in (180, 90, 0, 270, 180, 0, 0, 0)]}


def ctx(active=("zone2", "eat_clean", "no_alcohol"), food=None, behav=None, metrics=None, marks=None, custom=()):
    return {"habits": {"active": list(active), "custom": list(custom), "marks": marks or {}, "actions_done": {}},
            "food": food or {}, "behav": behav or {}, "metrics": metrics or {}, "alcohol": ALC}


CLEAN = [{"name": "lentil and veg stew", "cal": 500, "fibre": 20, "nova": 1, "additives": [],
          "plants": [{"name": p} for p in ("lentil", "carrot", "onion", "celery", "kale", "tomato",
                                           "capsicum", "brown rice", "chickpea", "spinach")]},
         {"name": "kimchi", "cal": 30, "fibre": 2, "fermented": 2, "nova": 3, "plants": [{"name": "cabbage"}]},
         {"name": "coconut kefir", "cal": 60, "fermented": 1, "nova": 3, "fibre": 0}]
JUNK = [{"name": "GF bread", "cal": 200, "fat": 2, "carbs": 35, "sugar": 3, "grams": 80, "sodium_mg": 320,
         "fibre": 2, "nova": 4, "eir": 90, "additives": ["E471"], "plants": []}]


class Zone2PaceTests(unittest.TestCase):
    def test_pace_numbers_and_4_week_adherence(self):
        z = server.zone2_progress(week(z2=72), TODAY)
        self.assertEqual((z["minutes"], z["target"], z["days_left"], z["remaining"], z["per_day"]), (72, 180, 4, 108, 27))
        self.assertEqual(z["adherence_4w"], round(100 * (1 + .5 + 0 + 1) / 4))   # capped at 100% a week
        self.assertEqual(server.zone2_progress(week(z2=200), TODAY)["per_day"], 0)

    def test_weekly_gap_is_judged_against_even_pace(self):
        self.assertEqual(server.habit_gap(done=72, target=180, weekday=3), round((180 * 3.5 / 7 - 72) / 180, 3))
        self.assertEqual(server.habit_gap(done=120, target=180, weekday=3), 0.0)         # ahead of pace
        self.assertEqual(server.habit_gap(done=0, target=0, weekday=3), 0.0)


class AdherenceTests(unittest.TestCase):
    def test_eat_clean_from_food_log_or_tap(self):
        c = ctx(food={"2026-11-10": {"items": CLEAN}, "2026-11-11": {"items": JUNK}, "2026-11-12": {"items": JUNK}},
                marks={"2026-11-09": {"eat_clean": True}})
        st = lambda d: server.habit_day_status("eat_clean", date.fromisoformat(d), TODAY, c)
        self.assertEqual([st("2026-11-09"), st("2026-11-10"), st("2026-11-11"), st("2026-11-12")],
                         ["done", "done", "missed", "pending"])           # today can still be rescued
        self.assertEqual(server.habit_day_status("eat_clean", date(2026, 11, 8), TODAY, c), "unknown")

    def test_alcohol_follows_the_rules(self):
        wine = lambda t, std=1.5: {"id": t, "time": t, "kind": "wine", "std": std}
        c = ctx(behav={"2026-11-09": {"drink": 0, "dry_confirmed": True},
                       "2026-11-10": {"drinks": [wine("19:00")], "drink": 1},             # Tuesday: rule broken
                       "2026-11-07": {"drinks": [wine("19:00")], "drink": 1}})            # Saturday, within rules
        st = lambda d: server.habit_day_status("no_alcohol", date.fromisoformat(d), TODAY, c)
        self.assertEqual([st("2026-11-09"), st("2026-11-10"), st("2026-11-07"), st("2026-11-11")],
                         ["done", "missed", "done", "unknown"])

    def test_kitchen_and_custom(self):
        c = ctx(food={"2026-11-10": {"items": [{"name": "x", "at": "21:30"}]}},
                metrics={"2026-11-11": {"bedtime_hour": {"value": 10.5}}},           # bed 22:30
                marks={"2026-11-11": {"c_1": False}}, custom=[{"id": "c_1", "name": "Stretch"}])
        self.assertEqual(server.habit_day_status("kitchen_3h", date(2026, 11, 10), TODAY, c), "missed")
        self.assertEqual(server.habit_day_status("c_1", date(2026, 11, 11), TODAY, c), "missed")
        self.assertEqual(server.habit_day_status("c_1", TODAY, TODAY, c), "pending")

    def test_daily_gap_counts_known_days_only(self):
        self.assertEqual(server.habit_gap(statuses=["done", "missed", "unknown", "missed"]), round(2 / 3, 3))
        self.assertEqual(server.habit_gap(statuses=["unknown", "pending"]), 0.0)


class TodayPlanTests(unittest.TestCase):
    def test_only_the_worst_slipping_habit_gets_attention(self):
        c = ctx(food={"2026-11-09": {"items": CLEAN}, "2026-11-10": {"items": CLEAN}, "2026-11-11": {"items": JUNK}},
                behav={d: {"drink": 0, "dry_confirmed": True} for d in ("2026-11-09", "2026-11-10", "2026-11-11")},
                metrics={"2026-11-12": {"readiness_score": {"value": 82}}})
        p = server.today_plan(TODAY, c, week(z2=30))
        z2, clean = p["habits"][0], p["habits"][1]
        self.assertTrue(z2["slipping"])                    # 30/180 by Thursday is the biggest gap
        self.assertFalse(clean["slipping"])                # 1 of 3 missed is behind but not the focus
        self.assertEqual(sum(h["slipping"] for h in p["habits"]), 1)
        self.assertTrue(p["headline"]["lead"].startswith("Good recovery"))
        self.assertIn("Zone 2", p["headline"]["focus"])
        self.assertIn("Also behind: Eat clean", p["headline"]["detail"])
        self.assertEqual(p["actions"][0]["key"], "recover:zone2")
        self.assertIn("38 min/day", p["actions"][0]["why"])            # 150 left over 4 days
        self.assertEqual([q["id"] for q in p["checkin"]], ["drinks", "eat_clean"])   # nothing logged today

    def test_zone2_ask_is_capped_when_target_is_out_of_reach(self):
        sunday = date(2026, 11, 15)
        p = server.today_plan(sunday, ctx(active=("zone2",)), week(z2=30))
        self.assertEqual(p["actions"][0]["text"], "90 min easy Zone 2 today")
        self.assertIn("out of reach", p["actions"][0]["why"])

    def test_sunday_review_nudge_goes_once_opened(self):
        sunday = date(2026, 11, 15)
        c = ctx(active=("no_alcohol",))
        self.assertIn("review", [a["key"] for a in server.today_plan(sunday, c, week())["actions"]])
        c["habits"]["actions_done"] = {"2026-11-15": ["review"]}
        self.assertNotIn("review", [a["key"] for a in server.today_plan(sunday, c, week())["actions"]])

    def test_on_track_says_so_and_asks_little(self):
        c = ctx(food={d: {"items": CLEAN} for d in ("2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12")},
                behav={d: {"drink": 0, "dry_confirmed": True} for d in ("2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12")})
        p = server.today_plan(TODAY, c, week(z2=120))
        self.assertFalse(any(h["slipping"] for h in p["habits"]))
        self.assertEqual(p["actions"], [])
        self.assertEqual(p["checkin"], [])
        self.assertIn("on track", p["headline"]["focus"])


if __name__ == "__main__":
    unittest.main()
