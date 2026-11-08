import unittest
from datetime import date

import server


def rows(sig, n_yes, n_no, yes_hrv, no_hrv, other=None):
    """n_yes evenings with the signal and n_no without; HRV the next night alternates ±2
    around the given means so the groups have some spread."""
    out = []
    for i in range(n_yes):
        out.append(({sig: True, **(other or {})}, {"hrv": yes_hrv + (2 if i % 2 else -2)}))
    for i in range(n_no):
        out.append(({sig: False, **(other or {})}, {"hrv": no_hrv + (2 if i % 2 else -2)}))
    return out


class CandidateTests(unittest.TestCase):
    def test_clear_effect_with_enough_nights_is_kept_with_an_action(self):
        c = server.insight_candidates(rows("alcohol", 10, 20, 32, 41))
        self.assertEqual(len(c), 1)
        top = c[0]
        self.assertEqual((top["key"], top["direction"], top["n_yes"], top["n_no"]), ("alcohol:hrv", "worse", 10, 20))
        self.assertLess(top["effect"], -1)
        self.assertGreater(top["confidence"], 0.99)
        self.assertIn("After drinks, your HRV is 9 ms lower", top["text"])
        self.assertTrue(top["action"])

    def test_fewer_than_8_nights_a_side_is_suppressed(self):
        self.assertEqual(server.insight_candidates(rows("alcohol", 7, 20, 32, 41)), [])

    def test_trivial_effect_is_suppressed(self):
        self.assertEqual(server.insight_candidates(rows("alcohol", 12, 12, 40.2, 40)), [])

    def test_low_confidence_is_suppressed(self):
        # a moderate difference that noise could produce: wide spread, few nights
        noisy = [({"alcohol": True}, {"hrv": v}) for v in (30, 50, 32, 48, 31, 49, 33, 47)] + \
                [({"alcohol": False}, {"hrv": v}) for v in (35, 55, 36, 54, 34, 56, 37, 53)]
        self.assertEqual(server.insight_candidates(noisy), [])

    def test_no_action_means_no_insight(self):
        # drinks "improving" HRV has no sensible action, so it is never shown
        self.assertEqual(server.insight_candidates(rows("alcohol", 10, 20, 45, 38)), [])

    def test_ranked_by_effect_size_times_confidence(self):
        r = rows("alcohol", 10, 10, 30, 42) + rows("late_meal", 10, 10, 38, 42)
        for s, _ in r:                                   # keep each signal unknown on the other's rows
            s.setdefault("alcohol", None)
            s.setdefault("late_meal", None)
        keys = [c["key"] for c in server.insight_candidates(r)]
        self.assertEqual(keys[:2], ["alcohol:hrv", "late_meal:hrv"])


class HeadlineTests(unittest.TestCase):
    def cands(self, e1=-1.5, e2=-0.8):
        return [{"key": "alcohol:hrv", "effect": e1, "score": 1.4}, {"key": "late_meal:hrv", "effect": e2, "score": 0.7}]

    def test_one_headline_per_week_and_no_repeats(self):
        st = {"surfaced": {}, "weekly": {}}
        h, new = server.pick_headline(self.cands(), st, "2026-W46")
        self.assertEqual((h["key"], new), ("alcohol:hrv", True))
        h, new = server.pick_headline(self.cands(), st, "2026-W46")       # same week: same headline
        self.assertEqual((h["key"], new), ("alcohol:hrv", False))
        h, _ = server.pick_headline(self.cands(), st, "2026-W47")         # next week: the next new one
        self.assertEqual(h["key"], "late_meal:hrv")
        h, _ = server.pick_headline(self.cands(), st, "2026-W48")         # nothing new, nothing changed
        self.assertIsNone(h)

    def test_known_insight_returns_only_when_materially_changed(self):
        st = {"surfaced": {"alcohol:hrv": {"week": "2026-W40", "effect": -1.5},
                           "late_meal:hrv": {"week": "2026-W41", "effect": -0.8}}, "weekly": {}}
        self.assertIsNone(server.pick_headline(self.cands(-1.6, -0.85), st, "2026-W46")[0])
        h, _ = server.pick_headline(self.cands(-2.2, -0.85), st, "2026-W47")   # 47% stronger
        self.assertEqual(h["key"], "alcohol:hrv")
        self.assertTrue(server._materially_changed({"effect": -0.5}, {"effect": 0.4}))   # sign flip


class SignalTests(unittest.TestCase):
    def test_food_collapses_into_behaviour_signals(self):
        ctx = {"behav": {"2026-11-10": {"drink": 0, "dry_confirmed": True}},
               "food": {"2026-11-10": {"items": [{"name": "pasta", "at": "22:00", "wahls_ok": False, "cups": 1, "group": "greens"}],
                                       "totals": {"cal": 3200, "carbs": 220}}},
               "metrics": {"2026-11-11": {"bedtime_hour": {"value": 12.5}}},              # in bed 00:30
               "alcohol": server.ALCOHOL_DEFAULTS, "cal_median": 2300, "bed_median": 10.75,
               "sessions": {"2026-11-10": [{"kind": "zone2", "zone2": 45}]}, "has_workouts": True}
        s = server.behaviour_signals("2026-11-10", ctx)
        self.assertEqual(s, {"alcohol": False, "late_meal": True, "heavy_meal": True, "high_carb": True,
                             "off_plan": True, "low_veg": True, "zone2_day": True, "hiit_day": False,
                             "strength_day": False, "late_bed": True})
        blank = server.behaviour_signals("2026-11-09", {**ctx, "has_workouts": False})
        self.assertTrue(all(v is None for k, v in blank.items() if k != "late_bed"))


if __name__ == "__main__":
    unittest.main()
