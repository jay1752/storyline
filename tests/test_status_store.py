"""History storage of the status and the stale-run expiry. In-memory databases only.
Run: python -m unittest tests.test_status_store -v   (from the repo root)"""

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.ai import status as st


def users_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    from app.db import USERS_SCHEMA
    c.executescript(USERS_SCHEMA)
    return c


def result(code="HOLD", label="Within your rules", weight=4.0, s2=None):
    return {"code": code, "label": label, "reasons": [{"kind": "entry", "text": f"weight {weight}"}], "enough_data": True, "owned": True,
            "rules": {"judged": 8, "passing": 7, "failing": ["x"], "unknown": []}, "speed1": {"status": "clear", "note": None},
            "speed2": {"status": "clear", "items": s2 or []}, "weight_pct": weight, "sector_pct": 10.0}


class History(unittest.TestCase):
    def setUp(self):
        self.u = users_db()

    def rows(self):
        return self.u.execute("SELECT code, label FROM position_status_history ORDER BY id").fetchall()

    def test_first_status_adds_a_row(self):
        self.assertTrue(st.record(self.u, 1, 7, result()))
        self.assertEqual([(r["code"], r["label"]) for r in self.rows()], [("HOLD", "Within your rules")])

    def test_same_status_with_moved_numbers_refreshes_the_row_not_a_new_one(self):
        st.record(self.u, 1, 7, result(weight=4.0))
        self.assertFalse(st.record(self.u, 1, 7, result(weight=4.1)))
        self.assertEqual(len(self.rows()), 1)
        self.assertIn("4.1", st.stored(self.u, 1)["reasons"][0]["text"])

    def test_a_change_of_status_adds_a_row_and_history_is_newest_first(self):
        st.record(self.u, 1, 7, result())
        self.assertTrue(st.record(self.u, 1, 7, result("TRIM", "Trim rule triggered")))
        self.assertEqual([h["label"] for h in st.history(self.u, 1)], ["Trim rule triggered", "Within your rules"])
        self.assertEqual(st.stored(self.u, 1)["code"], "TRIM")

    def test_a_new_exit_rule_under_the_same_code_is_a_change(self):
        a = result("EXIT", "Exit rule triggered", s2=[{"rule": "ROCE", "state": "confirmed", "note": ""}])
        b = result("EXIT", "Exit rule triggered", s2=[{"rule": "ROCE", "state": "confirmed", "note": ""}, {"rule": "Sales growth negative", "state": "confirmed", "note": ""}])
        st.record(self.u, 1, 7, a)
        self.assertTrue(st.record(self.u, 1, 7, b))

    def test_stored_many_returns_each_positions_latest_for_one_user_only(self):
        st.record(self.u, 1, 7, result())
        st.record(self.u, 1, 7, result("TRIM", "Trim rule triggered"))
        st.record(self.u, 2, 7, result("EXIT", "Exit rule triggered"))
        st.record(self.u, 3, 8, result())
        m = st.stored_many(self.u, 7)
        self.assertEqual({k: v["code"] for k, v in m.items()}, {1: "TRIM", 2: "EXIT"})


class StaleRuns(unittest.TestCase):
    def test_runs_older_than_three_hours_are_closed_as_failed_and_recent_ones_are_left(self):
        from app.ai import insights_engine as ie
        c = users_db()
        now = datetime.now(timezone.utc)
        fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT INTO insight_runs (position_id, user_id, trigger, status, started_at) VALUES (1, 7, 'daily', 'running', ?)", (fmt(now - timedelta(days=39)),))
        c.execute("INSERT INTO insight_runs (position_id, user_id, trigger, status, started_at) VALUES (2, 7, 'daily', 'queued', ?)", (fmt(now - timedelta(hours=4)),))
        c.execute("INSERT INTO insight_runs (position_id, user_id, trigger, status, started_at) VALUES (3, 7, 'daily', 'running', ?)", (fmt(now - timedelta(minutes=20)),))
        c.execute("INSERT INTO insight_runs (position_id, user_id, trigger, status, started_at) VALUES (4, 7, 'daily', 'success', ?)", (fmt(now - timedelta(days=2)),))
        c.commit()
        with mock.patch.object(ie, "get_users_conn", return_value=c):
            self.assertEqual(ie.expire_stale_runs(), 2)
        got = {r["position_id"]: r["status"] for r in c.execute("SELECT position_id, status FROM insight_runs")}
        self.assertEqual(got, {1: "failed", 2: "failed", 3: "running", 4: "success"})
        self.assertIn("did not finish", c.execute("SELECT error FROM insight_runs WHERE position_id = 1").fetchone()["error"])


if __name__ == "__main__":
    unittest.main()
