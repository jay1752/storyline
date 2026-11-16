""""Passes your rules" lists only stocks that pass every rule; "Misses one of your rules" holds the near misses.
Run: python -m unittest tests.test_rules_screens -v   (from the repo root)"""

import unittest
from datetime import date, timedelta
from unittest import mock

from app import discovery, framework

KEYS = ["roce", "de", "pledge", "sales_growth", "opm", "promoter", "interest_cover", "current_ratio", "quick_ratio", "cfo_pat"]
GOOD = {"roce": 30, "de": 0.1, "pledge": 0, "sales_growth": 35, "opm": 35, "promoter": 0.0, "interest_cover": 20, "current_ratio": 2.0, "quick_ratio": 1.5, "cfo_pat": 1.0}


def stock(sym, raws):
    return {"symbol": sym, "name": sym, "market": "IN", "sector": "X", "cap_tier": "large", "mcap": 50000.0, "pe": 20.0, "roce": raws.get("roce", 30), "de": raws.get("de", 0.1),
            "bank": 0, "raws": raws, "sales_cagr_3y": 20.0, "growth_min4": 35.0, "cfo_pat": 1.0, "div_payout": 10.0, "roce_3y": 30.0, "opm": 35.0, "n_q": 12,
            "last_q": (date.today() - timedelta(days=60)).isoformat(), "price": 100.0, "pe_low": 10.0, "pe_high": 30.0}


UNIVERSE = [stock("PASSALL", dict(GOOD)), stock("ONEMISS", {**GOOD, "de": 0.9}), stock("TWOMISS", {**GOOD, "de": 0.9, "opm": 5}),
            stock("THIN", {k: GOOD[k] for k in KEYS[:5]})]


class RulesScreens(unittest.TestCase):
    def run_screen(self, key):
        with mock.patch.object(discovery, "_load", return_value=UNIVERSE), mock.patch.object(discovery, "_visible", return_value=True):
            return discovery.run_screen(key, framework.builtin())

    def test_passes_your_rules_means_no_rule_failed(self):
        res = self.run_screen("rules")
        self.assertEqual([r["symbol"] for r in res["rows"]], ["PASSALL"])
        self.assertEqual(res["rows"][0]["judge"]["failing"], [])

    def test_misses_one_lists_exactly_the_near_misses(self):
        res = self.run_screen("near")
        self.assertEqual([r["symbol"] for r in res["rows"]], ["ONEMISS"])
        self.assertEqual(res["rows"][0]["judge"]["failing"], ["Debt / equity"])

    def test_a_stock_with_too_few_judgeable_rules_is_in_neither(self):
        for key in ("rules", "near"):
            self.assertNotIn("THIN", [r["symbol"] for r in self.run_screen(key)["rows"]])

    def test_the_near_screen_is_listed_next_to_the_main_one_and_the_copy_is_honest(self):
        keys = [s["key"] for s in discovery.SCREENS]
        self.assertEqual(keys[:2], ["rules", "near"])
        self.assertIn("every threshold", discovery.SCREEN_BY_KEY["rules"]["desc"])

    def test_the_why_column_reads_for_the_new_screen(self):
        from app.routes.analytics import _why
        row = self.run_screen("near")["rows"][0]
        self.assertEqual(_why("near", row), "9 of 10 rules · misses Debt / equity")


if __name__ == "__main__":
    unittest.main()
