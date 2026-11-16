import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

import server


class NutritionIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(server, "METRICS_FILE", os.path.join(self.tmp.name, "metrics.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def pts(self, *vals):
        return [{"date": f"2026-11-{d:02d} 08:00:00 +1300", "qty": v} for d, v in vals]

    def test_kj_converted_and_meals_summed_per_day(self):
        server.ingest_payload({"data": {"metrics": [
            {"name": "dietary_energy", "units": "kJ", "data": self.pts((14, 4184), (14, 4184), (15, 8368))},
            {"name": "protein", "units": "g", "data": self.pts((14, 60), (14, 70))}]}})
        st = server._load_metrics_store()
        self.assertEqual(st["2026-11-14"]["kcal_in"]["value"], 2000)
        self.assertEqual(st["2026-11-15"]["kcal_in"]["value"], 2000)
        self.assertEqual(st["2026-11-14"]["protein_in"]["value"], 130)

    def test_resent_day_replaces_rather_than_adds(self):
        for _ in range(2):
            server.ingest_payload({"data": {"metrics": [
                {"name": "dietary_energy", "units": "kcal", "data": self.pts((14, 2400))}]}})
        self.assertEqual(server._load_metrics_store()["2026-11-14"]["kcal_in"]["value"], 2400)


class TrendTests(unittest.TestCase):
    def test_trend_smooths_a_spike(self):
        t = server._smoothed_trend([("2026-11-01", 110.0), ("2026-11-02", 113.0), ("2026-11-03", 110.0)])
        self.assertLess(t[1][1], 110.5)
        self.assertAlmostEqual(t[0][1], 110.0)

    def test_gaps_weigh_the_new_reading_more(self):
        one = server._smoothed_trend([("2026-11-01", 110.0), ("2026-11-02", 109.0)])[-1][1]
        five = server._smoothed_trend([("2026-11-01", 110.0), ("2026-11-06", 109.0)])[-1][1]
        self.assertLess(five, one)


class CutStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = lambda n: os.path.join(self.tmp.name, n)
        self.patches = [mock.patch.object(server, "METRICS_FILE", p("metrics.json")),
                        mock.patch.object(server, "CUT_FILE", p("cut.json")),
                        mock.patch.object(server, "metrics_for", lambda d: server._load_metrics_store().get(d, {})),
                        mock.patch.object(server, "training_week", lambda *a, **k: {}),
                        mock.patch.object(server, "alcohol_status", lambda *a, **k: {}),
                        mock.patch.object(server, "_load_behav", lambda: {}),
                        mock.patch.object(server, "_sync_food_targets", lambda *a, **k: None)]
        for x in self.patches:
            x.start()
        self.today = date(2026, 12, 1)
        store = {}
        for i in range(20):
            d = (self.today - timedelta(days=i)).isoformat()
            store[d] = {"weight": {"value": 108.0 + i * 0.1}}
            if 1 <= i <= 4:
                store[d]["kcal_in"] = {"value": 2400}
                store[d]["protein_in"] = {"value": 150}
        server._save_metrics_store(store)
        server.cut_update_settings({"start_date": "2026-11-16", "start_weight": 110})

    def tearDown(self):
        for x in self.patches:
            x.stop()
        self.tmp.cleanup()

    def test_logging_gap_leads_the_actions(self):
        c = server.cut_status(self.today)
        self.assertEqual(c["actions"][0]["level"], "off")
        self.assertIn("Log every day", c["actions"][0]["text"])
        self.assertEqual(next(x for x in c["scorecard"] if x["key"] == "logged")["value"], "4/7")

    def test_weight_progress_and_waist_due(self):
        c = server.cut_status(self.today)
        self.assertLess(c["weight"]["week_change_kg"], 0)
        self.assertTrue(c["waist"]["due"])
        server.cut_log_waist(103.5, self.today.isoformat())
        self.assertFalse(server.cut_status(self.today)["waist"]["due"])

    def test_card_shows_last_scale_reading_and_feed_state(self):
        w = server.cut_status(self.today)["weight"]
        self.assertIsNotNone(w["scale"])
        self.assertEqual(w["scale_days_ago"], (self.today - __import__("datetime").date.fromisoformat(w["scale_date"])).days)
        self.assertIn(w["source"]["kind"], ("apple", "withings"))

    def test_bad_waist_rejected(self):
        with self.assertRaises(ValueError):
            server.cut_log_waist(20)


if __name__ == "__main__":
    unittest.main()
