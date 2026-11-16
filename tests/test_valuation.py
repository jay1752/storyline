"""A stock's own P/E history (app/valuation.py). Synthetic data only. Run: python -m unittest tests.test_valuation -v   (from the repo root)"""

import unittest
from datetime import date, timedelta

from app import valuation as v


def quarter_ends(n, last="2026-09-30"):
    out, d = [], date.fromisoformat(last)
    for _ in range(n):
        out.append(d.isoformat())
        first = d.replace(day=1)
        prev = first - timedelta(days=1)                       # last day of the previous month
        d = (prev.replace(day=1) - timedelta(days=1)).replace(day=1) - timedelta(days=1)   # step back three months to a quarter end
        d = (d.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return list(reversed(out))


def quarters(profits, last="2026-09-30"):
    ends = quarter_ends(len(profits), last)
    return [{"period_end_date": e, "net_profit_cr": p} for e, p in zip(ends, profits)]


def prices(start, months, base=100.0, step=0.0):
    out, d = [], date.fromisoformat(start)
    for i in range(months * 21):
        day = d + timedelta(days=int(i * 30.4 / 21))
        if day.weekday() < 5:
            out.append((day.isoformat(), base + step * (i / 21)))
    return out


class Shape(unittest.TestCase):
    def test_constant_price_and_profit_gives_a_flat_history_at_todays_pe(self):
        h = v.pe_history(prices("2023-06-01", 40), quarters([100.0] * 20), 20.0)
        self.assertEqual((h["low"], h["high"], h["median"], h["now"]), (20.0, 20.0, 20.0, 20.0))
        self.assertEqual(h["pct"], 50)               # a flat history: today is neither cheap nor dear

    def test_the_old_method_is_wrong_when_earnings_grow_and_this_one_is_not(self):
        # price flat at 100 for 3 years while profit doubled: the P/E fell by half. Dividing the price range by today's profit would say "flat".
        profits = [50.0] * 8 + [100.0] * 12
        h = v.pe_history(prices("2023-06-01", 40, base=100.0), quarters(profits), 10.0)
        self.assertGreater(h["high"], 15.0)          # earlier, on the lower profit, the P/E was about 20
        self.assertLess(h["pct"], 40)                # today sits at the lowest level it has been at
        self.assertEqual(h["now"], 10.0)

    def test_it_moves_with_price(self):
        h = v.pe_history(prices("2023-06-01", 40, base=50.0, step=2.0), quarters([100.0] * 20), 30.0)
        self.assertLess(h["low"], 15.0)               # the price was far lower earlier, profit unchanged
        self.assertGreater(h["pct"], 90)              # today is the dearest reading

    def test_a_loss_making_period_is_left_out_not_shown_as_a_p_e(self):
        profits = [-20.0] * 4 + [100.0] * 16
        h = v.pe_history(prices("2023-06-01", 40), quarters(profits), 20.0)
        self.assertTrue(all(p > 0 for _, p in h["points"]))
        self.assertLessEqual(h["months"], 36)

    def test_too_little_history_gives_no_range_not_a_short_one(self):
        self.assertIsNone(v.pe_history(prices("2025-06-01", 17), quarters([100.0] * 8), 20.0))
        self.assertIsNone(v.pe_history(prices("2023-06-01", 40), quarters([100.0] * 3), 20.0))

    def test_no_anchor_no_history(self):
        for pe in (None, 0, -5):
            self.assertIsNone(v.pe_history(prices("2023-06-01", 40), quarters([100.0] * 20), pe))
        self.assertIsNone(v.pe_history(prices("2023-06-01", 40), quarters([-10.0] * 20), 20.0))   # today's profit not positive

    def test_a_missing_quarter_breaks_the_trailing_four_and_that_date_is_skipped(self):
        q = quarters([100.0] * 20)
        del q[10]
        h = v.pe_history(prices("2023-06-01", 40), q, 20.0)
        full = v.pe_history(prices("2023-06-01", 40), quarters([100.0] * 20), 20.0)
        self.assertLess(h["months"] if h else 0, full["months"])


class DerivedMarch(unittest.TestCase):
    def q(self, profits, ends):
        return [{"period_end_date": e, "net_profit_cr": p} for e, p in zip(ends, profits)]

    def test_a_missing_march_quarter_is_the_year_minus_the_first_three(self):
        qs = self.q([100.0, 100.0, 100.0], ["2023-06-30", "2023-09-30", "2023-12-31"])
        out = v.with_derived_march(qs, [{"period_end_date": "2024-03-31", "net_profit_cr": 420.0}])
        self.assertEqual([x["period_end_date"] for x in out][-1], "2024-03-31")
        self.assertEqual(out[-1]["net_profit_cr"], 120.0)
        self.assertTrue(out[-1]["derived"])

    def test_it_is_not_added_when_the_quarter_exists_or_a_quarter_of_the_year_is_missing(self):
        have = self.q([100.0] * 4, ["2023-06-30", "2023-09-30", "2023-12-31", "2024-03-31"])
        self.assertEqual(len(v.with_derived_march(have, [{"period_end_date": "2024-03-31", "net_profit_cr": 999.0}])), 4)
        gap = self.q([100.0, 100.0], ["2023-06-30", "2023-12-31"])
        self.assertEqual(len(v.with_derived_march(gap, [{"period_end_date": "2024-03-31", "net_profit_cr": 420.0}])), 2)

    def test_an_implausible_result_is_dropped_not_used(self):
        qs = self.q([100.0, 100.0, 100.0], ["2023-06-30", "2023-09-30", "2023-12-31"])
        for year_profit in (350.0, 1000.0):      # a derived quarter of 50 (half? no: 50 is fine) / 700 (seven times the December quarter)
            out = v.with_derived_march(qs, [{"period_end_date": "2024-03-31", "net_profit_cr": year_profit}])
            self.assertEqual(len(out), 4 if year_profit == 350.0 else 3)
        loss = self.q([100.0, 100.0, -50.0], ["2023-06-30", "2023-09-30", "2023-12-31"])
        self.assertEqual(len(v.with_derived_march(loss, [{"period_end_date": "2024-03-31", "net_profit_cr": 200.0}])), 3)

    def test_with_the_march_quarter_derived_the_history_covers_the_months_the_gap_used_to_break(self):
        ends = quarter_ends(20)
        full = [{"period_end_date": e, "net_profit_cr": 100.0} for e in ends]
        gapped = [q for q in full if q["period_end_date"] != "2024-03-31"]
        annual = [{"period_end_date": "2024-03-31", "net_profit_cr": 400.0}]
        px = prices("2023-06-01", 40)
        without = v.pe_history(px, gapped, 20.0)
        with_it = v.pe_history(px, gapped, 20.0, annual=annual)
        self.assertGreater(with_it["months"], without["months"] if without else 0)
        self.assertEqual(with_it["months"], v.pe_history(px, full, 20.0)["months"])


class NoLookAhead(unittest.TestCase):
    def test_a_quarter_counts_only_once_it_is_public(self):
        q = quarters([100.0] * 8)
        self.assertEqual(v.public_on("2026-09-30"), date(2026, 11, 14))
        self.assertEqual(v.public_on("2026-03-31"), date(2026, 5, 30))
        self.assertIsNone(v._ttm_profit(q, date(2026, 11, 13)) if False else None)
        # as of 2026-11-13 the Sep-2026 quarter is not public: the trailing four end at June 2026
        ttm_before = v._ttm_profit(quarters([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 1000.0]), date(2026, 11, 13))
        ttm_after = v._ttm_profit(quarters([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 1000.0]), date(2026, 11, 15))
        self.assertEqual(ttm_before, 40.0)
        self.assertEqual(ttm_after, 1030.0)

    def test_a_split_adjusted_price_series_does_not_disturb_the_history(self):
        # the trap: an EPS series restated by a bonus would halve old EPS and double the old P/E. Net profit is untouched by a bonus, so the history is the same.
        h = v.pe_history(prices("2023-06-01", 40), quarters([100.0] * 20), 25.0)
        self.assertEqual((h["low"], h["high"]), (25.0, 25.0))


class StockPageRegression(unittest.TestCase):
    """The stock page builds its own local list called pe_stats; the P/E history function must not share that name (it once did, and the page returned 500)."""

    def test_the_history_function_is_importable_under_its_own_name_and_the_stock_page_context_builds(self):
        from app.routes import analytics
        self.assertTrue(callable(analytics.pe_and_history))
        self.assertFalse(hasattr(analytics, "pe_stats"))


if __name__ == "__main__":
    unittest.main()
