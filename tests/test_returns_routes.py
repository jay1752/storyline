"""The returns page and its forms, through the real app with a fake signed-in user and in-memory data.
Run: python -m unittest tests.test_returns_routes -v   (from the repo root)"""

import sqlite3
import unittest
from collections import defaultdict
from unittest import mock

from starlette.testclient import TestClient

from app import ledger as L
from tests.test_portfolio_returns import index, monthly, position, prices


def make_db():
    u = sqlite3.connect(":memory:", check_same_thread=False)
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


class ReturnsPage(unittest.TestCase):
    def setUp(self):
        self.u = make_db()
        prices(self.u, "NSE:AAA", monthly("2024-01-02", 31, 100.0, 100 / 30))
        prices(self.u, "NSE:GONE", monthly("2024-01-02", 31, 50.0, 1.0))
        index(self.u, "^NSEI", monthly("2024-01-02", 31, 20000.0, 333.0))
        position(self.u, 1, "NSE:AAA", 10, 100.0, "2024-01-02")
        L.add_trade(self.u, 7, "NSE:AAA", "buy", 1, 101.0, "2024-02-02", "journal", ref="9")
        L.add_trade(self.u, 7, "NSE:GONE", "buy", 10, 50.0, "2024-01-05", "import")
        self.u.commit()
        user = defaultdict(lambda: None, id=7, email="t@example.com", is_platform_admin=0)
        from app.main import app
        self.client = TestClient(app, follow_redirects=False)
        for p in (mock.patch("app.auth.middleware.get_session_user", return_value=user),
                  mock.patch("app.routes.returns.get_users_conn", return_value=self.u)):
            p.start()
            self.addCleanup(p.stop)
        self.client.cookies.set("session_id", "x")

    def test_the_page_shows_the_book_the_benchmark_and_the_disclaimer(self):
        r = self.client.get("/portfolio/returns")
        self.assertEqual(r.status_code, 200)
        for text in ("India holdings", "Annualised return (XIRR)", "Nifty 50", "not an investment adviser or a tax adviser", "Brokerage, taxes and dividends are not included"):
            self.assertIn(text, r.text)

    def test_realised_gains_by_year_and_the_tax_note_appear_once_there_is_a_sale(self):
        L.add_trade(self.u, 7, "NSE:GONE", "sell", 10, 70.0, "2025-02-10", "manual")
        self.u.commit()
        t = self.client.get("/portfolio/returns").text
        for text in ("Realised gains by financial year", "FY2024-25", "not tax advice and not a tax calculation", "more than 12 months"):
            self.assertIn(text, t)

    def test_a_stock_held_is_added_through_its_journal_not_here(self):
        r = self.client.post("/portfolio/returns/trade", data={"symbol": "NSE:AAA", "side": "buy", "when": "2024-05-01", "qty": "1", "price": "100"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("error=", r.headers["location"])
        self.assertEqual(len(L.load_trades(self.u, 7, "NSE:AAA")), 1)

    def test_a_past_trade_of_a_stock_not_held_is_recorded(self):
        r = self.client.post("/portfolio/returns/trade", data={"symbol": "NSE:GONE", "side": "sell", "when": "2025-01-10", "qty": "10", "price": "70"})
        self.assertIn("saved=1", r.headers["location"])
        self.assertEqual([(t["side"], t["source"]) for t in L.load_trades(self.u, 7, "NSE:GONE")], [("buy", "import"), ("sell", "manual")])
        self.assertIn("NSE:GONE", self.client.get("/portfolio/returns").text)

    def test_bad_input_is_refused_with_a_message(self):
        for data in ({"symbol": "NSE:GONE", "side": "hold", "when": "2025-01-10", "qty": "1", "price": "1"},
                     {"symbol": "NSE:GONE", "side": "buy", "when": "2999-01-01", "qty": "1", "price": "1"},
                     {"symbol": "NSE:GONE", "side": "buy", "when": "2025-01-10", "qty": "-1", "price": "1"},
                     {"symbol": "NSE:GONE", "side": "buy", "when": "not a date", "qty": "1", "price": "1"},
                     {"symbol": "NSE:UNKNOWN", "side": "buy", "when": "2025-01-10", "qty": "1", "price": "1"}):
            r = self.client.post("/portfolio/returns/trade", data=data)
            self.assertIn("error=", r.headers["location"], data)
        self.assertEqual(len(L.load_trades(self.u, 7, "NSE:GONE")), 1)

    def test_deleting_a_wrong_trade_works_but_a_journal_trade_must_be_undone_in_the_journal(self):
        mine = next(t for t in L.load_trades(self.u, 7) if t["source"] == "import")
        journal = next(t for t in L.load_trades(self.u, 7) if t["source"] == "journal")
        self.assertIn("saved=1", self.client.post("/portfolio/returns/trade/delete", data={"trade_id": mine["id"]}).headers["location"])
        self.assertIn("error=", self.client.post("/portfolio/returns/trade/delete", data={"trade_id": journal["id"]}).headers["location"])
        self.assertEqual([t["source"] for t in L.load_trades(self.u, 7)], ["journal"])

    def test_nobody_can_delete_another_users_trade(self):
        L.add_trade(self.u, 8, "NSE:GONE", "buy", 5, 40.0, "2024-01-05", "import")
        self.u.commit()
        other = next(t for t in L.load_trades(self.u, 8))
        self.assertIn("error=", self.client.post("/portfolio/returns/trade/delete", data={"trade_id": other["id"]}).headers["location"])
        self.assertEqual(len(L.load_trades(self.u, 8)), 1)

    def test_the_exports_are_csv_and_do_not_run_as_formulas(self):
        L.add_trade(self.u, 7, "=cmd|' /C calc'!A0", "buy", 1, 10.0, "2024-01-05", "manual")
        self.u.commit()
        r = self.client.get("/portfolio/returns/export/trades.csv")
        self.assertEqual(r.headers["content-type"].split(";")[0], "text/csv")
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertIn("date,symbol,side,quantity,price,source,estimated", r.text)
        self.assertNotIn(",=cmd", r.text)
        self.assertIn("'=cmd", r.text)
        self.assertIn("financial_year", self.client.get("/portfolio/returns/export/realised.csv").text)

    def test_a_signed_out_visitor_is_sent_to_login(self):
        from app.main import app
        r = TestClient(app, follow_redirects=False).get("/portfolio/returns")
        self.assertEqual(r.status_code, 303)
        self.assertIn("/login", r.headers["location"])


if __name__ == "__main__":
    unittest.main()
