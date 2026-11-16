"""Alerts: tell the user when something changed in their own rules or data, so they need not open every stock to find out.

What raises one (never a price move):
  status_change  a tracked stock's status against the user's rules changed (app/ai/status.py records every change; see on_status_change).
                 Moves to or from "Not enough data" are left out: that is missing data, not a change in the stock.
  results_due    a tracked Indian stock announces results within RESULTS_WINDOW_DAYS (market.results_calendar), once per result date.

Where it shows: always in the app (/alerts and a card on Management). Email is opt-in and off until the user turns it on:
  - a weekly digest on the day they choose, only in weeks that had a change;
  - optionally one immediate email when a stock reaches "Exit rule triggered".
Every email has an unsubscribe link, carries the "not investment advice" line, and is counted in email_log so a daily cap protects the sender's
free allowance. Wording says what the rules show, never what to do."""

from __future__ import annotations

import html
import logging
import secrets
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
NO_DATA = "Not enough data"
RESULTS_WINDOW_DAYS = 7
DAILY_EMAIL_CAP = 80            # Resend's free plan allows 100 a day: stay under it
IMMEDIATE_PER_USER_PER_DAY = 5
DIGEST_HOUR_IST = 9
DIGEST_MIN_GAP_DAYS = 6
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
LINE = "Not investment advice. Each status shows where a stock stands against rules you set, worked out from public data. Any decision is yours."


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------------------------------- preferences

def get_prefs(u, user_id: int):
    """The user's alert preferences, created with everything-by-email off on first use."""
    r = u.execute("SELECT * FROM alert_prefs WHERE user_id = ?", (user_id,)).fetchone()
    if r is None:
        u.execute("INSERT INTO alert_prefs (user_id, token) VALUES (?, ?)", (user_id, secrets.token_urlsafe(24)))
        u.commit()
        r = u.execute("SELECT * FROM alert_prefs WHERE user_id = ?", (user_id,)).fetchone()
    return r


def set_prefs(u, user_id: int, *, digest_email: bool, digest_day: int, exit_email: bool, results_alerts: bool) -> None:
    get_prefs(u, user_id)
    day = digest_day if isinstance(digest_day, int) and 0 <= digest_day <= 6 else 6
    u.execute("""UPDATE alert_prefs SET digest_email = ?, digest_day = ?, exit_email = ?, results_alerts = ?, updated_at = ?,
                 consent_at = CASE WHEN (? OR ?) AND consent_at IS NULL THEN ? ELSE consent_at END WHERE user_id = ?""",
              (int(digest_email), day, int(exit_email), int(results_alerts), _now_utc(), int(digest_email), int(exit_email), _now_utc(), user_id))
    u.commit()


def unsubscribe(u, token: str) -> bool:
    """Switch every alert email off for the owner of this token. True when the token was valid."""
    if not token:
        return False
    cur = u.execute("UPDATE alert_prefs SET digest_email = 0, exit_email = 0, updated_at = ? WHERE token = ?", (_now_utc(), token))
    u.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------------------------------- creating alerts

def on_status_change(u, user_id: int, position_id: int, prev: dict, new_id: int, result: dict, new_detail: dict) -> int | None:
    """Called when a new status row is recorded after an earlier one. `prev` = {label, detail}. Returns the alert id, or None."""
    if prev["label"] == result["label"] or NO_DATA in (prev["label"], result["label"]):
        return None
    pos = u.execute("SELECT symbol, company_name FROM tracked_positions WHERE id = ?", (position_id,)).fetchone()
    if not pos:
        return None
    old_h, new_h = (prev.get("detail") or {}).get("rules_hash"), (new_detail or {}).get("rules_hash")
    cause = None if not (old_h and new_h) else ("rules" if old_h != new_h else "data")   # unknown for a status recorded before rule settings were fingerprinted
    reasons = [x["text"] for x in result["reasons"] if x["kind"] != "note"][:2]
    said = {"rules": "This followed a change to your rule settings.", "data": "This followed new company data, or a price move that changed a position's size.", None: ""}[cause]
    body = " ".join(x for x in [f"Was: {prev['label']}."] + reasons + [said] if x)
    cur = u.execute("""INSERT OR IGNORE INTO alerts (user_id, position_id, symbol, kind, ref, title, body, code_from, code_to, cause)
                       VALUES (?, ?, ?, 'status_change', ?, ?, ?, ?, ?, ?)""",
                    (user_id, position_id, pos["symbol"], str(new_id), f"{pos['symbol']}: {result['label']}", body, prev.get("code"), result["code"], cause))
    return cur.lastrowid if cur.rowcount else None


def results_due(u, today: date | None = None) -> int:
    """One alert per tracked Indian stock whose results are announced within the next RESULTS_WINDOW_DAYS days (once per result date).
    `u` needs market attached. Returns how many alerts were created."""
    today = today or datetime.now(IST).date()
    horizon = today + timedelta(days=RESULTS_WINDOW_DAYS)
    rows = u.execute("""SELECT tp.id pid, tp.user_id, tp.symbol, rc.event_date, rc.purpose FROM tracked_positions tp
                        JOIN market.results_calendar rc ON rc.symbol = tp.symbol AND rc.status = 'upcoming'
                        LEFT JOIN alert_prefs p ON p.user_id = tp.user_id
                        WHERE tp.status = 'active' AND rc.event_date >= ? AND rc.event_date <= ? AND COALESCE(p.results_alerts, 1) = 1""",
                     (today.isoformat(), horizon.isoformat())).fetchall()
    made = 0
    for r in rows:
        body = (f"Results are announced for {r['event_date']}" + (f" ({r['purpose']})" if r["purpose"] else "") +
                ". Your status for this stock is worked out again once the new quarter is stored.")
        cur = u.execute("""INSERT OR IGNORE INTO alerts (user_id, position_id, symbol, kind, ref, title, body)
                           VALUES (?, ?, ?, 'results_due', ?, ?, ?)""",
                        (r["user_id"], r["pid"], r["symbol"], r["event_date"], f"{r['symbol']}: results expected {r['event_date']}", body))
        made += cur.rowcount
    u.commit()
    return made


def nightly() -> int:
    """The once-a-day part: result-date alerts for every user."""
    from app.db import get_users_conn
    with get_users_conn() as u:
        return results_due(u)


# ---------------------------------------------------------------------------------------------------- reading them

def recent(u, user_id: int, limit: int = 100, unread_only: bool = False) -> list[dict]:
    q = "SELECT * FROM alerts WHERE user_id = ?" + (" AND read_at IS NULL" if unread_only else "") + " ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in u.execute(q, (user_id, limit)).fetchall()]


def unread_count(u, user_id: int) -> int:
    return u.execute("SELECT COUNT(*) FROM alerts WHERE user_id = ? AND read_at IS NULL", (user_id,)).fetchone()[0]


def mark_read(u, user_id: int) -> None:
    u.execute("UPDATE alerts SET read_at = ? WHERE user_id = ? AND read_at IS NULL", (_now_utc(), user_id))
    u.commit()


# ---------------------------------------------------------------------------------------------------- email

def _base_url() -> str:
    from app.auth.email import APP_BASE_URL
    return APP_BASE_URL.rstrip("/")


def _item_html(a: dict, base: str) -> str:
    link = f"{base}/management/{a['position_id']}" if a.get("position_id") else f"{base}/alerts"
    return (f'<p style="margin:0 0 14px"><b>{html.escape(a["title"])}</b><br>'
            f'<span style="color:#444">{html.escape(a["body"] or "")}</span><br>'
            f'<a href="{html.escape(link)}">Open the story</a></p>')


def _wrap(heading: str, items: str, token: str, base: str) -> str:
    unsub = f"{base}/alerts/unsubscribe?token={token}"
    return (f'<div style="font-family:system-ui,Arial,sans-serif;max-width:560px;font-size:15px;line-height:1.45;color:#1a1a1a">'
            f'<h2 style="font-size:18px;margin:0 0 14px">{html.escape(heading)}</h2>{items}'
            f'<hr style="border:none;border-top:1px solid #ddd;margin:18px 0">'
            f'<p style="font-size:12.5px;color:#666;margin:0 0 8px">{html.escape(LINE)}</p>'
            f'<p style="font-size:12.5px;color:#666;margin:0">You get this because you turned on alert emails in Tenetfolio. '
            f'<a href="{base}/profile#alerts">Change what you receive</a> or <a href="{html.escape(unsub)}">unsubscribe</a>.</p></div>')


def _text(items: list[dict], token: str, base: str) -> str:
    lines = [f"{a['title']}\n{a['body'] or ''}\n{base}/management/{a['position_id']}\n" for a in items]
    return "\n".join(lines) + f"\n{LINE}\nUnsubscribe: {base}/alerts/unsubscribe?token={token}\n"


def exit_email(a: dict, token: str) -> tuple[str, str, str]:
    base = _base_url()
    return (f"{a['symbol']}: Exit rule triggered", _wrap("An exit rule of yours is triggered", _item_html(a, base), token, base), _text([a], token, base))


def digest_email(items: list[dict], token: str) -> tuple[str, str, str]:
    base = _base_url()
    n = len(items)
    return (f"Your Tenetfolio week: {n} change{'' if n == 1 else 's'}", _wrap("What changed in your tracked stocks", "".join(_item_html(a, base) for a in items), token, base),
            _text(items, token, base))


def _address(u, user_id: int) -> str | None:
    r = u.execute("SELECT email, contact_email FROM users WHERE id = ?", (user_id,)).fetchone()
    return (r["contact_email"] or r["email"]) if r else None


def deliver(send=None, now: datetime | None = None) -> dict:
    """Send what is due: immediate Exit emails (opt-in), then weekly digests (opt-in). Safe to call every few minutes. `send(to, subject, html,
    text, headers) -> bool`; None uses the Resend sender (which sends nothing when no key is set). Returns counts."""
    from app.auth import email as mail
    from app.db import get_users_conn
    send = send or mail.send_message
    now = (now or datetime.now(IST)).astimezone(IST)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    out = {"immediate": 0, "digests": 0, "skipped_cap": 0}
    with get_users_conn() as u:
        sent_today = u.execute("SELECT COUNT(*) FROM email_log WHERE ok = 1 AND sent_at >= ?", (day_start,)).fetchone()[0]

        def log(user_id, kind, ok):
            u.execute("INSERT INTO email_log (user_id, kind, ok) VALUES (?, ?, ?)", (user_id, kind, int(ok)))

        cut = (now - timedelta(hours=24)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        due = u.execute("""SELECT a.*, p.token FROM alerts a JOIN alert_prefs p ON p.user_id = a.user_id
                           WHERE p.exit_email = 1 AND a.kind = 'status_change' AND a.code_to = 'EXIT' AND a.emailed_at IS NULL
                           AND a.digested_at IS NULL AND a.created_at >= ? ORDER BY a.id""", (cut,)).fetchall()
        per_user: dict[int, int] = {}
        for a in due:
            a = dict(a)
            if sent_today >= DAILY_EMAIL_CAP:
                out["skipped_cap"] += 1
                continue
            if per_user.setdefault(a["user_id"], u.execute("SELECT COUNT(*) FROM email_log WHERE user_id = ? AND kind = 'exit' AND ok = 1 AND sent_at >= ?",
                                                           (a["user_id"], day_start)).fetchone()[0]) >= IMMEDIATE_PER_USER_PER_DAY:
                continue
            to = _address(u, a["user_id"])
            if not to:
                continue
            subject, h, t = exit_email(a, a["token"])
            ok = send(to, subject, h, t, {"List-Unsubscribe": f"<{_base_url()}/alerts/unsubscribe?token={a['token']}>"})
            log(a["user_id"], "exit", ok)
            if ok:
                u.execute("UPDATE alerts SET emailed_at = ? WHERE id = ?", (_now_utc(), a["id"]))
                per_user[a["user_id"]] += 1
                sent_today += 1
                out["immediate"] += 1

        if now.hour >= DIGEST_HOUR_IST:
            gap = (now - timedelta(days=DIGEST_MIN_GAP_DAYS)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            for p in u.execute("""SELECT * FROM alert_prefs WHERE digest_email = 1 AND digest_day = ?
                                  AND (last_digest_at IS NULL OR last_digest_at <= ?)""", (now.weekday(), gap)).fetchall():
                items = [dict(r) for r in u.execute("""SELECT * FROM alerts WHERE user_id = ? AND digested_at IS NULL AND emailed_at IS NULL
                                                        ORDER BY id DESC LIMIT 40""", (p["user_id"],))]
                if not items:
                    continue
                if sent_today >= DAILY_EMAIL_CAP:
                    out["skipped_cap"] += 1
                    continue
                to = _address(u, p["user_id"])
                if not to:
                    continue
                subject, h, t = digest_email(items, p["token"])
                ok = send(to, subject, h, t, {"List-Unsubscribe": f"<{_base_url()}/alerts/unsubscribe?token={p['token']}>",
                                              "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
                log(p["user_id"], "digest", ok)
                if ok:
                    ids = ",".join(str(i["id"]) for i in items)
                    u.execute(f"UPDATE alerts SET digested_at = ? WHERE id IN ({ids})", (_now_utc(),))
                    u.execute("UPDATE alert_prefs SET last_digest_at = ? WHERE user_id = ?", (_now_utc(), p["user_id"]))
                    sent_today += 1
                    out["digests"] += 1
        u.commit()
    return out
