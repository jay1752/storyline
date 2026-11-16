"""Alerts (app/alerts.py): what raises one, who gets email and when. In-memory database and a fake sender: nothing is ever really sent.
Run: python -m unittest tests.test_alerts -v   (from the repo root)"""

import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from app import alerts
from app.ai import status as st

IST = timezone(timedelta(hours=5, minutes=30))


def make_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    from app.db import USERS_SCHEMA
    c.executescript(USERS_SCHEMA)
    c.execute("ATTACH DATABASE ':memory:' AS market")
    c.execute("CREATE TABLE market.results_calendar (symbol TEXT, event_date TEXT, purpose TEXT, source TEXT DEFAULT 'nse', fetched_at TEXT DEFAULT '', status TEXT DEFAULT 'upcoming', "
              "tries INTEGER DEFAULT 0, last_try TEXT, note TEXT, PRIMARY KEY (symbol, event_date))")
    c.execute("ALTER TABLE users ADD COLUMN contact_email TEXT")   # added by a migration in the real database
    c.execute("INSERT INTO users (id, email, contact_email) VALUES (7, 'me@example.com', NULL)")
    c.execute("INSERT INTO users (id, email, contact_email) VALUES (8, 'them@example.com', 'alerts-here@example.com')")
    for pid, uid, sym in ((1, 7, "NSE:AAA"), (2, 7, "NSE:BBB"), (3, 8, "NSE:AAA")):
        c.execute("INSERT INTO tracked_positions (id, user_id, symbol, company_name, anchor_type, anchor_date, status) VALUES (?,?,?,?, 'position', '2026-01-01', 'active')",
                  (pid, uid, sym, sym + " Ltd"))
    c.commit()
    return c


def res(code="HOLD", label="Within your rules", h="aaa", reasons=None):
    return {"code": code, "label": label, "reasons": reasons or [{"kind": "entry", "text": "Not met: ROCE 10% against your rule > 25%."}], "enough_data": True, "owned": True,
            "rules": {"judged": 8, "passing": 7, "failing": ["x"], "unknown": []}, "speed1": {"status": "clear", "note": None},
            "speed2": {"status": "clear", "items": []}, "weight_pct": 4.0, "sector_pct": 10.0, "rules_hash": h}


class Raising(unittest.TestCase):
    def setUp(self):
        self.u = make_db()

    def rows(self):
        return [dict(r) for r in self.u.execute("SELECT * FROM alerts ORDER BY id")]

    def test_the_first_status_is_not_an_alert_a_change_is(self):
        st.record(self.u, 1, 7, res())
        self.assertEqual(self.rows(), [])
        st.record(self.u, 1, 7, res("TRIM", "Trim rule triggered"))
        a = self.rows()[0]
        self.assertEqual((a["kind"], a["symbol"], a["code_from"], a["code_to"], a["title"]), ("status_change", "NSE:AAA", "HOLD", "TRIM", "NSE:AAA: Trim rule triggered"))
        self.assertIn("Was: Within your rules.", a["body"])

    def test_same_label_under_a_new_basis_is_not_an_alert(self):
        st.record(self.u, 1, 7, res("EXIT", "Exit rule triggered"))
        r2 = res("EXIT", "Exit rule triggered")
        r2["speed2"]["items"] = [{"rule": "ROCE", "state": "confirmed", "note": ""}]
        self.assertTrue(st.record(self.u, 1, 7, r2))
        self.assertEqual(self.rows(), [])

    def test_moves_to_or_from_not_enough_data_are_not_alerts(self):
        st.record(self.u, 1, 7, res())
        st.record(self.u, 1, 7, res("WATCH", "Not enough data"))
        st.record(self.u, 1, 7, res("TRIM", "Trim rule triggered"))
        self.assertEqual(self.rows(), [])

    def test_cause_is_rules_when_the_users_rule_settings_changed_else_data(self):
        st.record(self.u, 1, 7, res(h="aaa"))
        st.record(self.u, 1, 7, res("TRIM", "Trim rule triggered", h="bbb"))
        st.record(self.u, 1, 7, res("EXIT", "Exit rule triggered", h="bbb"))
        causes = [r["cause"] for r in self.rows()]
        self.assertEqual(causes, ["rules", "data"])
        self.assertIn("change to your rule settings", self.rows()[0]["body"])

    def test_cause_is_left_unsaid_when_the_earlier_status_has_no_rule_fingerprint(self):
        st.record(self.u, 1, 7, res(h=None))
        st.record(self.u, 1, 7, res("TRIM", "Trim rule triggered", h="bbb"))
        a = self.rows()[0]
        self.assertIsNone(a["cause"])
        self.assertNotIn("This followed", a["body"])

    def test_an_alert_failing_to_save_never_breaks_recording_the_status(self):
        st.record(self.u, 1, 7, res())
        with mock.patch.object(alerts, "on_status_change", side_effect=RuntimeError("boom")):
            self.assertTrue(st.record(self.u, 1, 7, res("TRIM", "Trim rule triggered")))
        self.assertEqual(st.stored(self.u, 1)["code"], "TRIM")

    def test_results_due_is_once_per_date_inside_the_window_for_active_upcoming_only(self):
        today = date(2026, 11, 17)
        m = self.u
        m.execute("INSERT INTO market.results_calendar (symbol, event_date, purpose) VALUES ('NSE:AAA', '2026-11-20', 'Financial Results')")      # in window
        m.execute("INSERT INTO market.results_calendar (symbol, event_date, purpose) VALUES ('NSE:BBB', '2026-12-30', 'Financial Results')")      # too far
        m.execute("INSERT INTO market.results_calendar (symbol, event_date, status) VALUES ('NSE:BBB', '2026-11-18', 'refreshed')")              # not upcoming
        m.commit()
        self.assertEqual(alerts.results_due(m, today), 2)   # NSE:AAA is tracked by two users
        self.assertEqual(alerts.results_due(m, today), 0)   # again: nothing new
        self.assertEqual({r["user_id"] for r in self.rows()}, {7, 8})
        self.assertIn("2026-11-20", self.rows()[0]["title"])

    def test_results_alerts_can_be_switched_off(self):
        alerts.set_prefs(self.u, 7, digest_email=False, digest_day=6, exit_email=False, results_alerts=False)
        self.u.execute("INSERT INTO market.results_calendar (symbol, event_date) VALUES ('NSE:AAA', '2026-11-20')")
        self.u.commit()
        alerts.results_due(self.u, date(2026, 11, 17))
        self.assertEqual({r["user_id"] for r in self.rows()}, {8})


class Delivery(unittest.TestCase):
    def setUp(self):
        self.u = make_db()
        self.sent = []
        self.ok = True
        self.patch = mock.patch("app.db.get_users_conn", return_value=self.u)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def send(self, to, subject, html, text, headers):
        self.sent.append({"to": to, "subject": subject, "html": html, "text": text, "headers": headers})
        return self.ok

    def deliver(self, now):
        return alerts.deliver(send=self.send, now=now)

    def alert(self, uid=7, pid=1, to="EXIT", label="Exit rule triggered", ref=None, ago_h=1):
        self.u.execute("INSERT INTO alerts (user_id, position_id, symbol, kind, ref, title, body, code_from, code_to, created_at) VALUES (?,?,?, 'status_change', ?, ?, 'Was: Within your rules.', 'HOLD', ?, ?)",
                       (uid, pid, f"NSE:S{pid}", ref or f"r{pid}-{to}-{label}", f"NSE:S{pid}: {label}", to,
                        (datetime.now(timezone.utc) - timedelta(hours=ago_h)).strftime("%Y-%m-%d %H:%M:%S")))
        self.u.commit()

    def opt(self, uid=7, **kw):
        base = dict(digest_email=False, digest_day=6, exit_email=False, results_alerts=True)
        base.update(kw)
        alerts.set_prefs(self.u, uid, **base)

    NOW = datetime.now(IST)                              # immediate emails are judged on alerts made in the last 24 hours, so they use the real clock
    SUN_10 = datetime(2026, 11, 22, 10, 0, tzinfo=IST)   # a Sunday, 10:00 IST
    SUN_8 = datetime(2026, 11, 22, 8, 0, tzinfo=IST)
    MON_10 = datetime(2026, 11, 23, 10, 0, tzinfo=IST)

    def test_nothing_is_sent_unless_the_user_opted_in(self):
        self.alert()
        self.alert(to="TRIM", label="Trim rule triggered")
        self.opt()   # prefs exist, every email off
        self.assertEqual(self.deliver(self.SUN_10), {"immediate": 0, "digests": 0, "skipped_cap": 0})
        self.assertEqual(self.sent, [])

    def test_exit_email_goes_once_with_unsubscribe_headers_and_only_for_exit(self):
        self.opt(exit_email=True)
        self.alert()
        self.alert(to="TRIM", label="Trim rule triggered")
        self.assertEqual(self.deliver(self.NOW)["immediate"], 1)
        self.assertEqual(self.deliver(self.NOW)["immediate"], 0)   # not again
        m = self.sent[0]
        self.assertEqual(m["to"], "me@example.com")
        self.assertIn("NSE:S1: Exit rule triggered", m["subject"])
        self.assertIn("/alerts/unsubscribe?token=", m["headers"]["List-Unsubscribe"])
        self.assertIn("Not investment advice", m["html"])
        self.assertIn("Not investment advice", m["text"])

    def test_the_contact_email_is_used_when_set(self):
        self.opt(uid=8, exit_email=True)
        self.alert(uid=8, pid=3)
        self.deliver(self.NOW)
        self.assertEqual(self.sent[0]["to"], "alerts-here@example.com")

    def test_an_exit_alert_older_than_a_day_is_not_sent_immediately(self):
        self.opt(exit_email=True)
        self.alert(ago_h=30)
        self.assertEqual(self.deliver(self.NOW)["immediate"], 0)

    def test_failed_send_is_logged_and_retried_later(self):
        self.opt(exit_email=True)
        self.alert()
        self.ok = False
        self.assertEqual(self.deliver(self.NOW)["immediate"], 0)
        self.assertEqual([r["ok"] for r in self.u.execute("SELECT ok FROM email_log")], [0])
        self.ok = True
        self.assertEqual(self.deliver(self.NOW)["immediate"], 1)

    def test_digest_goes_on_the_chosen_day_after_nine_and_only_once_a_week(self):
        self.opt(digest_email=True, digest_day=6)
        self.alert(to="TRIM", label="Trim rule triggered", ref="a")
        self.alert(pid=2, ref="b", to="EXIT")
        self.assertEqual(self.deliver(self.SUN_8)["digests"], 0)     # too early
        self.assertEqual(self.deliver(self.MON_10)["digests"], 0)    # wrong day
        self.assertEqual(self.deliver(self.SUN_10)["digests"], 1)
        self.assertEqual(self.deliver(self.SUN_10)["digests"], 0)    # not twice
        m = self.sent[0]
        self.assertEqual(m["subject"], "Your Tenetfolio week: 2 changes")
        self.assertIn("List-Unsubscribe-Post", m["headers"])
        self.assertEqual(self.u.execute("SELECT COUNT(*) FROM alerts WHERE digested_at IS NULL").fetchone()[0], 0)

    def test_no_digest_in_a_week_with_nothing_new(self):
        self.opt(digest_email=True, digest_day=6)
        self.assertEqual(self.deliver(self.SUN_10)["digests"], 0)
        self.assertEqual(self.sent, [])

    def test_an_alert_already_emailed_immediately_is_not_repeated_in_the_digest(self):
        self.opt(exit_email=True, digest_email=True, digest_day=6)
        self.alert()
        self.deliver(self.NOW)                       # the immediate email
        self.assertEqual(self.deliver(self.SUN_10)["digests"], 0)

    def test_daily_cap_stops_sending(self):
        self.opt(exit_email=True)
        self.alert()
        for _ in range(alerts.DAILY_EMAIL_CAP):
            self.u.execute("INSERT INTO email_log (user_id, kind, ok, sent_at) VALUES (99, 'digest', 1, ?)", (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),))
        self.u.commit()
        out = self.deliver(datetime.now(IST))
        self.assertEqual((out["immediate"], out["skipped_cap"]), (0, 1))

    def test_at_most_five_immediate_emails_per_user_per_day(self):
        self.opt(exit_email=True)
        for i in range(8):
            self.alert(pid=1, ref=f"x{i}")
        self.assertEqual(self.deliver(self.NOW)["immediate"], alerts.IMMEDIATE_PER_USER_PER_DAY)

    def test_unsubscribe_switches_emails_off_and_rejects_a_bad_token(self):
        self.opt(exit_email=True, digest_email=True)
        token = alerts.get_prefs(self.u, 7)["token"]
        self.assertFalse(alerts.unsubscribe(self.u, "nope"))
        self.assertFalse(alerts.unsubscribe(self.u, ""))
        self.assertTrue(alerts.unsubscribe(self.u, token))
        p = alerts.get_prefs(self.u, 7)
        self.assertEqual((p["exit_email"], p["digest_email"]), (0, 0))
        self.alert()
        self.assertEqual(self.deliver(self.NOW)["immediate"], 0)

    def test_consent_time_is_recorded_when_an_email_is_first_turned_on(self):
        self.opt()
        self.assertIsNone(alerts.get_prefs(self.u, 7)["consent_at"])
        self.opt(digest_email=True)
        self.assertIsNotNone(alerts.get_prefs(self.u, 7)["consent_at"])

    def test_email_text_never_tells_the_user_what_to_do(self):
        self.opt(exit_email=True, digest_email=True, digest_day=6)
        self.alert()
        self.alert(to="TRIM", label="Trim rule triggered", ref="t")
        self.deliver(self.NOW)
        self.deliver(self.SUN_10)
        for m in self.sent:
            text = (m["subject"] + m["html"] + m["text"]).lower()
            for bad in ("you should", "consider", "we suggest", "it is time to", "buy now", "sell now"):
                self.assertNotIn(bad, text)

    def test_html_in_a_title_is_escaped(self):
        self.opt(exit_email=True)
        self.alert()
        self.u.execute("UPDATE alerts SET title = '<script>x</script>'")
        self.u.commit()
        self.deliver(self.NOW)
        self.assertNotIn("<script>x</script>", self.sent[0]["html"])


if __name__ == "__main__":
    unittest.main()
