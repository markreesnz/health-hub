import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

import server

START = date(2026, 11, 2)
END = START + timedelta(days=14)            # the day after the last experiment day


def metrics(base_hrv, exp_hrv, weight_base=None, weight_exp=None):
    """28 baseline nights before START and 14 nights during; HRV alternates ±2 around the mean."""
    m = {}
    for i in range(28):
        d = START - timedelta(days=i)
        m[d.isoformat()] = {"hrv": {"value": base_hrv + (2 if i % 2 else -2)}}
        if weight_base is not None:
            m[d.isoformat()]["weight"] = {"value": weight_base(i)}
    for i in range(14):
        d = START + timedelta(days=i + 1)                      # the night after experiment day i
        m.setdefault(d.isoformat(), {})["hrv"] = {"value": exp_hrv + (2 if i % 2 else -2)}
    if weight_exp is not None:
        for i in range(14):
            m.setdefault((START + timedelta(days=i)).isoformat(), {})["weight"] = {"value": weight_exp(i)}
    return m


def exp(marks):
    return {"id": "e1", "name": "Magnesium at night", "preset": None, "detector": "manual",
            "start": START.isoformat(), "days": 14, "marks": marks}


def ctx(m):
    return {"metrics": m, "behav": {}, "food": {}, "alcohol": server.ALCOHOL_DEFAULTS, "sessions": {}}


ALL = {(START + timedelta(days=i)).isoformat(): True for i in range(14)}


class BeforeAfterTests(unittest.TestCase):
    def test_clear_improvement_is_called_helped(self):
        r = server.experiment_result(exp(ALL), ctx(metrics(40, 47)), END)
        hrv = r["metrics"][0]
        self.assertEqual((r["adherence"], r["days_done"]), (1.0, 14))
        self.assertEqual((hrv["before"], hrv["during"], hrv["diff"], hrv["verdict"]), (40.0, 47.0, 7.0, "improved"))
        self.assertEqual(r["outcome"], "helped")
        self.assertTrue(r["verdict"].startswith("Probably helped: HRV +7 ms"))

    def test_same_numbers_mean_no_clear_effect(self):
        r = server.experiment_result(exp(ALL), ctx(metrics(40, 40.3)), END)
        self.assertEqual(r["metrics"][0]["verdict"], "no clear change")
        self.assertEqual(r["outcome"], "no_effect")

    def test_low_adherence_is_inconclusive_whatever_the_numbers(self):
        some = dict(list(ALL.items())[:5])
        r = server.experiment_result(exp(some), ctx(metrics(40, 47)), END)
        self.assertAlmostEqual(r["adherence"], 5 / 14, places=2)
        self.assertEqual(r["outcome"], "inconclusive")
        self.assertIn("Not enough adherence", r["verdict"])

    def test_only_nights_after_adherent_days_count(self):
        marks = {k: (i % 2 == 0) for i, k in enumerate(ALL)}       # every other day
        m = metrics(40, 40)
        for i in range(14):
            night = (START + timedelta(days=i + 1)).isoformat()
            if i % 2:                                              # missed days' nights are terrible
                m[night]["hrv"] = {"value": 10}
            else:                                                  # adherent nights match the baseline
                m[night]["hrv"] = {"value": 40 + (2 if (i // 2) % 2 else -2)}
        r = server.experiment_result(exp(marks), ctx(m), END)
        self.assertEqual(r["metrics"][0]["n_exp"], 7)
        self.assertEqual(r["metrics"][0]["verdict"], "no clear change")

    def test_weight_trend_compares_slopes(self):
        m = metrics(40, 40, weight_base=lambda i: 101.0, weight_exp=lambda i: 101.0 - i * 0.1)   # -0.7 kg/week
        r = server.experiment_result(exp(ALL), ctx(m), END)
        w = r["metrics"][-1]
        self.assertEqual((w["before"], w["during"], w["verdict"]), (0.0, -0.7, "improved"))
        self.assertIn("weight trend -0.70 kg/week", r["verdict"])

    def test_too_few_nights(self):
        r = server.experiment_result(exp(ALL), ctx({}), END)
        self.assertEqual(r["outcome"], "inconclusive")
        self.assertIn("Not enough nights", r["verdict"])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = lambda n: os.path.join(self.tmp.name, n)
        self.patches = [mock.patch.object(server, "EXPERIMENTS_FILE", p("experiments.json")),
                        mock.patch.object(server, "HABITS_FILE", p("habits.json")),
                        mock.patch.object(server, "WEEKLY_PLANS_FILE", p("weekly_plans.json")),
                        mock.patch.object(server, "_exp_ctx", lambda today: ctx(metrics(40, 47)))]
        for x in self.patches:
            x.start()

    def tearDown(self):
        for x in self.patches:
            x.stop()
        self.tmp.cleanup()

    def test_one_at_a_time_then_finish_and_promote(self):
        v = server.start_experiment({"preset": "kitchen_3h", "start": START.isoformat()}, START)
        self.assertEqual((v["active"]["name"], v["active"]["days"]), ("Kitchen closes 3 h before bed", 14))
        with self.assertRaises(ValueError):
            server.start_experiment({"name": "Cold showers"}, START)
        v = server.experiments_view(END, ctx(metrics(40, 47)))      # days are up: finished into history
        self.assertIsNone(v["active"])
        self.assertEqual(len(v["history"]), 1)
        server.promote_experiment(v["history"][0]["id"])
        self.assertIn("kitchen_3h", server.load_habits()["active"])
        self.assertTrue(server.load_experiments()["history"][0]["promoted"])

    def test_manual_experiment_is_marked_by_tap(self):
        server.start_experiment({"name": "Magnesium at night"}, START)
        with mock.patch.object(server, "_drinking_day", lambda: START):
            v = server.mark_experiment(True, START.isoformat())
        self.assertEqual(v["active"]["marks"], {START.isoformat(): True})

    def test_suggestions_learn_from_history(self):
        e = {"active": None, "history": [
            {"preset": "dry", "ended": "2026-10-01", "outcome": "helped"},
            {"preset": "bed_2230", "ended": "2026-11-01", "outcome": "no_effect"},
            {"preset": "veg_9", "ended": "2026-11-01", "outcome": "inconclusive"}]}
        s = server.experiment_suggestions(e, date(2026, 11, 20), insight_signal="late_meal")
        ids = [x["preset"] for x in s]
        self.assertNotIn("dry", ids)               # helped: promote it rather than re-test
        self.assertNotIn("bed_2230", ids)          # no effect recently
        self.assertEqual(ids[0], "kitchen_3h")     # the insight points there
        self.assertIn("adherence was too low", next(x for x in s if x["preset"] == "veg_9")["why"])


class UsualFoodTests(unittest.TestCase):
    def test_most_logged_foods_first_and_singles_dropped(self):
        store = {"2026-11-05": {"items": [{"name": "Salmon (200g)", "cal": 460, "meal": "dinner", "id": "a"}]},
                 "2026-11-06": {"items": [{"name": "Salmon (200g)", "cal": 470}, {"name": "Kale (2 cups)", "cal": 66}]},
                 "2026-11-07": {"items": [{"name": "salmon (200g)", "cal": 465}, {"name": "Kale (2 cups)", "cal": 66},
                                          {"name": "Once only"}]}}
        with mock.patch.object(server, "_nz_now", lambda: server.datetime(2026, 11, 8, 12, 0)):
            u = server.usual_foods(store=store)
        self.assertEqual([(x["item"]["name"], x["count"]) for x in u], [("salmon (200g)", 3), ("Kale (2 cups)", 2)])
        self.assertNotIn("id", u[0]["item"])


class ReviewProposalTests(unittest.TestCase):
    def test_proposals_cover_slipping_habits_experiment_and_insight(self):
        habits = [{"id": "zone2", "name": "Zone 2", "period": "week", "done": 60, "target": 180, "gap": 0.5},
                  {"id": "eat_clean", "name": "Eat clean", "period": "day", "done": 4, "missed": 3, "known": 7, "gap": 0.43},
                  {"id": "no_alcohol", "name": "No alcohol", "period": "day", "done": 7, "missed": 0, "known": 7, "gap": 0.0}]
        insight = {"key": "late_meal:hrv", "signal": "late_meal", "action": "Close the kitchen 3 hours before bed.", "text": "After a late meal…"}
        with mock.patch.object(server, "load_experiments", lambda: {"active": None, "history": []}):
            p = server.propose_next_week(habits, {"active": None}, insight)
        self.assertEqual([x["kind"] for x in p], ["habit", "habit", "experiment", "insight"])
        self.assertEqual(p[0]["text"], "Book 3 × 60 min Zone 2 sessions")
        self.assertEqual(p[2]["preset"], "kitchen_3h")
        self.assertTrue(all(x["status"] == "proposed" for x in p))
        with mock.patch.object(server, "load_experiments", lambda: {"active": {"preset": "dry"}, "history": []}):
            p = server.propose_next_week(habits[2:], {"active": {"x": 1}}, None)
        self.assertEqual(p, [])                    # all on track, experiment running, no insight

    def test_accept_edit_skip_and_this_weeks_plan(self):
        tmp = tempfile.TemporaryDirectory()
        sun = date(2026, 11, 15)
        plans = {"2026-W46": {"for_week": "2026-W47", "proposals": [
            {"id": "habit:zone2", "kind": "habit", "text": "Book 3 × 60 min Zone 2 sessions", "status": "proposed"},
            {"id": "insight:x", "kind": "insight", "text": "Close the kitchen", "status": "proposed"},
            {"id": "habit:eat_clean", "kind": "habit", "text": "Veg", "status": "proposed"}]}}
        with mock.patch.object(server, "WEEKLY_PLANS_FILE", os.path.join(tmp.name, "p.json")), \
                mock.patch.object(server, "weekly_review_screen", lambda *a, **k: {"ok": True}):
            server._save_plans(plans)
            server.decide_proposal({"week": "2026-W46", "id": "habit:zone2", "action": "accept"}, sun)
            server.decide_proposal({"week": "2026-W46", "id": "insight:x", "action": "edit", "text": "Kitchen shut 7:30pm"}, sun)
            server.decide_proposal({"week": "2026-W46", "id": "habit:eat_clean", "action": "skip"}, sun)
            with self.assertRaises(ValueError):
                server.decide_proposal({"week": "2026-W46", "id": "habit:zone2", "action": "maybe"}, sun)
            got = [(x["text"], x["status"]) for x in server.week_plan(date(2026, 11, 17))]
        tmp.cleanup()
        self.assertEqual(got, [("Book 3 × 60 min Zone 2 sessions", "accepted"), ("Kitchen shut 7:30pm", "accepted")])


if __name__ == "__main__":
    unittest.main()
