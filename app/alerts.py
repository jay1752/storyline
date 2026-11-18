"""Alerts: tell the user when something changed in their own rules or data, so they need not open every stock to find out.

What raises one (never a price move):
  status_change  a tracked stock's status against the user's rules changed (app/ai/status.py records every change; see on_status_change).
                 Moves to or from "Not enough data" are left out: that is missing data, not a change in the stock.
  results_due    a tracked Indian stock announces results within RESULTS_WINDOW_DAYS (market.results_calendar), once per result date.

Where it shows: always in the app (/alerts and a card on Management). Email:
  - ON BY DEFAULT, the user can switch it off in Profile or with the unsubscribe link: ONE consolidated email per user, at most one a day, listing every
    tracked stock whose Exit, Trim or Add rule is triggered (flag_email, see flag_emails). It is sent when something newly became triggered, and as a
    weekly reminder while flags stay open. A price move that pushes a position past the user's size limit counts: it changes the status.
  - opt-in extras, off until chosen: a weekly digest of status changes on a day they pick, and one immediate email when a stock reaches Exit.
Every email has an unsubscribe link, carries the "not investment advice" line, and is counted in email_log so a daily cap protects the sender's
free allowance. Wording says what the rules show, never what to do."""

from __future__ import annotations

import html
import json
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
FLAG_REMINDER_DAYS = 7          # while flags stay open and nothing is new, remind once a week
FLAG_MAX_PER_SECTION = 25       # the email lists this many stocks per section, then points to Management for the rest
FLAG_CODES = ("EXIT", "TRIM", "BUY")   # BUY is stored for "Add rules met"
FLAG_SECTIONS = [("EXIT", "Exit rule triggered"), ("TRIM", "Trim rule triggered"), ("BUY", "Add rules met")]
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


def set_prefs(u, user_id: int, *, digest_email: bool, digest_day: int, exit_email: bool, results_alerts: bool, flag_email: bool | None = None) -> None:
    get_prefs(u, user_id)
    day = digest_day if isinstance(digest_day, int) and 0 <= digest_day <= 6 else 6
    u.execute("""UPDATE alert_prefs SET digest_email = ?, digest_day = ?, exit_email = ?, results_alerts = ?, updated_at = ?,
                 consent_at = CASE WHEN (? OR ?) AND consent_at IS NULL THEN ? ELSE consent_at END WHERE user_id = ?""",
              (int(digest_email), day, int(exit_email), int(results_alerts), _now_utc(), int(digest_email), int(exit_email), _now_utc(), user_id))
    if flag_email is not None:
        u.execute("UPDATE alert_prefs SET flag_email = ? WHERE user_id = ?", (int(flag_email), user_id))
    u.commit()


def unsubscribe(u, token: str) -> bool:
    """Switch every alert email off for the owner of this token. True when the token was valid."""
    if not token:
        return False
    cur = u.execute("UPDATE alert_prefs SET digest_email = 0, exit_email = 0, flag_email = 0, updated_at = ? WHERE token = ?", (_now_utc(), token))
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


def flag_candidates(u) -> dict[int, list[dict]]:
    """Every user's tracked stocks whose CURRENT status is Exit, Trim or Add rules met, for users who have not switched the email off (no preferences row
    means on). {user_id: [{symbol, name, code, label, reason, since}]}, Exit first. 'Not enough data' is not a trigger."""
    rows = u.execute("""SELECT h.user_id, h.code, h.label, h.reasons_json, h.computed_at since, tp.id pid, tp.symbol, tp.company_name, tp.anchor_type
                        FROM position_status_history h
                        JOIN (SELECT position_id, MAX(id) mid FROM position_status_history GROUP BY position_id) x ON x.mid = h.id
                        JOIN tracked_positions tp ON tp.id = h.position_id AND tp.status = 'active'
                        LEFT JOIN alert_prefs p ON p.user_id = h.user_id
                        WHERE h.code IN ('EXIT', 'TRIM', 'BUY') AND h.label != ? AND COALESCE(p.flag_email, 1) = 1""", (NO_DATA,)).fetchall()
    out: dict[int, list[dict]] = {}
    order = {c: i for i, (c, _) in enumerate(FLAG_SECTIONS)}
    for r in rows:
        try:
            reasons = [x["text"] for x in json.loads(r["reasons_json"] or "[]") if x.get("kind") != "note"]
        except ValueError:
            reasons = []
        out.setdefault(r["user_id"], []).append({"pid": r["pid"], "symbol": r["symbol"], "name": r["company_name"], "code": r["code"], "label": r["label"],
                                                 "reason": (reasons[0] if reasons else ""), "since": r["since"], "kind": r["anchor_type"]})
    for items in out.values():
        items.sort(key=lambda i: (order[i["code"]], i["symbol"]))
    return out


def flag_email(items: list[dict], new_ids: set[int], token: str, reminder: bool) -> tuple[str, str, str]:
    """ONE email for one user: every flagged stock, grouped Exit, Trim, Add, with the new ones marked. States what each rule shows, never what to do."""
    base = _base_url()
    n = len(items)
    n_new = sum(1 for i in items if i["pid"] in new_ids)
    subject = (f"Your tracked stocks: {n} rule trigger{'' if n == 1 else 's'}" + (f", {n_new} new" if n_new and n_new != n else "")) if not reminder else \
              f"Reminder: {n} of your tracked stocks still have a rule triggered"
    blocks_html, blocks_text = [], []
    for code, title in FLAG_SECTIONS:
        sec = [i for i in items if i["code"] == code]
        if not sec:
            continue
        shown = sec[:FLAG_MAX_PER_SECTION]
        lis = "".join(
            f'<li style="margin:0 0 9px"><a href="{base}/management/{i["pid"]}" style="font-weight:600;text-decoration:none">{html.escape(i["symbol"])}</a> '
            f'<span style="color:#555">{html.escape(i["name"] or "")}</span>'
            + (' <span style="background:#e8f0ea;color:#1d5c3a;border-radius:4px;padding:1px 6px;font-size:12px">new</span>' if i["pid"] in new_ids else "")
            + (f'<br><span style="color:#444;font-size:14px">{html.escape(i["reason"][:200])}</span>' if i["reason"] else "") + "</li>" for i in shown)
        more = f'<p style="margin:0 0 14px;color:#555;font-size:14px">and {len(sec) - len(shown)} more: <a href="{base}/management">see them all in Management</a></p>' if len(sec) > len(shown) else ""
        blocks_html.append(f'<h3 style="font-size:15px;margin:18px 0 8px">{html.escape(title)} ({len(sec)})</h3><ul style="margin:0 0 6px;padding-left:18px">{lis}</ul>{more}')
        blocks_text.append(f"{title} ({len(sec)})\n" + "\n".join(f"- {i['symbol']} {i['name'] or ''}{' [new]' if i['pid'] in new_ids else ''}: {i['reason'][:200]}\n  {base}/management/{i['pid']}" for i in shown)
                           + (f"\n  ...and {len(sec) - len(shown)} more: {base}/management" if len(sec) > len(shown) else ""))
    intro = ("These tracked stocks have a rule of yours triggered, as of last night's check. Each line says where the stock stands against the rules you set." if not reminder else
             "These are still triggered. Nothing new since your last email; this is a weekly reminder while they stay open.")
    body = f'<p style="margin:0 0 4px;color:#333">{html.escape(intro)}</p>' + "".join(blocks_html)
    unsub = f"{base}/alerts/unsubscribe?token={token}"
    page = (f'<div style="font-family:system-ui,Arial,sans-serif;max-width:600px;font-size:15px;line-height:1.45;color:#1a1a1a">'
            f'<h2 style="font-size:18px;margin:0 0 10px">{html.escape("Your rules, applied to your tracked stocks")}</h2>{body}'
            f'<hr style="border:none;border-top:1px solid #ddd;margin:20px 0">'
            f'<p style="font-size:12.5px;color:#666;margin:0 0 8px">{html.escape(LINE)}</p>'
            f'<p style="font-size:12.5px;color:#666;margin:0">You get this because you track stocks and set rules in Tenetfolio. It is sent at most once a day, and only when a rule is triggered. '
            f'<a href="{base}/profile#alerts">Change what you receive</a> or <a href="{html.escape(unsub)}">turn these emails off</a>.</p></div>')
    text = f"{intro}\n\n" + "\n\n".join(blocks_text) + f"\n\n{LINE}\nTurn these emails off: {unsub}\n"
    return subject, page, text


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
    out = {"immediate": 0, "digests": 0, "flags": 0, "skipped_cap": 0}
    with get_users_conn() as u:
        sent_today = u.execute("SELECT COUNT(*) FROM email_log WHERE ok = 1 AND sent_at >= ?", (day_start,)).fetchone()[0]

        def log(user_id, kind, ok):
            u.execute("INSERT INTO email_log (user_id, kind, ok) VALUES (?, ?, ?)", (user_id, kind, int(ok)))

        if now.hour >= DIGEST_HOUR_IST:   # the nightly status check ran the evening before; the email goes out in the morning
            week_ago = (now - timedelta(days=FLAG_REMINDER_DAYS)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            for uid, items in flag_candidates(u).items():
                p = get_prefs(u, uid)
                last = p["last_flag_email_at"]
                if last and last >= day_start:
                    continue                                   # at most one a day
                new_ids = {i["pid"] for i in items if not last or i["since"] > last}
                reminder = not new_ids
                if reminder and last and last > week_ago:
                    continue                                   # nothing new, and told them within the week
                if sent_today >= DAILY_EMAIL_CAP:
                    out["skipped_cap"] += 1
                    continue
                to = _address(u, uid)
                if not to:
                    continue
                subject, h, t = flag_email(items, new_ids, p["token"], reminder)
                ok = send(to, subject, h, t, {"List-Unsubscribe": f"<{_base_url()}/alerts/unsubscribe?token={p['token']}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
                log(uid, "flags", ok)
                if ok:
                    u.execute("UPDATE alert_prefs SET last_flag_email_at = ? WHERE user_id = ?", (_now_utc(), uid))
                    sent_today += 1
                    out["flags"] += 1

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
