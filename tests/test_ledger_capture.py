"""Trades reach the ledger from imports and from the journal, and leave it again on undo. In-memory database only.
Run: python -m unittest tests.test_ledger_capture -v   (from the repo root)"""

import sqlite3
import unittest

from app import journal, ledger, positions
from app.positions import Txn


def make_db():
    u = sqlite3.connect(":memory:")
    u.row_factory = sqlite3.Row
    from app.db import USERS_SCHEMA
    u.executescript(USERS_SCHEMA)
    u.execute("ALTER TABLE tracked_positions ADD COLUMN closed_at TEXT")    # added by migrations in the real database
    u.execute("ALTER TABLE tracked_positions ADD COLUMN exit_price REAL")
    u.execute("INSERT INTO tracked_positions (id, user_id, symbol, company_name, anchor_type, anchor_date, buy_price, quantity, status) "
              "VALUES (1, 7, 'NSE:TCS', 'TCS', 'position', '2024-01-02', 3000, 10, 'active')")
    u.commit()
    return u


def tx(side, qty, price, date, **kw):
    return Txn("TCS", side, qty, price, date, **kw)


class LedgerFromStatements(unittest.TestCase):
    def test_a_holdings_snapshot_is_one_estimated_opening_lot(self):
        out = positions._ledger_for([tx("holding", 10, 3000, "2025-03-31")], None)
        self.assertEqual(out, [{"side": "buy", "qty": 10, "price": 3000, "date": "2025-03-31", "source": "opening", "estimated": True}])

    def test_only_trades_after_the_snapshot_count(self):
        g = [tx("holding", 10, 3000, "2025-03-31"), tx("buy", 5, 3200, "2025-02-01"), tx("buy", 5, 3300, "2025-05-01"), tx("sell", 3, 3500, "2025-06-01")]
        out = positions._ledger_for(g, None)
        self.assertEqual([(t["side"], t["qty"], t["date"], t["source"]) for t in out],
                         [("buy", 10, "2025-03-31", "opening"), ("buy", 5, "2025-05-01", "import"), ("sell", 3, "2025-06-01", "import")])

    def test_a_trade_statement_keeps_every_dated_trade(self):
        out = positions._ledger_for([tx("buy", 10, 3000, "2024-01-02"), tx("sell", 10, 3600, "2024-09-02")], None)
        self.assertEqual([t["side"] for t in out], ["buy", "sell"])
        self.assertTrue(all(not t["estimated"] for t in out))

    def test_rows_that_cannot_be_placed_in_time_are_left_out(self):
        out = positions._ledger_for([tx("buy", 10, None, "2024-01-02"), tx("buy", 10, 3000, None), tx("buy", 0, 3000, "2024-01-02"), tx("buy", 5, 3000, "2024-01-03")], None)
        self.assertEqual(len(out), 1)

    def test_a_snapshot_with_no_date_uses_the_statement_date_and_ignores_the_trades_like_the_position_does(self):
        out = positions._ledger_for([tx("holding", 10, 3000, None), tx("buy", 5, 3000, "2025-01-01")], "2025-04-01")
        self.assertEqual([(t["source"], t["date"]) for t in out], [("opening", "2025-04-01")])

    def test_the_browser_never_gets_the_trades(self):
        p = {"rows": [{"symbol": "NSE:TCS", "ledger": [{"a": 1}], "quantity": 10}], "skipped": [{"kind": "sold", "ledger": [{"a": 1}], "label": "x"}], "statement_date": None}
        pub = positions._strip_ledger(p)
        self.assertNotIn("ledger", pub["rows"][0])
        self.assertNotIn("ledger", pub["skipped"][0])
        self.assertIn("ledger", p["rows"][0])          # the original is untouched


class WritingAtCommit(unittest.TestCase):
    def setUp(self):
        self.u = make_db()
        self.orig = {"symbol": "NSE:TCS", "quantity": 10.0, "avg_price": 3000.0, "anchor_date": "2025-03-31",
                     "ledger": [{"side": "buy", "qty": 10, "price": 3000, "date": "2025-03-31", "source": "opening", "estimated": True},
                                {"side": "buy", "qty": 5, "price": 3200, "date": "2025-05-01", "source": "import", "estimated": False}]}

    def edited(self, **kw):
        r = {"symbol": "NSE:TCS", "quantity": 10.0, "avg_price": 3000.0, "anchor_date": "2025-03-31"}
        r.update(kw)
        return r

    def test_unchanged_rows_write_their_trades(self):
        positions._write_ledger(self.u, 7, 1, self.edited(), self.orig)
        rows = ledger.load_trades(self.u, 7)
        self.assertEqual([(r["source"], r["qty"], r["estimated"], r["position_id"]) for r in rows], [("opening", 10, 1, 1), ("import", 5, 0, 1)])

    def test_a_row_the_user_corrected_writes_nothing_and_the_position_record_stands_in(self):
        for change in ({"quantity": 12.0}, {"avg_price": 3100.0}, {"anchor_date": "2025-04-05"}, {"avg_price": None}):
            positions._write_ledger(self.u, 7, 1, self.edited(**change), self.orig)
        self.assertEqual(ledger.load_trades(self.u, 7), [])

    def test_importing_the_same_statement_twice_does_not_double_the_ledger(self):
        for _ in range(2):
            positions._write_ledger(self.u, 7, 1, self.edited(), self.orig)
        self.assertEqual(len(ledger.load_trades(self.u, 7)), 2)

    def test_a_newer_snapshot_replaces_the_earlier_opening_lot_but_keeps_real_trades(self):
        positions._write_ledger(self.u, 7, 1, self.edited(), self.orig)
        newer = {"symbol": "NSE:TCS", "quantity": 20.0, "avg_price": 3100.0, "anchor_date": "2025-09-30",
                 "ledger": [{"side": "buy", "qty": 20, "price": 3100, "date": "2025-09-30", "source": "opening", "estimated": True}]}
        positions._write_ledger(self.u, 7, 1, self.edited(quantity=20.0, avg_price=3100.0, anchor_date="2025-09-30"), newer)
        rows = ledger.load_trades(self.u, 7)
        self.assertEqual(sorted((r["source"], r["qty"]) for r in rows), [("import", 5), ("opening", 20)])

    def test_a_row_without_trades_leaves_the_ledger_alone(self):
        positions._write_ledger(self.u, 7, 1, self.edited(), {"symbol": "NSE:TCS", "quantity": 10.0, "avg_price": 3000.0, "anchor_date": "2025-03-31", "ledger": []})
        positions._write_ledger(self.u, 7, 1, self.edited(), None)
        self.assertEqual(ledger.load_trades(self.u, 7), [])


class JournalIsTheLedger(unittest.TestCase):
    def setUp(self):
        self.u = make_db()

    def test_add_trim_and_exit_become_a_buy_and_two_sales(self):
        journal.add_entry(self.u, 7, 1, "add", "2024-06-03", 5, 3200, None)
        journal.add_entry(self.u, 7, 1, "trim", "2024-09-02", 3, 3600, None)
        journal.add_entry(self.u, 7, 1, "exit", "2025-01-15", None, 3900, None)
        rows = ledger.load_trades(self.u, 7)
        self.assertEqual([(r["side"], r["qty"], r["price"], r["source"]) for r in rows], [("buy", 5, 3200, "journal"), ("sell", 3, 3600, "journal"), ("sell", 12, 3900, "journal")])

    def test_a_note_is_not_a_trade(self):
        journal.add_entry(self.u, 7, 1, "note", "2024-06-03", None, None, "thesis: cash flows")
        self.assertEqual(ledger.load_trades(self.u, 7), [])

    def test_undo_removes_the_trade_of_that_entry_only(self):
        journal.add_entry(self.u, 7, 1, "add", "2024-06-03", 5, 3200, None)
        journal.add_entry(self.u, 7, 1, "trim", "2024-09-02", 3, 3600, None)
        journal.undo_last(self.u, 7, 1)
        self.assertEqual([(r["side"], r["qty"]) for r in ledger.load_trades(self.u, 7)], [("buy", 5)])

    def test_the_old_holding_plus_a_journal_add_reconcile_into_one_book(self):
        journal.add_entry(self.u, 7, 1, "add", "2024-06-03", 5, 3200, None)
        pos = dict(self.u.execute("SELECT * FROM tracked_positions WHERE id = 1").fetchone())
        trades, notes = ledger.reconcile(ledger.to_trades(ledger.load_trades(self.u, 7)), pos)
        self.assertEqual(round(sum(t.qty if t.side == "buy" else -t.qty for t in trades)), 15)     # the original 10 (estimated) plus the 5 bought
        self.assertTrue(any(t.estimated for t in trades))


if __name__ == "__main__":
    unittest.main()
