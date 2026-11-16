"""The portfolio ledger maths and storage (app/ledger.py). Pure functions plus an in-memory database. Run: python -m unittest tests.test_ledger -v"""

import sqlite3
import unittest

from app import ledger as L
from app.ledger import Trade


def T(d, side, qty, price, **kw):
    return Trade(d, side, qty, price, **kw)


class Lots(unittest.TestCase):
    def test_a_sale_realises_the_oldest_shares_first_and_splits_term_by_holding_period(self):
        f = L.fifo([T("2024-01-01", "buy", 10, 100), T("2024-06-01", "buy", 10, 120), T("2025-02-01", "sell", 15, 150)])
        legs = f["realised"]
        self.assertEqual([(r["qty"], round(r["gain"]), r["term"]) for r in legs], [(10, 500, "long"), (5, 150, "short")])
        self.assertEqual((f["open_qty"], f["open_cost"], f["avg_cost"]), (5, 600, 120))
        self.assertEqual(round(f["realised_gain"]), 650)

    def test_selling_more_than_was_bought_is_reported_not_hidden(self):
        f = L.fifo([T("2024-01-01", "buy", 10, 100), T("2024-02-01", "sell", 15, 120)])
        self.assertEqual(f["unmatched_sell_qty"], 5)
        self.assertEqual(f["open_qty"], 0)

    def test_a_days_buys_come_before_its_sells(self):
        f = L.fifo([T("2024-03-01", "sell", 10, 110), T("2024-03-01", "buy", 10, 100)])
        self.assertEqual(f["unmatched_sell_qty"], 0)
        self.assertEqual(round(f["realised_gain"]), 100)

    def test_us_shares_become_long_term_after_24_months_india_after_12(self):
        tr = [T("2023-01-01", "buy", 1, 100), T("2024-06-01", "sell", 1, 150)]    # 517 days
        self.assertEqual(L.fifo(tr, "IN")["realised"][0]["term"], "long")
        self.assertEqual(L.fifo(tr, "US")["realised"][0]["term"], "short")

    def test_exactly_a_year_is_still_short(self):
        self.assertEqual(L.fifo([T("2024-01-01", "buy", 1, 100), T("2025-01-01", "sell", 1, 120)])["realised"][0]["term"], "long")   # 366 days (leap year)
        self.assertEqual(L.fifo([T("2023-01-01", "buy", 1, 100), T("2024-01-01", "sell", 1, 120)])["realised"][0]["term"], "short")  # exactly 365

    def test_financial_year_runs_april_to_march(self):
        self.assertEqual(L.financial_year("2025-03-31"), "FY2024-25")
        self.assertEqual(L.financial_year("2025-04-01"), "FY2025-26")
        self.assertEqual(L.financial_year("2099-12-31"), "FY2099-00")

    def test_gains_are_grouped_by_year_and_term_with_losses_netted(self):
        f = L.fifo([T("2023-01-01", "buy", 10, 100), T("2023-02-01", "buy", 10, 100), T("2025-05-01", "sell", 10, 130), T("2025-06-01", "sell", 10, 80)])
        y = L.realised_by_year(f["realised"])
        self.assertEqual(len(y), 1)
        self.assertEqual((y[0]["fy"], round(y[0]["long"]), round(y[0]["short"]), round(y[0]["total"])), ("FY2025-26", 100, 0, 100))


class Xirr(unittest.TestCase):
    def test_two_years_of_ten_percent(self):
        r = L.xirr([("2024-01-01", -100), ("2026-01-01", 121)])
        self.assertAlmostEqual(r, 0.10, places=2)

    def test_a_loss_is_negative(self):
        self.assertAlmostEqual(L.xirr([("2024-01-01", -100), ("2026-01-01", 81)]), -0.10, places=2)

    def test_it_solves_for_several_flows(self):
        flows = [("2023-01-01", -100), ("2024-01-01", -100), ("2025-01-01", 250)]
        r = L.xirr(flows)
        t0 = L._d("2023-01-01")
        npv = sum(a / (1 + r) ** ((L._d(d) - t0).days / 365) for d, a in flows)
        self.assertLess(abs(npv), 1e-4)

    def test_under_a_year_is_not_annualised(self):
        self.assertIsNone(L.xirr([("2025-01-01", -100), ("2025-09-01", 130)]))

    def test_one_sided_or_empty_flows_have_no_xirr(self):
        self.assertIsNone(L.xirr([("2024-01-01", -100), ("2026-01-01", -50)]))
        self.assertIsNone(L.xirr([]))


class Summaries(unittest.TestCase):
    def test_unrealised_and_gain_on_cost_for_a_holding(self):
        s = L.summarise([T("2024-01-01", "buy", 10, 100)], 150, "2024-06-01")
        self.assertEqual((s["value"], s["unrealised"], s["realised"], s["total_gain"]), (1500, 500, 0, 500))
        self.assertEqual(round(s["gain_on_cost_pct"]), 50)
        self.assertIsNone(s["xirr"])                      # five months: not annualised

    def test_xirr_appears_once_the_money_has_been_in_a_year(self):
        s = L.summarise([T("2024-01-01", "buy", 10, 100)], 121, "2026-01-01")
        self.assertAlmostEqual(s["xirr"], 0.10, places=2)

    def test_a_sold_out_holding_reports_realised_only(self):
        s = L.summarise([T("2023-01-01", "buy", 10, 100), T("2025-01-01", "sell", 10, 150)], 200, "2026-01-01")
        self.assertEqual((s["open_qty"], s["unrealised"], s["realised"], s["total_gain"]), (0, 0, 500, 500))

    def test_without_a_price_an_open_holding_has_no_total(self):
        s = L.summarise([T("2024-01-01", "buy", 10, 100)], None, "2025-01-01")
        self.assertIsNone(s["total_gain"])
        self.assertIsNone(s["value"])

    def test_estimated_lots_are_flagged(self):
        self.assertTrue(L.summarise([T("2024-01-01", "buy", 1, 100, estimated=True)], 110, "2025-01-01")["has_estimated"])


class Benchmark(unittest.TestCase):
    def idx(self, table):
        return lambda d: table.get(d)

    def test_the_same_money_in_an_index_that_grew_ten_percent_a_year(self):
        flows = [("2024-01-01", -100.0)]
        b = L.benchmark(flows, self.idx({"2024-01-01": 100.0}), 121.0, "2026-01-01")
        self.assertAlmostEqual(b["value"], 121.0)
        self.assertAlmostEqual(b["xirr"], 0.10, places=2)

    def test_a_sale_takes_units_out_of_the_benchmark_too(self):
        flows = [("2024-01-01", -200.0), ("2025-01-01", 110.0)]
        b = L.benchmark(flows, self.idx({"2024-01-01": 100.0, "2025-01-01": 110.0}), 121.0, "2026-01-01")
        self.assertAlmostEqual(b["units"], 2.0 - 1.0)
        self.assertAlmostEqual(b["value"], 121.0)

    def test_no_benchmark_when_the_index_cannot_price_a_date_or_nothing_is_left(self):
        self.assertIsNone(L.benchmark([("2024-01-01", -100.0)], self.idx({}), 121.0, "2026-01-01"))
        self.assertIsNone(L.benchmark([("2024-01-01", -100.0), ("2025-01-01", 150.0)], self.idx({"2024-01-01": 100.0, "2025-01-01": 100.0}), 121.0, "2026-01-01"))
        self.assertIsNone(L.benchmark([], self.idx({}), 121.0, "2026-01-01"))


class Splits(unittest.TestCase):
    def test_a_price_about_double_the_adjusted_one_hints_at_a_one_for_one_bonus(self):
        self.assertEqual(L.split_suspect(200, 100), 2.0)
        self.assertEqual(L.split_suspect(1010, 100), 10.0)

    def test_an_ordinary_price_gap_is_not_a_hint(self):
        self.assertIsNone(L.split_suspect(108, 100))
        self.assertIsNone(L.split_suspect(150, 100))
        self.assertIsNone(L.split_suspect(100, None))
        self.assertIsNone(L.split_suspect(None, 100))


class Reconcile(unittest.TestCase):
    def pos(self, **kw):
        base = {"quantity": 10.0, "buy_price": 110.0, "anchor_date": "2024-01-01", "status": "active"}
        base.update(kw)
        return base

    def test_a_position_with_no_stored_trades_becomes_one_estimated_opening_lot(self):
        tr, notes = L.reconcile([], self.pos())
        self.assertEqual([(t.side, t.qty, t.price, t.estimated, t.date) for t in tr], [("buy", 10.0, 110.0, True, "2024-01-01")])
        self.assertTrue(notes)

    def test_shares_the_trades_do_not_cover_are_added_at_the_price_that_keeps_the_recorded_average(self):
        tr, _ = L.reconcile([T("2024-03-01", "buy", 6, 100)], self.pos())
        gap = [t for t in tr if t.estimated][0]
        self.assertEqual(gap.qty, 4)
        self.assertAlmostEqual(gap.price, (10 * 110 - 6 * 100) / 4)
        self.assertEqual(round(sum(t.qty * t.price for t in tr)), 1100)

    def test_after_a_recorded_sale_the_opening_lot_keeps_the_positions_average_cost(self):
        # 10 shares at 110, then 4 sold at 150: the position now records 6 shares at the same average, 110
        tr, _ = L.reconcile([T("2024-09-01", "sell", 4, 150)], self.pos(quantity=6.0, buy_price=110.0))
        opening = [t for t in tr if t.estimated][0]
        self.assertEqual((opening.qty, round(opening.price, 4)), (10.0, 110.0))
        f = L.fifo(tr)
        self.assertEqual((f["open_qty"], round(f["open_cost"])), (6, 660))
        self.assertEqual(round(f["realised_gain"]), 160)           # 4 shares: sold at 150, cost 110

    def test_trades_that_cover_the_position_add_nothing(self):
        tr, notes = L.reconcile([T("2024-03-01", "buy", 10, 110)], self.pos())
        self.assertEqual(len(tr), 1)
        self.assertEqual(notes, [])

    def test_trades_that_add_up_to_more_than_the_position_are_flagged(self):
        _, notes = L.reconcile([T("2024-03-01", "buy", 12, 110)], self.pos())
        self.assertTrue(any("add up to 12" in n for n in notes))

    def test_a_closed_position_without_trades_becomes_an_estimated_buy_and_sale(self):
        tr, _ = L.reconcile([], self.pos(status="exited", closed_at="2025-02-01", exit_price=150.0))
        self.assertEqual([(t.side, t.price) for t in tr], [("buy", 110.0), ("sell", 150.0)])
        self.assertTrue(all(t.estimated for t in tr))

    def test_a_watchlist_row_has_nothing_to_reconcile(self):
        tr, _ = L.reconcile([], {"quantity": None, "buy_price": None, "anchor_date": "2024-01-01", "status": "active"})
        self.assertEqual(tr, [])


class Storage(unittest.TestCase):
    def setUp(self):
        self.u = sqlite3.connect(":memory:")
        self.u.row_factory = sqlite3.Row
        from app.db import USERS_SCHEMA
        self.u.executescript(USERS_SCHEMA)

    def test_the_same_trade_imported_twice_is_stored_once(self):
        a = L.add_trade(self.u, 7, "NSE:TCS", "buy", 10, 3500, "2024-05-02", "import")
        b = L.add_trade(self.u, 7, "NSE:TCS", "buy", 10, 3500, "2024-05-02", "import")
        self.assertIsNotNone(a)
        self.assertIsNone(b)
        self.assertEqual(len(L.load_trades(self.u, 7)), 1)

    def test_two_real_trades_that_look_alike_from_different_sources_both_count(self):
        L.add_trade(self.u, 7, "NSE:TCS", "buy", 10, 3500, "2024-05-02", "import")
        L.add_trade(self.u, 7, "NSE:TCS", "buy", 10, 3500, "2024-05-02", "journal", ref="5")
        self.assertEqual(len(L.load_trades(self.u, 7)), 2)

    def test_bad_input_is_refused_and_one_users_trades_stay_theirs(self):
        for bad in (("hold", 10, 100), ("buy", 0, 100), ("buy", 10, 0), ("buy", -1, 100), ("buy", 10, None)):
            self.assertIsNone(L.add_trade(self.u, 7, "NSE:TCS", bad[0], bad[1], bad[2], "2024-01-01", "manual"))
        L.add_trade(self.u, 8, "NSE:TCS", "buy", 1, 10, "2024-01-01", "manual")
        self.assertEqual(L.load_trades(self.u, 7), [])
        self.assertEqual(len(L.load_trades(self.u, 8, "NSE:TCS")), 1)

    def test_undoing_a_journal_entry_removes_its_trade_only(self):
        L.add_trade(self.u, 7, "NSE:TCS", "buy", 5, 3600, "2024-06-01", "journal", ref="12")
        L.add_trade(self.u, 7, "NSE:TCS", "buy", 10, 3500, "2024-05-02", "import")
        self.assertEqual(L.delete_for_ref(self.u, 7, "journal", "12"), 1)
        self.assertEqual([r["source"] for r in L.load_trades(self.u, 7)], ["import"])

    def test_rows_convert_to_trades(self):
        L.add_trade(self.u, 7, "NSE:TCS", "sell", 3, 4000, "2024-09-01", "journal", estimated=True)
        t = L.to_trades(L.load_trades(self.u, 7))[0]
        self.assertEqual((t.side, t.qty, t.price, t.estimated, t.source), ("sell", 3, 4000, True, "journal"))


if __name__ == "__main__":
    unittest.main()
