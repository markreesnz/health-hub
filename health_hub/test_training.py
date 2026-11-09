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
        self.assertEqual(server._session_zone2({**z, "avg_hr": 95}, 120, 135), (0, "average HR 95 outside 120–135"))  # a stroll
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


class OuraOnlyActivityTests(unittest.TestCase):
    def test_apple_steps_and_energy_ignored(self):
        store = {}
        payload = {"data": {"metrics": [
            {"name": "step_count", "units": "count", "data": [{"date": "2026-11-08 00:00:00 +1300", "qty": 4200, "source": "iPhone"}]},
            {"name": "active_energy", "units": "kcal", "data": [{"date": "2026-11-08 00:00:00 +1300", "qty": 300, "source": "iPhone"}]},
            {"name": "body_mass", "units": "kg", "data": [{"date": "2026-11-08 00:00:00 +1300", "qty": 101.2, "source": "Scale"}]}]}}
        with mock.patch.object(server, "_load_metrics_store", lambda: store), \
                mock.patch.object(server, "_save_metrics_store", lambda st: None):
            server.ingest_payload(payload)
        day = store.get("2026-11-08", {})
        self.assertNotIn("steps", day)
        self.assertNotIn("active_energy", day)
        self.assertEqual(day["weight"]["value"], 101.2)              # other Apple metrics still flow
        states = [{"entity_id": "hae.step_count", "state": "4200", "attributes": {}},
                  {"entity_id": "hae.active_energy", "state": "300", "attributes": {"unit_of_measurement": "kcal"}}]
        self.assertEqual(server.metrics_from_ha(states), {})


class OuraWorkoutSyncTests(unittest.TestCase):
    def test_rowing_saves_even_when_heart_rate_fails(self):
        store = {}

        def get(path, params):
            if path == "workout":
                return [{"id": "w1", "activity": "rowing", "day": "2026-11-08", "intensity": "moderate",
                         "start_datetime": "2026-11-08T07:00:00+13:00", "end_datetime": "2026-11-08T07:40:00+13:00"}]
            raise RuntimeError("heartrate 400")
        with mock.patch.object(server, "_oura_get", get), \
                mock.patch.object(server, "_load_workouts_file", lambda: store), \
                mock.patch.object(server, "_save_workouts_file", store.update), \
                mock.patch.object(server, "training_settings", lambda: S), \
                mock.patch.object(server, "_nz_now", lambda: datetime(2026, 11, 9, 9, 0)):
            self.assertEqual(server.sync_oura_workouts(7), 1)
        w = store["oura_w1"]
        self.assertEqual((w["date"], w["category"], w["minutes"], w["avg_hr"]), ("2026-11-08", "endurance", 40, None))
        # no heart rate at all: a 40-minute row still counts as Zone 2, marked as such
        self.assertEqual(server._session_zone2(w, 120, 135), (40, "no heart rate"))


class WorkoutSyncStatusTests(unittest.TestCase):
    def test_status_records_success_and_failure(self):
        tmp = tempfile.TemporaryDirectory()
        with mock.patch.object(server, "WORKOUT_STATUS_FILE", os.path.join(tmp.name, "s.json")), \
                mock.patch.object(server, "_nz_now", lambda: datetime(2026, 11, 9, 13, 50)):
            self.assertEqual(server.workout_sync_status(), {})
            server._workout_sync_status(found=1, days=7)
            self.assertEqual(server.workout_sync_status(), {"at": "2026-11-09 13:50", "error": None, "found": 1, "days": 7})
            server._workout_sync_status(error="HTTP Error 401: Unauthorized")
            self.assertEqual(server.workout_sync_status()["error"], "HTTP Error 401: Unauthorized")
        tmp.cleanup()


class SessionEditTests(unittest.TestCase):
    def test_correct_a_row_and_keep_it_through_resync(self):
        store = {"oura_w1": {"id": "oura_w1", "date": "2026-11-08", "type": "other", "category": "other",
                             "time": "07:00", "minutes": 40, "avg_hr": 142, "zone2_min": None, "source": "oura"}}
        with mock.patch.object(server, "_load_workouts_file", lambda: store), \
                mock.patch.object(server, "_save_workouts_file", store.update), \
                mock.patch.object(server, "training_settings", lambda: S), \
                mock.patch.object(server, "_nz_now", lambda: datetime(2026, 11, 8, 9, 0)):
            r = server.training_week()
            self.assertEqual(r["zone2_minutes"], 0)                       # "other" never counts
            r = server.edit_session("oura_w1", kind="zone2")
            s = r["sessions"][0]
            self.assertEqual((s["kind"], s["zone2"], s["zone2_basis"]), ("zone2", 0, "average HR 142 outside 120–135"))
            r = server.edit_session("oura_w1", zone2=32)
            self.assertEqual((r["zone2_minutes"], r["sessions"][0]["zone2_basis"]), (32, "set by you"))
            with mock.patch.object(server, "_oura_get", lambda path, params: [
                    {"id": "w1", "activity": "other", "day": "2026-11-08", "start_datetime": "2026-11-08T07:00:00+13:00",
                     "end_datetime": "2026-11-08T07:40:00+13:00"}] if path == "workout" else []):
                server.sync_oura_workouts(7)                              # Oura resends the same workout
            self.assertEqual(server.training_week()["zone2_minutes"], 32)
            with self.assertRaises(ValueError):
                server.edit_session("oura_w1", kind="yoga")


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
