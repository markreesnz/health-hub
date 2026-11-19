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
