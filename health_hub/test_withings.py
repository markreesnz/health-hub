import os
import tempfile
import time
import unittest
from unittest import mock

import server


def grp(ts, kg=None, fat=None, cat=1):
    ms = []
    if kg is not None:
        ms.append({"type": 1, "value": int(kg * 1000), "unit": -3})
    if fat is not None:
        ms.append({"type": 6, "value": int(fat * 10), "unit": -1})
    return {"date": ts, "category": cat, "measures": ms}


# 2026-11-09 06:30 and 19:00 NZDT (UTC+13) → both the 9th locally, though 06:30 is the 8th in UTC
MORNING, EVENING = 1794159000, 1794204000


class DailyTests(unittest.TestCase):
    def test_first_weigh_in_of_the_local_day_wins(self):
        d = server.withings_daily([grp(EVENING, 101.9), grp(MORNING, 101.2, 24.5), grp(MORNING - 86400, 101.6)])
        self.assertEqual(d["2026-11-09"]["weight"], 101.2)
        self.assertEqual(d["2026-11-09"]["weight_time"], "06:30")
        self.assertEqual(d["2026-11-09"]["body_fat"], 24.5)
        self.assertEqual(d["2026-11-08"]["weight"], 101.6)

    def test_objectives_are_not_measurements(self):
        self.assertEqual(server.withings_daily([grp(MORNING, 95.0, cat=2)]), {})


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = {}
        p = lambda n: os.path.join(self.tmp.name, n)
        self.patches = [mock.patch.object(server, "WITHINGS_FILE", p("withings.json")),
                        mock.patch.object(server, "_load_metrics_store", lambda: self.store),
                        mock.patch.object(server, "_save_metrics_store", lambda st: None),
                        mock.patch.object(server.threading, "Thread", mock.MagicMock())]
        for x in self.patches:
            x.start()

    def tearDown(self):
        for x in self.patches:
            x.stop()
        self.tmp.cleanup()

    def test_setup_connect_refresh_and_sync(self):
        with self.assertRaises(ValueError):
            server.withings_save_creds("id", "secret", "http://insecure")
        st = server.withings_save_creds("id", "secret", "https://localhost/withings")
        self.assertIn("redirect_uri=https%3A%2F%2Flocalhost%2Fwithings", st["authorize_url"])
        self.assertIn("scope=user.metrics", st["authorize_url"])
        calls = []

        def post(url, params, token=None):
            calls.append((url, params.get("action"), params.get("grant_type"), token))
            if params.get("action") == "requesttoken":
                return {"access_token": "a2" if params["grant_type"] == "refresh_token" else "a1",
                        "refresh_token": "r2" if params["grant_type"] == "refresh_token" else "r1", "expires_in": 10800}
            return {"measuregrps": [grp(MORNING, 101.2, 24.5)], "more": 0}
        with mock.patch.object(server, "_withings_post", post):
            server.withings_exchange_code("https://localhost/withings?code=abc123&state=health-hub")
            self.assertEqual(calls[0][1:3], ("requesttoken", "authorization_code"))
            w = server._withings_load()
            w["expires_at"] = time.time() + 60                     # about to expire → refresh first
            server._withings_save(w)
            server.withings_sync(14)
        self.assertEqual(calls[1][2], "refresh_token")
        self.assertEqual(calls[2][3], "a2")                        # measures fetched with the new token
        self.assertEqual(server._withings_load()["refresh_token"], "r2")   # rotated refresh token kept
        self.assertEqual(self.store["2026-11-09"]["weight"], {"value": 101.2, "unit": "kg", "source": "Withings", "time": "06:30"})
        self.assertEqual(server.withings_status()["last_weight"], {"date": "2026-11-09", "kg": 101.2})

    def test_withings_weight_is_not_overwritten_by_apple(self):
        self.store["2026-11-09"] = {"weight": {"value": 101.2, "unit": "kg", "source": "Withings"}}
        payload = {"data": {"metrics": [{"name": "body_mass", "units": "kg",
                                         "data": [{"date": "2026-11-09 08:00:00 +1300", "qty": 103.0, "source": "Health"}]}]}}
        server.ingest_payload(payload)
        self.assertEqual(self.store["2026-11-09"]["weight"]["value"], 101.2)
        self.assertTrue(server._device_owned("weight", {"source": "Withings"}))
        self.assertFalse(server._device_owned("weight", {"source": "Health"}))


if __name__ == "__main__":
    unittest.main()
