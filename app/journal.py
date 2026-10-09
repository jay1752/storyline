"""The user's optional journal for a tracked stock: adding more, trimming, exiting, or a plain note.

Entries are applied to the position right away (so Management, Portfolio and the analysis all see the new size and
average cost) and record the position's previous state so the latest entry can be undone exactly. This module also
computes the *rule-based* story flags — the framework rules the user's own actions can break — deterministically,
from their dates and prices; the AI adds judgement on top of these but never replaces them."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
TYPES = ("add", "trim", "exit", "note")
MAX_NOTE = 1000
_STATE_COLS = ("anchor_type", "anchor_date", "buy_price", "quantity", "status", "closed_at", "exit_price")


class JournalError(ValueError):
    """A user-correctable problem with an entry; the message is shown as-is."""


def today_ist() -> date:
    return datetime.now(IST).date()


def _quarter(d: str) -> tuple[int, int]:
    x = date.fromisoformat(d)
    return x.year, (x.month - 1) // 3 + 1


def _state(pos) -> dict:
    return {c: pos[c] for c in _STATE_COLS}


def add_entry(u, user_id: int, position_id: int, etype: str, event_date: str, quantity, price, note: str | None) -> int:
    """Validate and apply one journal entry. Caller commits. Raises JournalError for anything the user can fix."""
    pos = u.execute("SELECT * FROM tracked_positions WHERE id = ? AND user_id = ? AND status = 'active'", (position_id, user_id)).fetchone()
    if not pos:
        raise JournalError("That stock isn't an active tracked stock of yours.")
    if etype not in TYPES:
        raise JournalError("Choose what you did: added more, trimmed, exited, or a note.")
    try:
        when = date.fromisoformat(event_date)
    except (TypeError, ValueError):
        raise JournalError("Enter a valid date.")
    if when > today_ist():
        raise JournalError("That date is in the future.")
    note = (note or "").strip() or None
    if note and len(note) > MAX_NOTE:
        raise JournalError(f"Notes are limited to {MAX_NOTE} characters.")

    qty_now, avg_now = pos["quantity"], pos["buy_price"]
    qty = price_v = None
    if etype != "note":
        try:
            price_v = float(price)
        except (TypeError, ValueError):
            raise JournalError("Enter the price per share.")
        if price_v <= 0:
            raise JournalError("Price must be more than zero.")
    elif not note:
        raise JournalError("Write the note first.")

    if etype in ("add", "trim"):
        try:
            qty = float(quantity)
        except (TypeError, ValueError):
            raise JournalError("Enter how many shares.")
        if qty <= 0:
            raise JournalError("Shares must be more than zero.")
    if etype == "trim":
        if not qty_now:
            raise JournalError("There's no recorded quantity to trim from — add the shares you hold first.")
        if qty >= qty_now - 1e-9:
            raise JournalError(f"That's your whole position ({qty_now:g} shares) — record it as 'Exited' instead.")
    if etype == "exit":
        if qty_now:
            qty = float(qty_now)
        elif quantity not in (None, ""):
            qty = float(quantity)

    # The first entry freezes today's position as an 'open' baseline, so the story reads from where the user started.
    has_events = u.execute("SELECT 1 FROM position_events WHERE position_id = ? LIMIT 1", (position_id,)).fetchone()
    if not has_events and qty_now and pos["anchor_type"] == "position":
        u.execute("INSERT INTO position_events (position_id, user_id, event_type, event_date, quantity, price, note) VALUES (?,?,?,?,?,?,?)",
                  (position_id, user_id, "open", pos["anchor_date"], qty_now, avg_now, "Position as it was when you started the journal"))

    before = json.dumps(_state(pos))
    if etype == "add":
        base_qty = qty_now or 0.0
        new_qty = base_qty + qty
        new_avg = ((base_qty * (avg_now if avg_now else price_v)) + qty * price_v) / new_qty
        converting = pos["anchor_type"] == "watchlist" or not base_qty
        u.execute("UPDATE tracked_positions SET quantity = ?, buy_price = ?, anchor_type = 'position', anchor_date = CASE WHEN ? THEN ? ELSE anchor_date END WHERE id = ?",
                  (new_qty, round(new_avg, 4), 1 if converting else 0, event_date, position_id))
    elif etype == "trim":
        u.execute("UPDATE tracked_positions SET quantity = ? WHERE id = ?", (qty_now - qty, position_id))
    elif etype == "exit":
        u.execute("UPDATE tracked_positions SET status = 'exited', closed_at = ?, exit_price = ? WHERE id = ?", (event_date, price_v, position_id))

    cur = u.execute(
        "INSERT INTO position_events (position_id, user_id, event_type, event_date, quantity, price, note, before_json) VALUES (?,?,?,?,?,?,?,?)",
        (position_id, user_id, etype, event_date, qty, price_v, note, before))
    return cur.lastrowid


def undo_last(u, user_id: int, position_id: int) -> dict | None:
    """Reverse the most recent entry for this stock (restoring the stored previous state). Returns the removed entry."""
    pos = u.execute("SELECT id FROM tracked_positions WHERE id = ? AND user_id = ?", (position_id, user_id)).fetchone()
    if not pos:
        return None
    last = u.execute("SELECT * FROM position_events WHERE position_id = ? AND event_type != 'open' ORDER BY id DESC LIMIT 1", (position_id,)).fetchone()
    if not last:
        return None
    if last["before_json"]:
        before = json.loads(last["before_json"])
        sets = ", ".join(f"{c} = ?" for c in before)
        u.execute(f"UPDATE tracked_positions SET {sets} WHERE id = ?", (*before.values(), position_id))
    u.execute("DELETE FROM position_events WHERE id = ?", (last["id"],))
    if not u.execute("SELECT 1 FROM position_events WHERE position_id = ? AND event_type != 'open' LIMIT 1", (position_id,)).fetchone():
        u.execute("DELETE FROM position_events WHERE position_id = ? AND event_type = 'open'", (position_id,))
    return dict(last)


def entries(u, position_id: int) -> list[dict]:
    rows = u.execute("SELECT * FROM position_events WHERE position_id = ? ORDER BY event_date, id", (position_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------- rule-based story flags

def rule_flags(events: list[dict], pos, quarters: list[dict], latest_price: float | None, weight_pct: float | None,
               prior_analyses: list[dict], sz: dict | None = None, growth_bar: float = 30.0) -> list[dict]:
    """Framework rules the user's own actions can break, computed from their entries. Each flag:
    {level: ok|watch|warning|alert, text, source: 'rule'}.

    quarters: quarterly rows (dict-like, oldest first) with period_end_date / revenue_growth_pct.
    prior_analyses: earlier analysis dicts (newest first) so a missed exit can be spotted.
    sz: the user's sizing / discipline numbers (framework.sizing_plan; built-in defaults when omitted); growth_bar: their sales-growth rule."""
    from app.framework import sizing_defaults
    sz = {**sizing_defaults(), **(sz or {})}
    flags: list[dict] = []

    def flag(level, text, kind):
        flags.append({"level": level, "text": text, "source": "rule", "kind": kind})

    lots = [e for e in events if e["event_type"] in ("open", "add")]
    adds = [e for e in events if e["event_type"] == "add"]

    # 1) Never buy a 2nd lot in the same quarter as the 1st.
    if sz["lot_gap"] and len(lots) >= 2:
        first_q = _quarter(lots[0]["event_date"])
        same = [e for e in lots[1:] if _quarter(e["event_date"]) == first_q]
        if same:
            flag("warning", f"You bought another lot on {same[0]['event_date']} in the same quarter as your first ({lots[0]['event_date']}). "
                            "Your rule: no second lot in the same quarter as the first.", "same_quarter")

    # 2) Averaging down: more than once is a breach.
    running_qty, running_cost, down = 0.0, 0.0, 0
    for e in lots:
        if e["event_type"] == "add" and running_qty and e["price"] and running_cost / running_qty > e["price"]:
            down += 1
        running_qty += e["quantity"] or 0
        running_cost += (e["quantity"] or 0) * (e["price"] or 0)
    allowed = int(sz["avg_down_max"])
    if down > allowed:
        flag("warning", f"You've averaged down {down} times. Your rule: at most {allowed} until the first lot shows a profit.", "averaging")
    elif down and down == allowed:
        flag("watch", f"{'One' if down == 1 else down} of your adds {'was' if down == 1 else 'were'} below your average cost (averaging down). That is your limit of {allowed} — another is a breach until the first lot is in profit.", "averaging")

    # 3) Evidence before scaling up: N consecutive quarters above the user's sales-growth bar that were public at the time.
    n_ev = int(sz["evidence_quarters"])
    for e in adds:
        known = [q for q in quarters if q["period_end_date"] <= (date.fromisoformat(e["event_date"]) - timedelta(days=45)).isoformat()]
        g = [q.get("revenue_growth_pct") for q in known[-n_ev:]]
        if len(g) == n_ev and all(x is not None for x in g) and not all(x > growth_bar for x in g):
            shown = " and ".join(f"{x:.0f}%" for x in g)
            flag("warning", f"On {e['event_date']} you added shares, but the {n_ev} latest reported quarters then showed sales growth of "
                            f"{shown} — below your {growth_bar:g}% bar. Your rule: no scaling up without {n_ev}-quarter evidence.", "evidence")
            break

    # 4) Position size, against the user's own limits.
    if weight_pct is not None:
        if weight_pct > sz["size_max"]:
            flag("alert", f"This is {weight_pct:.1f}% of your book. Your rule: above {sz['size_max']:g}% trim back toward {sz['size_target']:g}%.", "size")
        elif weight_pct > sz["size_watch"]:
            flag("watch", f"This is {weight_pct:.1f}% of your book — over your {sz['size_watch']:g}% yellow-flag line (trim back to {sz['size_target']:g}% if it keeps growing).", "size")

    # 5) Exit within the user's window after a confirmed two-quarter deterioration.
    for a in reversed(prior_analyses):  # oldest first
        if a.get("speed2", {}).get("status") == "two_quarter_confirmed":
            when = (a.get("generated_at") or "")[:10]
            try:
                days = (today_ist() - date.fromisoformat(when)).days
            except ValueError:
                break
            if days > sz["exit_days"] and pos["status"] == "active" and pos["anchor_type"] == "position":
                flag("alert", f"The analysis confirmed a two-quarter deterioration on {when} ({days} days ago) and you still hold it. "
                              f"Your rule: exit within {sz['exit_days']:g} days of the second confirmation.", "late_exit")
            break
    return flags
