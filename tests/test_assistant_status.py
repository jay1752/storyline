"""The chat assistant reads the status computed in code (app/ai/status.py), not the story model's stored signal.
In-memory databases only. Run: python -m unittest tests.test_assistant_status -v   (from the repo root)"""

import json
import sqlite3
import unittest
from contextlib import contextmanager
from unittest import mock

from app.ai import agent as agent_mod, page_context, status as st
from app.auth.middleware import current_user_id


def make_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    from app.db import USERS_SCHEMA
    c.executescript(USERS_SCHEMA)
    c.execute("ALTER TABLE position_snapshots ADD COLUMN analysis_json TEXT")   # added by a migration in the real database
    for pid, sym, kind in ((1, "NSE:AAA", "position"), (2, "NSE:BBB", "watchlist")):
        c.execute("INSERT INTO tracked_positions (id, user_id, symbol, company_name, anchor_type, anchor_date, buy_price, quantity, status) VALUES (?, 7, ?, ?, ?, '2026-01-01', 100, 10, 'active')",
                  (pid, sym, sym + " Ltd", kind))
        # the story model's stored read said "watch" (shown as "Not yet meeting your rules") on both
        c.execute("INSERT INTO position_snapshots (position_id, price_at_check, since_anchor_pct, valuation_label, signal, reasoning_json, trigger_type, analysis_json, checked_at) "
                  "VALUES (?, 100, 5, 'Reserve', 'watch', '[]', 'interval', ?, '2026-11-10 03:00:00')",
                  (pid, json.dumps({"verdict": "WATCH", "bucket": "Reserve", "what_changed": "Sales growth slowed.", "story_flags": [], "generated_at": "2026-11-10T03:00:00"})))
    c.commit()
    return c


def result(code, label, text):
    return {"code": code, "label": label, "reasons": [{"kind": "data", "text": text}, {"kind": "note", "text": "an annual-figure note"}], "enough_data": True, "owned": True,
            "rules": {"judged": 5, "passing": 3, "failing": [], "unknown": []}, "speed1": {"status": "clear", "note": None}, "speed2": {"status": "clear", "items": []},
            "weight_pct": None, "sector_pct": None}


class AssistantReadsTheStoredStatus(unittest.TestCase):
    def setUp(self):
        self.u = make_db()
        st.record(self.u, 1, 7, result("WATCH", "Not enough data", "Only 5 of your rules could be judged with the data we hold."))
        self.u.commit()      # position 2 has no stored status yet
        self.market = sqlite3.connect(":memory:", check_same_thread=False)
        self.market.row_factory = sqlite3.Row
        self.market.execute("CREATE TABLE price_history (symbol TEXT, date TEXT, close REAL)")
        self.market.execute("INSERT INTO price_history VALUES ('NSE:AAA', '2026-11-13', 110), ('NSE:BBB', '2026-11-13', 90)")

        @contextmanager
        def market():
            yield self.market

        for p in (mock.patch.object(agent_mod, "get_users_conn", return_value=self.u),
                  mock.patch.object(page_context, "get_users_conn", return_value=self.u),
                  mock.patch.object(page_context, "get_market_conn", market)):
            p.start()
            self.addCleanup(p.stop)
        self.tok = current_user_id.set(7)
        self.addCleanup(lambda: current_user_id.reset(self.tok))

    def test_the_portfolio_tool_reports_the_code_status_with_its_reason(self):
        rows = {r["symbol"]: r for r in agent_mod.get_portfolio_or_watchlist()}
        a = rows["NSE:AAA"]
        self.assertEqual(a["status_vs_your_rules"], "Not enough data")          # not the old signal's "Not yet meeting your rules"
        self.assertIn("Only 5 of your rules", a["status_detail"])
        self.assertNotIn("annual-figure note", a["status_detail"])
        self.assertNotIn("position_id", a)
        self.assertNotIn("signal", a)

    def test_before_the_first_computation_it_falls_back_to_the_stored_signal(self):
        b = {r["symbol"]: r for r in agent_mod.get_portfolio_or_watchlist()}["NSE:BBB"]
        self.assertEqual(b["status_vs_your_rules"], "Not yet meeting your rules")
        self.assertIsNone(b["status_detail"])

    def test_the_management_digest_leads_with_the_status_and_keeps_the_story_apart(self):
        text = page_context.management_digest(7)
        line = next(x for x in text.split("\n") if "NSE:AAA" in x)
        self.assertIn("STATUS: Not enough data (since", line)
        self.assertIn("written story (Reserve, 2026-11-10)", line)
        self.assertNotIn("story status vs the user's rules: Not yet meeting your rules", line)
        self.assertIn("Sales growth slowed.", line)
        other = next(x for x in text.split("\n") if "NSE:BBB" in x)
        self.assertIn("story status vs the user's rules: Not yet meeting your rules", other)   # no stored status yet: the old read, labelled as the story's

    def test_a_stock_with_a_status_but_no_story_says_so(self):
        st.record(self.u, 2, 7, result("HOLD", "Within your rules", "Not met: ROCE 10% against your rule > 25%."))
        self.u.execute("UPDATE position_snapshots SET analysis_json = NULL WHERE position_id = 2")
        self.u.commit()
        line = next(x for x in page_context.management_digest(7).split("\n") if "NSE:BBB" in x)
        self.assertIn("STATUS: Within your rules", line)
        self.assertIn("no written story yet", line)

    def test_one_line_is_short_and_drops_notes(self):
        s = st.stored(self.u, 1)
        self.assertTrue(st.one_line(s).startswith("Not enough data (since "))
        long = dict(s, reasons=[{"kind": "data", "text": "x" * 500}])
        self.assertLessEqual(len(st.one_line(long)), 240)
        self.assertIsNone(st.one_line(None))

    def test_the_assistant_is_told_the_status_is_decided_by_code(self):
        p = agent_mod.SYSTEM_PROMPT
        self.assertIn("decided by code", p)
        self.assertIn("Not enough data", p)
        self.assertIn("never as an instruction", p)


if __name__ == "__main__":
    unittest.main()
