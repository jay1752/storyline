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
        self.assertEqual(self.deliver(self.SUN_10), {"immediate": 0, "digests": 0, "flags": 0, "skipped_cap": 0})
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



class FlagEmail(unittest.TestCase):
    """The default-on email: ONE a week per user (Monday morning, India time), every tracked stock whose Exit, Trim or Add rule is triggered, new ones marked."""

    def setUp(self):
        self.u = make_db()
        for pid, uid, sym, kind in ((4, 7, "NSE:CCC", "position"), (5, 7, "NSE:DDD", "watchlist")):
            self.u.execute("INSERT INTO tracked_positions (id, user_id, symbol, company_name, anchor_type, anchor_date, status) VALUES (?,?,?,?,?, '2026-01-01', 'active')",
                           (pid, uid, sym, sym + " Ltd", kind))
        self.u.commit()
        self.sent, self.ok = [], True
        p = mock.patch("app.db.get_users_conn", return_value=self.u)
        p.start()
        self.addCleanup(p.stop)
        today = datetime.now(IST)
        self.now = (today - timedelta(days=today.weekday())).replace(hour=10, minute=0, second=0, microsecond=0)   # the latest Monday, 10:00 IST

    def send(self, to, subject, html, text, headers):
        self.sent.append({"to": to, "subject": subject, "html": html, "text": text, "headers": headers})
        return self.ok

    def deliver(self, now=None):
        return alerts.deliver(send=self.send, now=now or self.now)

    def status(self, pid, uid, code, label, reason="Your rule is triggered."):
        st.record(self.u, pid, uid, res(code, label, reasons=[{"kind": "entry", "text": reason}]))
        self.u.commit()

    def age_statuses(self, when="2026-01-01 00:00:00"):
        self.u.execute("UPDATE position_status_history SET computed_at = ?", (when,))
        self.u.commit()

    def told(self, uid, days_ago):
        alerts.get_prefs(self.u, uid)
        self.u.execute("UPDATE alert_prefs SET last_flag_email_at = ? WHERE user_id = ?", ((self.now.astimezone(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S") if days_ago is not None else None, uid))   # relative to the simulated Monday
        self.u.commit()

    def test_one_email_lists_every_flagged_stock_grouped_exit_trim_add(self):
        self.status(1, 7, "EXIT", "Exit rule triggered", "Promoter pledge is 13.8%.")
        self.status(2, 7, "TRIM", "Trim rule triggered", "This is 9.8% of your book.")
        self.status(4, 7, "BUY", "Add rules met", "All 9 rules are met.")
        self.status(5, 7, "BUY", "Add rules met", "A watchlist stock passes every rule.")
        self.assertEqual(self.deliver()["flags"], 1)
        m = self.sent[0]
        self.assertEqual((m["to"], m["subject"]), ("me@example.com", "Your week: 4 tracked stocks have a rule triggered"))
        for t in ("Exit rule triggered (1)", "Trim rule triggered (1)", "Add rules met (2)", "NSE:AAA", "NSE:BBB", "NSE:CCC", "NSE:DDD", "Promoter pledge is 13.8%"):
            self.assertIn(t, m["html"])
        self.assertLess(m["html"].index("Exit rule triggered"), m["html"].index("Trim rule triggered"))
        self.assertLess(m["html"].index("Trim rule triggered"), m["html"].index("Add rules met"))
        self.assertEqual(m["html"].count(">new<"), 4)                    # the first email: everything is news
        self.assertIn("once a week, on Mondays", m["html"])
        self.assertIn("/alerts/unsubscribe?token=", m["html"])
        self.assertIn("List-Unsubscribe", m["headers"])
        self.assertIn("Not investment advice", m["html"])

    def test_a_single_stock_reads_naturally(self):
        self.status(1, 7, "EXIT", "Exit rule triggered")
        self.deliver()
        self.assertEqual(self.sent[0]["subject"], "Your week: 1 tracked stock has a rule triggered")

    def test_it_is_on_by_default_with_no_preferences_row_and_only_once(self):
        self.status(1, 7, "EXIT", "Exit rule triggered")
        self.assertIsNone(self.u.execute("SELECT 1 FROM alert_prefs WHERE user_id = 7").fetchone())
        self.assertEqual(self.deliver()["flags"], 1)
        self.assertEqual(self.deliver()["flags"], 0)
        self.assertEqual(len(self.sent), 1)

    def test_a_user_with_nothing_triggered_gets_nothing(self):
        self.status(1, 7, "HOLD", "Within your rules")
        self.status(2, 7, "WATCH", "Not enough data")
        self.assertEqual(self.deliver()["flags"], 0)
        self.assertEqual(self.sent, [])

    def test_it_goes_on_monday_morning_only(self):
        self.status(1, 7, "EXIT", "Exit rule triggered")
        for d in range(1, 7):                                            # Tuesday .. Sunday
            self.assertEqual(self.deliver(self.now + timedelta(days=d))["flags"], 0, d)
        self.assertEqual(self.deliver(self.now.replace(hour=7))["flags"], 0)   # Monday before nine
        self.assertEqual(self.deliver(self.now.replace(hour=9))["flags"], 1)

    def test_switching_it_off_or_unsubscribing_stops_it(self):
        self.status(1, 7, "EXIT", "Exit rule triggered")
        alerts.set_prefs(self.u, 7, digest_email=False, digest_day=6, exit_email=False, results_alerts=True, flag_email=False)
        self.assertEqual(self.deliver()["flags"], 0)
        alerts.set_prefs(self.u, 7, digest_email=False, digest_day=6, exit_email=False, results_alerts=True, flag_email=True)
        self.assertEqual(self.deliver()["flags"], 1)
        self.told(7, None)
        self.assertTrue(alerts.unsubscribe(self.u, alerts.get_prefs(self.u, 7)["token"]))
        self.assertEqual(self.deliver()["flags"], 0)
        self.assertEqual(alerts.get_prefs(self.u, 7)["flag_email"], 0)

    def test_something_new_midweek_waits_for_the_next_monday_and_is_marked_new(self):
        self.status(1, 7, "EXIT", "Exit rule triggered")
        self.age_statuses()
        self.told(7, 3)                                                  # emailed three days ago
        self.status(2, 7, "TRIM", "Trim rule triggered")                 # became triggered since
        self.assertEqual(self.deliver()["flags"], 0)                     # one a week: not yet
        self.told(7, 8)
        self.assertEqual(self.deliver()["flags"], 1)
        m = self.sent[0]
        self.assertEqual(m["subject"], "Your week: 2 tracked stocks have a rule triggered, 1 new")
        self.assertEqual(m["html"].count(">new<"), 1)

    def test_a_standing_flag_is_listed_again_every_week_without_a_new_mark(self):
        self.status(1, 7, "EXIT", "Exit rule triggered")
        self.age_statuses()
        self.told(7, 8)
        self.assertEqual(self.deliver()["flags"], 1)
        self.assertNotIn(">new<", self.sent[0]["html"])
        self.assertIn("NSE:AAA", self.sent[0]["html"])

    def test_a_price_driven_flip_is_in_the_next_weekly_email(self):
        self.status(1, 7, "HOLD", "Within your rules")
        self.age_statuses()
        self.told(7, 8)
        self.status(1, 7, "TRIM", "Trim rule triggered", "This is 8.4% of your book. Your rule: above 8%, trim back toward 5%.")   # the price rose
        self.assertEqual(self.deliver()["flags"], 1)
        self.assertIn("8.4% of your book", self.sent[0]["html"])
        self.assertEqual(self.sent[0]["html"].count(">new<"), 1)

    def test_a_long_list_is_cut_per_section_and_points_to_management(self):
        for i in range(30):
            pid = 100 + i
            self.u.execute("INSERT INTO tracked_positions (id, user_id, symbol, company_name, anchor_type, anchor_date, status) VALUES (?, 7, ?, ?, 'position', '2026-01-01', 'active')", (pid, f"NSE:S{i:02d}", f"S{i:02d} Ltd"))
            st.record(self.u, pid, 7, res("EXIT", "Exit rule triggered"))
        self.u.commit()
        self.deliver()
        h = self.sent[0]["html"]
        self.assertIn("Exit rule triggered (30)", h)
        self.assertIn("and 5 more", h)
        self.assertEqual(h.count("/management/"), 25)                     # 25 stock links; the "see them all" link points at /management itself

    def test_each_user_gets_only_their_own_stocks(self):
        self.status(1, 7, "EXIT", "Exit rule triggered")
        self.status(3, 8, "TRIM", "Trim rule triggered")
        self.assertEqual(self.deliver()["flags"], 2)
        to = {m["to"]: m["html"] for m in self.sent}
        self.assertIn("NSE:AAA", to["me@example.com"])
        self.assertNotIn("Trim rule triggered", to["me@example.com"])
        self.assertEqual(set(to), {"me@example.com", "alerts-here@example.com"})

    def test_a_failed_send_is_retried_and_not_counted_as_told(self):
        self.status(1, 7, "EXIT", "Exit rule triggered")
        self.ok = False
        self.assertEqual(self.deliver()["flags"], 0)
        self.assertIsNone(alerts.get_prefs(self.u, 7)["last_flag_email_at"])
        self.assertEqual([r["ok"] for r in self.u.execute("SELECT ok FROM email_log WHERE kind = 'flags'")], [0])
        self.ok = True
        self.assertEqual(self.deliver()["flags"], 1)

    def test_the_daily_sender_cap_applies(self):
        self.status(1, 7, "EXIT", "Exit rule triggered")
        for _ in range(alerts.DAILY_EMAIL_CAP):
            self.u.execute("INSERT INTO email_log (user_id, kind, ok, sent_at) VALUES (99, 'digest', 1, ?)", (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),))
        self.u.commit()
        out = self.deliver()
        self.assertEqual((out["flags"], out["skipped_cap"]), (0, 1))

    def test_text_says_what_the_rules_show_never_what_to_do_and_escapes_names(self):
        self.u.execute("UPDATE tracked_positions SET company_name = '<script>x</script>' WHERE id = 1")
        self.status(1, 7, "EXIT", "Exit rule triggered", "Your exit rule on ROCE is confirmed over two quarters.")
        self.status(2, 7, "TRIM", "Trim rule triggered")
        self.deliver()
        m = self.sent[0]
        low = (m["subject"] + m["html"] + m["text"]).lower()
        for bad in ("you should", "consider", "we suggest", "it is time to", "buy now", "sell now"):
            self.assertNotIn(bad, low)
        self.assertNotIn("<script>x</script>", m["html"])


if __name__ == "__main__":
    unittest.main()
