"""ZOE rules scoring (zoe_rules.py)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zoe_rules as z  # noqa: E402


class PlantTests(unittest.TestCase):
    def test_unique_plants_herbs_quarter(self):
        items = [{"plants": [{"name": "Carrots"}, {"name": "carrot"}, {"name": "Cumin", "kind": "herb_spice"}]},
                 {"plants": ["kale", {"name": "berries"}, {"name": "Berry"}]}]
        p = z.plant_points(items)
        self.assertEqual(p["plants"], ["berry", "carrot", "cumin", "kale"])
        self.assertEqual(p["points"], 3.25)

    def test_old_items_without_plants(self):
        self.assertEqual(z.plant_points([{"name": "x", "cups": 3}])["points"], 0)


class DayTests(unittest.TestCase):
    def test_full_day_scores_100(self):
        items = [{"name": "stew", "cal": 600, "fibre": 30, "nova": 1,
                  "plants": [{"name": n} for n in ("lentil", "carrot", "onion", "kale", "tomato")]},
                 {"name": "kimchi", "cal": 30, "fermented": 3, "nova": 3, "plants": [{"name": "cabbage"}]}]
        r = z.day_eval(items, items, 0, {"eating_window_h": 10})
        self.assertEqual(r["score"], 100)
        self.assertEqual(r["todo"], ["24 more plant points this week"])

    def test_gaps_and_processing(self):
        items = [{"name": "white toast", "cal": 200, "fibre": 2, "nova": 4, "eir": 90, "additives": ["E471"],
                  "fat": 2, "carbs": 35, "grams": 80, "sodium_mg": 320}]
        r = z.day_eval(items, items, 6, {"eating_window_h": 14})
        self.assertEqual(r["risky_items"], ["white toast"])
        self.assertEqual(r["processing_score"], 0)
        self.assertEqual(r["parts"]["window"], 0.5)
        self.assertIn("swap white toast for a less processed option", r["todo"])
        self.assertLess(r["score"], 20)

    def test_nothing_logged(self):
        self.assertIsNone(z.day_eval([], [], 3)["score"])


if __name__ == "__main__":
    unittest.main()
