import os
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

import server

S = dict(server.ALCOHOL_DEFAULTS, reset_start="2026-10-28")
WED, FRI, SAT = date(2026, 11, 4), date(2026, 12, 4), date(2026, 12, 5)


def wine(t, std=1.5):
    return {"id": t, "time": t, "kind": "wine", "std": std}


class RuleTests(unittest.TestCase):
    def info(self, rec, d):
        return server._drink_day_info(rec, d, S)

    def test_blank_and_unsubmitted_days_are_unknown_not_dry(self):
        self.assertEqual(self.info({}, FRI)["status"], "unknown")
        self.assertEqual(self.info({"drink": 0}, FRI)["status"], "unknown")
        self.assertEqual(self.info({"drink": 0, "locked": True}, FRI)["status"], "dry")
        self.assertEqual(self.info({"drink": 0, "dry_confirmed": True}, FRI)["status"], "dry")

    def test_rules(self):
        self.assertEqual(self.info({"drinks": [wine("18:30")]}, SAT)["broken"], [])
        self.assertEqual(self.info({"drinks": [wine("18:30")]}, date(2026, 12, 3))["broken"], ["weekday"])
        self.assertEqual(self.info({"drinks": [wine("18:00"), wine("19:00")]}, SAT)["broken"], ["over_limit"])
        self.assertEqual(self.info({"drinks": [wine("20:00", 1)]}, SAT)["broken"], ["late"])
        self.assertEqual(self.info({"drinks": [wine("00:30", 1)]}, SAT)["broken"], ["late"])
        self.assertIn("reset", self.info({"drinks": [wine("18:00", 1)]}, date(2026, 11, 7))["broken"])
        self.assertNotIn("reset", self.info({"drinks": [wine("18:00", 1)]}, date(2026, 11, 27))["broken"])

    def test_reset_day_and_streak(self):
        store = {"2026-10-28": {"drink": 0, "locked": True},
                 "2026-10-29": {"drink": 0, "dry_confirmed": True},
                 "2026-10-30": {}}                       # today, not answered yet
        r = server._reset_info(store, S, date(2026, 10, 30))
        self.assertEqual((r["day"], r["active"], r["streak"]), (3, True, 2))
        store["2026-10-29"] = {"drinks": [wine("19:00")], "drink": 1}
        r = server._reset_info(store, S, date(2026, 10, 30))
        self.assertEqual((r["streak"], r["drink_days"]), (0, ["2026-10-29"]))
        r = server._reset_info({}, S, date(2026, 11, 27))
        self.assertEqual((r["day"], r["active"], r["complete"]), (30, False, True))

    def test_week_counts_skip_future_days(self):
        st = server.alcohol_status("2026-12-02", {"2026-11-30": {"drink": 0, "locked": True}}, S,
                                   today=date(2026, 12, 2))
        self.assertEqual([d["status"] for d in st["week"]["days"]],
                         ["dry", "unknown", "unknown", "future", "future", "future", "future"])
        self.assertEqual((st["week"]["dry_days"], st["week"]["unknown_days"]), (1, 2))


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = lambda n: os.path.join(self.tmp.name, n)
        self.patches = [mock.patch.object(server, "BEHAV_FILE", p("behaviours.json")),
                        mock.patch.object(server, "ALCOHOL_FILE", p("alcohol.json")),
                        mock.patch.object(server, "BACKUPS", self.tmp.name),
                        mock.patch.object(server, "_ha_request", lambda *a, **k: None),
                        mock.patch.object(server, "alcohol_settings", lambda: S)]
        for x in self.patches:
            x.start()

    def tearDown(self):
        for x in self.patches:
            x.stop()
        self.tmp.cleanup()

    def at(self, hhmm, d=SAT):
        h, m = map(int, hhmm.split(":"))
        return mock.patch.object(server, "_nz_now", lambda: datetime(d.year, d.month, d.day, h, m))

    def test_add_now_lands_on_today(self):
        with self.at("19:10"):
            st = server.add_drink({"kind": "wine"})
        self.assertEqual((st["day"]["date"], st["day"]["std"]), ("2026-12-05", 1.5))
        self.assertEqual(st["day"]["drinks"][0]["time"], "19:10")

    def test_after_midnight_and_next_morning_belong_to_last_night(self):
        with self.at("00:40"):
            self.assertEqual(server.add_drink({"kind": "wine"})["day"]["date"], "2026-12-04")
        with self.at("08:00"):
            st = server.add_drink({"kind": "wine", "time": "21:00"})
        self.assertEqual(st["day"]["date"], "2026-12-04")
        self.assertEqual([x["time"] for x in st["day"]["drinks"]], ["21:00", "00:40"])
        self.assertEqual(sorted(st["day"]["broken"]), ["late", "over_limit"])

    def test_none_remove_and_checkin_guard(self):
        with self.at("19:00"):
            st = server.mark_no_drinks()
            self.assertEqual(st["day"]["status"], "dry")
            st = server.add_drink({"kind": "wine"})
            self.assertEqual(st["day"]["status"], "drink")
            with self.assertRaises(ValueError):
                server.mark_no_drinks()
            rec = server.save_checkin({"date": "2026-12-05", "drink": False})
            self.assertEqual(rec["drink"], 1)              # check-in can't erase logged drinks
            st = server.remove_drink("2026-12-05", st["day"]["drinks"][0]["id"])
            self.assertEqual(st["day"]["status"], "unknown")

    def test_backup_written_before_first_change(self):
        with open(server.BEHAV_FILE, "w") as f:
            f.write('{"2026-01-01": {"drink": 1}}')
        with self.at("19:00"):
            server.add_drink({"kind": "wine"})
        with open(os.path.join(self.tmp.name, "behaviours-before-drink-log.json")) as f:
            self.assertEqual(f.read(), '{"2026-01-01": {"drink": 1}}')

    def test_client_id_is_idempotent_and_dry_run_saves_nothing(self):
        with self.at("19:00"):
            st = server.add_drink({"kind": "wine", "dry_run": True})
            self.assertTrue(st["dry_run"])
            self.assertEqual(st["day"]["std"], 1.5)
            self.assertEqual(server.alcohol_status()["day"]["status"], "unknown")
            server.add_drink({"kind": "wine", "id": "abcdef0123456789"})
            again = server.add_drink({"kind": "wine", "id": "abcdef0123456789"})
        self.assertTrue(again["already_applied"])
        self.assertEqual(len(again["day"]["drinks"]), 1)

    def test_bad_input(self):
        with self.at("19:00"):
            for bad in ({"time": "25:00"}, {"kind": "beer"}, {"std": 50}):
                with self.assertRaises(ValueError):
                    server.add_drink(bad)


class FoodItemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(server, "FOOD_FILE", os.path.join(self.tmp.name, "food.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_items_saved_once_and_sanitised(self):
        items = [{"name": "Kale (2 cups)", "cups": 2, "group": "greens", "cal": 70, "evil": "x"}]
        out = server.add_food_items(items, "lunch", "2026-12-05", "0123456789abcdef")
        self.assertEqual(out["wahls"]["cups"]["greens"], 2.0)
        self.assertNotIn("evil", out["items"][0])
        again = server.add_food_items(items, "lunch", "2026-12-05", "0123456789abcdef")
        self.assertTrue(again["already_applied"])
        self.assertEqual(len(again["items"]), 1)
        with self.assertRaises(ValueError):
            server.add_food_items([], "lunch", "2026-12-05")


class SnapshotTests(unittest.TestCase):
    def test_toggle_off_is_not_recorded_as_dry(self):
        store = {}
        today = date.today().isoformat()
        states = [{"entity_id": "input_boolean.had_a_drink", "state": "off"},
                  {"entity_id": "input_boolean.ate_clean", "state": "on"}]
        with mock.patch.object(server, "_load_behav", lambda: store), \
                mock.patch.object(server, "_save_behav", store.update):
            server.snapshot_behaviours(states)
            self.assertNotIn("drink", store[today])
            states[0]["state"] = "on"
            server.snapshot_behaviours(states)
            self.assertEqual(store[today]["drink"], 1)


class ReviewTests(unittest.TestCase):
    def test_weekly_review(self):
        store = {"2026-11-30": {"drink": 0, "locked": True},
                 "2026-12-03": {"drinks": [wine("20:30")], "drink": 1},
                 "2026-12-05": {"drinks": [wine("18:00")], "drink": 1}}
        food = {"2026-11-30": {"items": [{"cups": 3, "group": "greens"}, {"cups": 3, "group": "sulfur"},
                                         {"cups": 3, "group": "color", "tags": ["seaweed"]}]},
                "2026-12-01": {"items": [{"cups": 1, "group": "greens"}]}}
        sm = {"weight": {"2026-11-24": 102.0, "2026-12-01": 101.0},
              "hrv": {"2026-12-01": 50, "2026-12-04": 30, "2026-12-06": 34},
              "sleep": {"2026-12-01": 7.5, "2026-12-04": 6.0}}
        v = server.weekly_review("2026-12-06", store, food, sm, S, today=date(2026, 12, 6))
        rules = {r["rule"]: r for r in v["rules"]}
        self.assertFalse(rules["Mon–Thu dry"]["kept"])
        self.assertEqual(rules["Mon–Thu dry"]["dates"], ["2026-12-03"])
        self.assertFalse(rules["Nothing after 8pm"]["kept"])
        self.assertTrue(rules["Max 2 standard drinks"]["kept"])
        self.assertNotIn("No drinks (30-day reset)", rules)    # reset ended 26 Nov
        self.assertEqual(v["alcohol"]["std"], 3.0)
        self.assertEqual(v["veg"]["avg_cups"]["greens"], 2.0)
        self.assertEqual(v["veg"]["days_all_9"], 1)
        self.assertEqual(v["weight"]["change"], -1.0)
        self.assertEqual(v["sleep_after"]["drink"], {"nights": 2, "hrv": 32.0, "sleep_h": 6.0})
        self.assertEqual(v["sleep_after"]["dry"]["hrv"], 50.0)
        self.assertTrue(v["complete"])


class RangeTests(unittest.TestCase):
    def test_range_summary(self):
        store = {"2026-12-05": {"drinks": [wine("21:00")], "drink": 1}, "2026-12-04": {"drink": 0, "locked": True}}
        food = {"2026-12-04": {"items": [{"name": "kale", "cups": 3, "group": "greens"},
                                         {"name": "GF bread", "wahls_ok": False}], "totals": {"cal": 500}}}
        with mock.patch.object(server, "_load_behav", lambda: store), \
                mock.patch.object(server, "_load_food", lambda: food), \
                mock.patch.object(server, "_series_map", lambda: {"weight": {"2026-12-04": 101.0}}), \
                mock.patch.object(server, "alcohol_settings", lambda: S), \
                mock.patch.object(server, "_drinking_day", lambda: date(2026, 12, 5)):
            r = server.range_summary("2026-12-04", "2026-12-06")
            with self.assertRaises(ValueError):
                server.range_summary("2026-12-06", "2026-12-04")
        d4, d5, d6 = r["days"]
        self.assertEqual((d4["alcohol"], d4["off_plan"], d4["weight"]), ("dry", ["GF bread"], 101.0))
        self.assertEqual((d5["alcohol"], d5["rules_broken"]), ("drink", ["late"]))
        self.assertEqual(d6["alcohol"], "future")
        self.assertEqual(r["summary"]["avg_cups"]["greens"], 3.0)
        self.assertEqual((r["summary"]["drink_days"], r["summary"]["dry_days"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
