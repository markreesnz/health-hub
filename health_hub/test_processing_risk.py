"""Calibration tests: ZOE's published examples should land where ZOE puts them.
Run: python3 -m unittest health_hub/test_processing_risk.py (from the repo root)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from processing_risk import additive_tier, classify, day_summary, hyperpalatable  # noqa: E402


def lvl(**kw):
    return classify(kw)["level"]


class ZoeExamples(unittest.TestCase):
    def test_whole_foods_unprocessed(self):
        self.assertEqual(lvl(nova=1, cal=55, carbs=12, grams=100, eir=20), "unprocessed")
        self.assertEqual(lvl(nova=1, cal=165, protein=31, fat=3.6, grams=100), "unprocessed")

    def test_unsalted_butter_no_risk(self):
        self.assertEqual(lvl(nova=2, grams=10, cal=72, fat=8.1, carbs=0, sugar=0,
                             sodium_mg=1, eir=40, additives=[]), "none")

    def test_dark_chocolate_no_risk(self):
        self.assertEqual(lvl(nova=3, grams=25, cal=150, fat=10.7, carbs=11, sugar=6,
                             sodium_mg=5, eir=55, additives=["E322"]), "none")

    def test_weetabix_low(self):
        self.assertEqual(lvl(nova=4, grams=37.5, cal=136, fat=0.8, carbs=26, sugar=1.7,
                             sodium_mg=100, eir=50, additives=[]), "low")

    def test_seeded_sourdough_low(self):
        self.assertEqual(lvl(nova=3, grams=80, cal=210, fat=4, carbs=36, sugar=2,
                             sodium_mg=380, eir=45, additives=[]), "low")

    def test_milk_chocolate_high(self):
        self.assertEqual(lvl(nova=4, grams=45, cal=240, fat=13.3, carbs=25.6, sugar=25,
                             sodium_mg=45, eir=85, additives=["E442", "E476"]), "high")

    def test_soft_white_bread_high(self):
        self.assertEqual(lvl(nova=4, grams=80, cal=190, fat=1.6, carbs=35, sugar=2.5,
                             sodium_mg=320, eir=90, additives=["E471", "E481", "E300"]), "high")

    def test_chicken_nuggets_high(self):
        self.assertEqual(lvl(nova=4, grams=100, cal=290, fat=18, carbs=17, sugar=0.5,
                             sodium_mg=550, eir=95, additives=["E450", "modified starch"]), "high")

    def test_many_low_additives_are_fine(self):
        self.assertEqual(lvl(nova=4, grams=150, cal=90, fat=0.5, carbs=15, sugar=12,
                             sodium_mg=40, eir=30,
                             additives=["pectin", "citric acid", "ascorbic acid", "inulin"]),
                         "low")

    def test_one_high_additive_alone_is_medium(self):
        self.assertEqual(lvl(nova=4, grams=250, cal=120, fat=1, carbs=10, sugar=8,
                             sodium_mg=100, eir=40, additives=["carrageenan"]), "medium")


class Pieces(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(additive_tier("E407"), "high")
        self.assertEqual(additive_tier("emulsifier (e471)"), "high")
        self.assertEqual(additive_tier("Mono- and diglycerides of fatty acids"), "high")
        self.assertEqual(additive_tier("E 211"), "medium")
        self.assertEqual(additive_tier("pectin"), "low")
        self.assertEqual(additive_tier("E330"), "low")

    def test_hp_needs_data(self):
        self.assertIsNone(hyperpalatable({"cal": 200, "fat": 15}))  # no sodium/sugar → can't fire
        self.assertEqual(hyperpalatable({"cal": 500, "fat": 30, "carbs": 50, "grams": 100,
                                         "sodium_mg": 500}), "fat+salt")

    def test_day_summary(self):
        s = day_summary([{"cal": 300, "processing": {"level": "high"}},
                         {"cal": 700, "processing": {"level": "unprocessed"}},
                         {"cal": 200}])
        self.assertEqual(s["pct"]["high"], 30)
        self.assertEqual(s["risky_pct"], 30)
        self.assertEqual(s["scored_items"], 2)


if __name__ == "__main__":
    unittest.main()
