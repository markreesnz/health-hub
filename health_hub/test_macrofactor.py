import os
import tempfile
import unittest
from unittest import mock

import server

DAY = "2026-11-19"


def food(name, cal=100, t="06:30", p=10, c=5, f=3):
    return {"name": name, "time": t, "calories": cal, "protein": p, "carbs": c, "fat": f}


def fake_parse(desc):
    # the parser sees MacroFactor's numbers and returns its own guess plus ZOE inputs
    return [{"name": "x", "cal": 999, "protein": 1, "carbs": 1, "fat": 1, "fibre": 2.0,
             "plants": ["salmon"], "fermented": 0, "grams": 120, "nova": 1}]


class MacroFactorSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(server, "FOOD_FILE", os.path.join(self.tmp.name, "food.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def items(self):
        return server._load_food().get(DAY, {}).get("items", [])

    def test_adds_with_macrofactor_macros_and_parser_extras(self):
        r = server.mf_apply(DAY, [food("Salmon (Akaroa)", 298.638, "10:17", 20.475, 1.75, 24.36)], fake_parse)
        self.assertEqual(r["added"], 1)
        [i] = self.items()
        self.assertEqual((i["name"], i["cal"], i["protein"], i["fat"]), ("Salmon (Akaroa)", 298.6, 20.5, 24.4))
        self.assertEqual((i["fibre"], i["plants"], i["meal"], i["at"], i["source"]), (2.0, ["salmon"], "breakfast", "10:17", "macrofactor"))
        self.assertEqual(server._load_food()[DAY]["totals"]["cal"], 299)

    def test_commas_in_names_stay_one_item(self):
        seen = []
        server.mf_apply(DAY, [food("Chicken Breast, Cooked")], lambda d: seen.append(d) or [])
        self.assertNotIn(",", seen[0])
        self.assertEqual(len(self.items()), 1)

    def test_second_sync_is_a_no_op_and_follows_edits(self):
        server.mf_apply(DAY, [food("Egg", 70)], fake_parse)
        self.assertEqual(server.mf_apply(DAY, [food("Egg", 70)], fake_parse)["added"], 0)
        server.mf_apply(DAY, [food("Egg", 140, "07:00")], fake_parse)
        [i] = self.items()
        self.assertEqual((i["cal"], i["at"]), (140, "07:00"))

    def test_deleted_in_macrofactor_is_removed_here_but_own_items_stay(self):
        server.mf_apply(DAY, [food("Egg")], fake_parse)
        server.add_food_items([{"name": "Coffee", "cal": 5}], "breakfast", DAY, at="07:00")
        r = server.mf_apply(DAY, [], fake_parse)
        self.assertEqual(r["removed"], 1)
        self.assertEqual([i["name"] for i in self.items()], ["Coffee"])

    def test_removed_here_is_not_re_added(self):
        server.mf_apply(DAY, [food("Egg")], fake_parse)
        server.remove_food_item(self.items()[0]["id"], DAY)
        self.assertEqual(server.mf_apply(DAY, [food("Egg")], fake_parse)["added"], 0)
        self.assertEqual(self.items(), [])

    def test_meal_moved_here_is_kept(self):
        server.mf_apply(DAY, [food("Egg")], fake_parse)
        server.update_food_item(self.items()[0]["id"], meal="lunch", day=DAY)
        server.mf_apply(DAY, [food("Egg")], fake_parse)
        self.assertEqual(self.items()[0]["meal"], "lunch")

    def test_parse_failure_still_logs_the_macros(self):
        def boom(_):
            raise RuntimeError("no key")
        server.mf_apply(DAY, [food("Mystery bar", 210)], boom)
        [i] = self.items()
        self.assertEqual((i["name"], i["cal"]), ("Mystery bar", 210))
        self.assertNotIn("plants", i)

    def test_meal_from_time(self):
        self.assertEqual([server._mf_meal(t) for t in ("06:30", "12:29", "15:30", "19:00", "22:10", None)],
                         ["breakfast", "lunch", "snack", "dinner", "snack", "snack"])

    def test_sync_without_settings_does_nothing(self):
        with mock.patch.object(server, "MF_URL", ""), mock.patch.object(server, "MF_TOKEN", ""):
            self.assertIn("skipped", server.mf_sync())


if __name__ == "__main__":
    unittest.main()


class ZoePushTests(unittest.TestCase):
    def test_sync_pushes_todays_zoe_status_and_survives_a_push_failure(self):
        sent = []

        class Resp:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *a): return self.body

        def fake_urlopen(req, timeout=0):
            if req.full_url.endswith("/zoe-status"):
                sent.append(server.json.loads(req.data))
                if len(sent) == 2:
                    raise OSError("network down")
                return Resp(b'{"ok": true}')
            return Resp(b'{"foods": []}')

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(server, "FOOD_FILE", os.path.join(tmp, "food.json")), \
             mock.patch.object(server, "MF_URL", "https://mf.example"), \
             mock.patch.object(server, "MF_TOKEN", "hub"), \
             mock.patch.object(server.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.object(server.json, "load", lambda r: server.json.loads(r.read())):
            today = server._nz_now().date().isoformat()
            server.add_food_items([{"name": "Kimchi", "cal": 12, "plants": ["cabbage"], "fermented": 1, "fibre": 1.5}],
                                  "lunch", today, at="12:00")
            r = server.mf_sync()
            self.assertIsNone(r["error"])
            self.assertEqual(sent[0]["date"], today)
            self.assertEqual(sent[0]["zoe"]["ferments"], 1)
            self.assertIn("cabbage", sent[0]["zoe"]["plants_week_list"])
            self.assertIsNone(server.mf_sync()["error"])   # a failed ZOE push doesn't fail the food sync


class FastFoodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = [mock.patch.object(server, "FOOD_FILE", os.path.join(self.tmp.name, "food.json")),
                  mock.patch.object(server, "mf_push_zoe", lambda day: {"ok": True})]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.tmp.cleanup()

    def items(self):
        return server._load_food().get(DAY, {}).get("items", [])

    def test_foods_appear_at_once_and_zoe_inputs_follow(self):
        import threading, time
        gate = threading.Event()

        def slow_parse(desc):
            gate.wait(5)                                  # the parser is slow…
            return fake_parse(desc)

        with mock.patch.object(server, "parse_food_text", slow_parse):
            r = server.mf_apply(DAY, [food("Salmon (Akaroa)", 298.6), food("Egg", 70)])
            self.assertEqual(r["added"], 2)
            now = self.items()                            # …but the foods are already in the log
            self.assertEqual(sorted(i["name"] for i in now), ["Egg", "Salmon (Akaroa)"])
            self.assertTrue(all(i.get("zoe_pending") and "plants" not in i for i in now))
            self.assertEqual(server._load_food()[DAY]["totals"]["cal"], 369)
            gate.set()
            for _ in range(50):
                if not any(i.get("zoe_pending") for i in self.items()):
                    break
                time.sleep(0.1)
        done = self.items()
        self.assertTrue(all(i.get("plants") == ["salmon"] and i.get("fibre") == 2.0 for i in done))
        self.assertEqual([i["cal"] for i in done], [298.6, 70])   # MacroFactor's macros kept

    def test_a_failed_parse_leaves_the_food_logged(self):
        import time

        def boom(desc):
            raise RuntimeError("no key")

        with mock.patch.object(server, "parse_food_text", boom):
            server.mf_apply(DAY, [food("Mystery bar", 210)])
            time.sleep(0.3)
        [i] = self.items()
        self.assertEqual((i["name"], i["cal"]), ("Mystery bar", 210))

    def test_only_one_sync_at_a_time_and_page_open_trigger_is_throttled(self):
        with mock.patch.object(server, "MF_URL", "https://mf.example"), mock.patch.object(server, "MF_TOKEN", "hub"):
            server._MF_SYNC_LOCK.acquire()
            try:
                self.assertIn("already running", server.mf_sync()["skipped"])
                self.assertFalse(server.mf_sync_if_stale())
            finally:
                server._MF_SYNC_LOCK.release()
            with mock.patch.dict(server.MF_STATUS, {"last_sync": server.datetime.now().isoformat(timespec="seconds")}):
                self.assertFalse(server.mf_sync_if_stale())          # synced just now
            started = []
            with mock.patch.dict(server.MF_STATUS, {"last_sync": "2026-01-01T00:00:00"}), \
                 mock.patch.object(server.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda s: started.append(target)})()):
                self.assertTrue(server.mf_sync_if_stale())
            self.assertEqual(started, [server.mf_sync])


class ServerSpeedTests(unittest.TestCase):
    def test_backlog_and_gzip(self):
        import gzip, http.client, json as _j, threading
        self.assertGreaterEqual(server.HubServer.request_queue_size, 64)
        srv = server.HubServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            c.request("GET", "/", headers={"Accept-Encoding": "gzip"})
            r = c.getresponse()
            body = r.read()
            self.assertEqual(r.getheader("Content-Encoding"), "gzip")
            self.assertIn(b"<html", gzip.decompress(body)[:2000].lower())
            c.request("GET", "/", headers={})
            r = c.getresponse()
            self.assertIsNone(r.getheader("Content-Encoding"))
            r.read()
        finally:
            srv.shutdown()
