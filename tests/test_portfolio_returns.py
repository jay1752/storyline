"""The returns page's numbers (app/portfolio_returns.py): per holding, per market, realised by year, benchmark on the same dates.
In-memory databases only. Run: python -m unittest tests.test_portfolio_returns -v   (from the repo root)"""

import sqlite3
import unittest
from datetime import date, timedelta

from app import ledger as L
from app import portfolio_returns as PR

TODAY = date(2026, 6, 15)


def monthly(start, months, base, step):
    out, d = [], date.fromisoformat(start)
    for i in range(months):
        out.append((d.isoformat(), round(base + step * i, 4)))
        d = (d.replace(day=1) + timedelta(days=32)).replace(day=1) + timedelta(days=1)   # the 2nd of the next month
    return out


def make_db():
    u = sqlite3.connect(":memory:")
    u.row_factory = sqlite3.Row
    from app.db import USERS_SCHEMA
    u.executescript(USERS_SCHEMA)
    u.execute("ALTER TABLE tracked_positions ADD COLUMN closed_at TEXT")
    u.execute("ALTER TABLE tracked_positions ADD COLUMN exit_price REAL")
    u.execute("ATTACH DATABASE ':memory:' AS market")
    u.execute("CREATE TABLE market.stocks (symbol TEXT, name TEXT)")
    u.execute("CREATE TABLE market.price_history (symbol TEXT, date TEXT, close REAL)")
    u.execute("CREATE TABLE market.index_prices (symbol TEXT, date TEXT, close REAL)")
    return u


def prices(u, symbol, series):
    u.execute("INSERT OR IGNORE INTO market.stocks VALUES (?, ?)", (symbol, symbol + " Ltd"))
    u.executemany("INSERT INTO market.price_history VALUES (?,?,?)", [(symbol, d, c) for d, c in series])


def index(u, symbol, series):
    u.executemany("INSERT INTO market.index_prices VALUES (?,?,?)", [(symbol, d, c) for d, c in series])


def position(u, pid, symbol, qty, price, anchor, kind="position", status="active", **kw):
    u.execute("INSERT INTO tracked_positions (id, user_id, symbol, company_name, anchor_type, anchor_date, buy_price, quantity, status, closed_at, exit_price) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (pid, 7, symbol, symbol + " Ltd", kind, anchor, price, qty, status, kw.get("closed_at"), kw.get("exit_price")))


class OneHolding(unittest.TestCase):
    def setUp(self):
        self.u = make_db()
        # price doubles over 30 months: 100 -> 200 on the 2nd of each month from 2024-01-02
        prices(self.u, "NSE:AAA", monthly("2024-01-02", 31, 100.0, 100 / 30))
        index(self.u, "^NSEI", monthly("2024-01-02", 31, 20000.0, 20000 * 0.5 / 30))     # +50% over the same period
        position(self.u, 1, "NSE:AAA", 10, 100.0, "2024-01-02")
        self.u.commit()

    def test_a_position_with_no_stored_trades_is_an_estimated_opening_lot(self):
        a = PR.assemble(self.u, 7, TODAY)
        h = a["holdings"][0]
        self.assertTrue(h["summary"]["has_estimated"])
        self.assertTrue(any("recorded average cost" in n for n in h["notes"]))
        b = a["books"]["IN"]
        self.assertEqual((b["invested"], round(b["value"]), round(b["total_gain"])), (1000.0, 2000, 1000))
        self.assertEqual(round(b["gain_on_cost_pct"]), 100)

    def test_xirr_is_the_annualised_rate_and_the_index_is_measured_on_the_same_dates(self):
        b = PR.assemble(self.u, 7, TODAY)["books"]["IN"]
        self.assertAlmostEqual(b["xirr"], 0.32, delta=0.02)            # +100% over 2.5 years
        bm = b["benchmark"]
        self.assertEqual(bm["name"], "Nifty 50")
        self.assertAlmostEqual(bm["bench_value"], 1500.0, delta=1.0)    # the same 1000 in an index that rose 50%
        self.assertGreater(bm["own_value"], bm["bench_value"])
        self.assertAlmostEqual(bm["bench_xirr"], 0.18, delta=0.02)

    def test_a_stale_index_is_flagged(self):
        self.assertTrue(PR.assemble(self.u, 7, date(2027, 1, 1))["books"]["IN"]["benchmark"]["stale"])
        self.assertFalse(PR.assemble(self.u, 7, date(2026, 7, 5))["books"]["IN"]["benchmark"]["stale"])

    def test_no_index_data_means_no_benchmark_not_an_error(self):
        self.u.execute("DELETE FROM market.index_prices")
        self.assertIsNone(PR.assemble(self.u, 7, TODAY)["books"]["IN"]["benchmark"])


class SalesAndYears(unittest.TestCase):
    def setUp(self):
        self.u = make_db()
        prices(self.u, "NSE:AAA", monthly("2023-01-02", 40, 100.0, 5.0))
        prices(self.u, "NSE:OLD", monthly("2023-01-02", 40, 50.0, 1.0))
        position(self.u, 1, "NSE:AAA", 6, 100.0, "2023-01-02")          # 10 bought, 4 sold since: 6 held at the same average
        # a stock sold out earlier, with no tracked position at all: only its trades exist
        L.add_trade(self.u, 7, "NSE:OLD", "buy", 100, 50.0, "2023-02-02", "import")
        L.add_trade(self.u, 7, "NSE:OLD", "sell", 100, 80.0, "2024-08-02", "import")
        L.add_trade(self.u, 7, "NSE:AAA", "sell", 4, 150.0, "2025-05-02", "journal", ref="1")
        self.u.commit()

    def test_a_stock_with_trades_but_no_position_still_counts(self):
        a = PR.assemble(self.u, 7, TODAY)
        old = next(h for h in a["holdings"] if h["symbol"] == "NSE:OLD")
        self.assertTrue(old["closed"])
        self.assertEqual(round(old["summary"]["realised"]), 3000)
        self.assertFalse(old["tracked"])

    def test_realised_gains_are_grouped_by_financial_year_and_term(self):
        b = PR.assemble(self.u, 7, TODAY)["books"]["IN"]
        years = {y["fy"]: y for y in b["by_year"]}
        self.assertEqual(round(years["FY2024-25"]["long"]), 3000)           # OLD: bought Feb 2023, sold Aug 2024 (18 months)
        self.assertEqual(round(years["FY2025-26"]["long"]), 200)            # AAA: 4 shares bought Jan 2023 at 100, sold May 2025 at 150 (>12 months)
        self.assertEqual(round(b["realised"]), 3200)

    def test_only_the_open_shares_are_valued(self):
        a = PR.assemble(self.u, 7, TODAY)
        aaa = next(h for h in a["holdings"] if h["symbol"] == "NSE:AAA")
        self.assertEqual(round(aaa["summary"]["open_qty"]), 6)


class Markets(unittest.TestCase):
    def test_india_and_us_are_separate_books_in_their_own_currency(self):
        u = make_db()
        prices(u, "NSE:AAA", monthly("2024-01-02", 20, 100.0, 1.0))
        prices(u, "NASDAQ:BBB", monthly("2024-01-02", 20, 50.0, 1.0))
        position(u, 1, "NSE:AAA", 10, 100.0, "2024-01-02")
        position(u, 2, "NASDAQ:BBB", 5, 50.0, "2024-01-02")
        u.commit()
        books = PR.assemble(u, 7, TODAY)["books"]
        self.assertEqual({k: v["cur"] for k, v in books.items()}, {"IN": "₹", "US": "$"})
        self.assertEqual((books["IN"]["invested"], books["US"]["invested"]), (1000.0, 250.0))

    def test_a_watchlist_stock_is_not_a_holding_and_an_unpriced_one_is_named(self):
        u = make_db()
        prices(u, "NSE:AAA", monthly("2024-01-02", 20, 100.0, 1.0))
        position(u, 1, "NSE:AAA", None, None, "2024-01-02", kind="watchlist")
        position(u, 2, "NSE:NOPRICE", 10, 100.0, "2024-01-02")
        u.commit()
        a = PR.assemble(u, 7, TODAY)
        self.assertEqual([h["symbol"] for h in a["holdings"]], ["NSE:NOPRICE"])
        self.assertEqual(a["books"]["IN"]["unpriced"], ["NSE:NOPRICE"])

    def test_one_users_trades_never_reach_another(self):
        u = make_db()
        prices(u, "NSE:AAA", monthly("2024-01-02", 20, 100.0, 1.0))
        L.add_trade(u, 8, "NSE:AAA", "buy", 10, 100.0, "2024-01-02", "import")
        u.commit()
        self.assertEqual(PR.assemble(u, 7, TODAY)["holdings"], [])


class SplitHints(unittest.TestCase):
    def test_a_purchase_priced_double_the_adjusted_price_is_flagged(self):
        u = make_db()
        prices(u, "NSE:AAA", monthly("2024-01-02", 20, 100.0, 1.0))           # adjusted: about 100 in January 2024
        L.add_trade(u, 7, "NSE:AAA", "buy", 10, 205.0, "2024-01-02", "import")
        position(u, 1, "NSE:AAA", 10, 205.0, "2024-01-02")
        u.commit()
        h = PR.assemble(u, 7, TODAY)["holdings"][0]
        self.assertIn("about 2x", h["split_hint"])

    def test_an_ordinary_purchase_is_not(self):
        u = make_db()
        prices(u, "NSE:AAA", monthly("2024-01-02", 20, 100.0, 1.0))
        L.add_trade(u, 7, "NSE:AAA", "buy", 10, 103.0, "2024-01-02", "import")
        position(u, 1, "NSE:AAA", 10, 103.0, "2024-01-02")
        u.commit()
        self.assertIsNone(PR.assemble(u, 7, TODAY)["holdings"][0]["split_hint"])


if __name__ == "__main__":
    unittest.main()
