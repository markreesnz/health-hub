import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import server

S = dict(server.TRAINING_DEFAULTS)
TODAY = date(2026, 11, 12)                 # a Thursday; week runs Mon 9 – Sun 15 Nov


def w(wid, day, cat, time=None, **kw):
    return {"id": wid, "date": day, "category": cat, "type": cat, "time": time, **kw}


class Zone2Tests(unittest.TestCase):
    def test_minutes_in_range_with_gap_cap(self):
        t = datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc)
        pts = [(t + timedelta(minutes=5 * i), bpm) for i, bpm in enumerate([100, 125, 130, 128, 140])]
        self.assertEqual(server.zone2_minutes(pts, 120, 135), 15)       # three 5-min samples in range
        gap = [(t, 125), (t + timedelta(minutes=60), 125)]               # a dropout counts at most 5 min
        self.assertEqual(server.zone2_minutes(gap, 120, 135), 5)
        self.assertEqual(server.zone2_minutes([], 120, 135), 0)

    def test_session_zone2_basis(self):
        self.assertEqual(server._session_zone2({"zone2_min": 40, "source": "oura"}, 120, 135), (40, "heart rate"))
        z = {"category": "endurance", "minutes": 60}
        self.assertEqual(server._session_zone2({**z, "avg_hr": 128}, 120, 135), (60, "average HR"))
        self.assertEqual(server._session_zone2({**z, "avg_hr": 95}, 120, 135), (0, None))       # a stroll
        self.assertEqual(server._session_zone2({**z, "minutes": 15}, 120, 135), (0, None))
        self.assertEqual(server._session_zone2(w("s", "x", "strength", minutes=50), 120, 135), (0, None))


class WeekTests(unittest.TestCase):
    def test_counts_dedupe_and_history(self):
        store = {
            "a1": w("a1", "2026-11-09", "strength", "07:00", source="apple"),
            "o1": w("o1", "2026-11-09", "strength", "07:10", source="oura", avg_hr=110),  # same session
            "a2": w("a2", "2026-11-10", "endurance", "06:30", source="apple", minutes=60, zone2_min=48),
            "o2": w("o2", "2026-11-10", "endurance", "06:31", source="oura", minutes=60),       # poorer dup
            "m1": w("m1", "2026-11-11", "conditioning", source="manual"),
            "x1": w("x1", "2026-11-11", "strength", "18:00", source="apple", ignored=True),
            "old": w("old", "2026-11-03", "strength", "07:00", source="apple"),
        }
        r = server.training_week(store=store, settings=S, today=TODAY)
        self.assertEqual((r["start"], r["strength"], r["hiit"], r["zone2_minutes"]), ("2026-11-09", 1, 1, 48))
        self.assertEqual(len(r["sessions"]), 3)
        self.assertEqual(next(x for x in r["sessions"] if x["kind"] == "strength")["id"], "o1")   # richer kept
        self.assertEqual(r["met"], {"strength": False, "hiit": True, "zone2": False})
        self.assertEqual(r["history"][0], {"start": "2026-11-02", "strength": 1, "hiit": 0, "zone2_minutes": 0})
        self.assertEqual(len(r["history"]), 8)


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = lambda n: os.path.join(self.tmp.name, n)
        self.patches = [mock.patch.object(server, "WORKOUTS_FILE", p("workouts.json")),
                        mock.patch.object(server, "TRAINING_FILE", p("training.json")),
                        mock.patch.object(server, "_nz_now", lambda: datetime(2026, 11, 12, 18, 30))]
        for x in self.patches:
            x.start()

    def tearDown(self):
        for x in self.patches:
            x.stop()
        self.tmp.cleanup()

    def test_log_remove_and_settings(self):
        r = server.log_session("zone2", 45)
        self.assertEqual(r["zone2_minutes"], 45)
        r = server.log_session("strength")
        self.assertEqual(r["strength"], 1)
        with self.assertRaises(ValueError):
            server.log_session("zone2")                 # minutes required
        with self.assertRaises(ValueError):
            server.log_session("yoga")
        sid = next(x["id"] for x in r["sessions"] if x["kind"] == "strength")
        self.assertEqual(server.remove_session(sid)["strength"], 0)
        store = server._load_workouts_file()
        store["apple1"] = w("apple1", "2026-11-12", "strength", "07:00", source="apple")
        server._save_workouts_file(store)
        self.assertEqual(server.remove_session("apple1")["strength"], 0)
        self.assertTrue(server._load_workouts_file()["apple1"]["ignored"])     # hidden, not deleted
        r = server.save_training_settings({"zone2_minutes": 180, "strength": 3})
        self.assertEqual(r["targets"]["zone2_minutes"], 180)
        with self.assertRaises(ValueError):
            server.save_training_settings({"zone2_hr": [140, 120]})


class FoodRatingTests(unittest.TestCase):
    def test_score_and_label(self):
        full = [{"name": "kale", "cups": 3, "group": "greens"}, {"name": "broccoli", "cups": 3, "group": "sulfur"},
                {"name": "beets", "cups": 3, "group": "color", "tags": ["seaweed"]}]
        self.assertEqual(server.food_rating(full), {"score": 100, "label": "Nutritious", "meals": 1})
        r = server.food_rating(full[:1] + [{"name": "GF bread", "wahls_ok": False}])
        self.assertEqual((r["score"], r["label"]), (38, "Limited"))       # 1/4 dailies, half clean
        self.assertEqual(server.food_rating([])["label"], None)


if __name__ == "__main__":
    unittest.main()
