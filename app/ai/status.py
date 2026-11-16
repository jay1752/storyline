"""The status of a tracked stock against the user's OWN rules, decided in code.

The model used to choose BUY / HOLD / TRIM / EXIT / WATCH. Now this module does, from the rule checks `evaluate_framework` already
computes (the user's thresholds), the user's sizing numbers and the user's journal. The model only writes the explanation. Every status
comes with the reasons that produced it, a missing figure is never guessed (the rule is "not judged" and said so), and nothing here
calls an AI, so the status is there even when AI credit is out.

Status codes are the stored ones (BUY/HOLD/TRIM/EXIT/WATCH) so history and old stories keep working; the words shown are CODE_LABEL.

Mapping, in order (first match wins):
  EXIT   owned stock: an immediate-exit fact in the data (exact promoter pledge at or above the user's limit), or an exit rule below
         confirmed over two consecutive quarters.
  TRIM   owned stock: an exit rule failing for one quarter (the quarter before was within it), the position above the user's largest
         size, or its sector above the user's sector limit while this position is at least the size a trim aims for.
         An exit rule that is below its limit on an annual figure cannot be told quarter by quarter: it is shown as a note, never a trigger.
  NODATA too few rules could be judged. Shown as "Not enough data"; stored as WATCH.
  BUY    no enabled rule fails, enough rules judged, the sales-growth evidence the user requires is there, and (owned) no lot bought
         already this quarter when the user's one-lot-per-quarter rule is on.
  HOLD   owned stock, anything else.   WATCH   watchlist stock, anything else.

The exit rules, with the user's own thresholds where they have one: ROCE (ROE for banks) below the rule, sales growth negative, operating
margin more than 500 bps under its 8-quarter peak, operating cash flow ÷ profit below the rule, debt / equity above the rule.
Headline-based Speed 1 events (auditor change, SEBI action, fraud) and Speed 3 are not numbers: they stay in the written story and
never change this status."""

from __future__ import annotations

import json
import logging
from datetime import date

CODE_LABEL = {"BUY": "Add rules met", "HOLD": "Within your rules", "TRIM": "Trim rule triggered", "EXIT": "Exit rule triggered",
              "WATCH": "Not yet meeting your rules"}
NO_DATA_LABEL = "Not enough data"
MIN_JUDGED = 6        # rules that must be judgeable before "all pass" or "within" means much (financials have fewer applicable rules than 7)
OPM_DROP_BPS = 500    # operating-margin compression from the 8-quarter peak that counts as a Speed 2 rule (framework constant)
PLEDGE_FLAG_NOTE = "only 'pledged"   # evaluate_framework's note when only a pledged / no-pledge flag is stored, not the exact %


def _by_key(filters) -> dict:
    return {f["key"]: f for f in (filters or [])}


def _same_source(c: dict, p: dict) -> bool:
    """True when the earlier quarter's figure is the very same number (an annual figure used for both quarters): not a second quarter."""
    return c.get("raw") is not None and p.get("raw") is not None and c["raw"] == p["raw"]


def _threshold_item(label: str, key: str, cur: dict, prev: dict) -> dict | None:
    """One exit rule that is a threshold the user sets. None = not judged (rule off, or no figure)."""
    c = cur.get(key)
    if not c or c["status"] in ("off", "unknown"):
        return None
    if c["status"] != "fail":
        return {"rule": label, "state": "clear", "note": None}
    p = prev.get(key)
    where = f"{c['value']} against your rule {c['threshold']}"
    if p and p["status"] == "fail" and not _same_source(c, p):
        return {"rule": label, "state": "confirmed", "note": f"{where}, and below it in the quarter before"}
    if p and p["status"] == "pass":
        return {"rule": label, "state": "warning", "note": f"{where}; the quarter before was within it"}
    # Same figure for both quarters (an annual number) or no earlier figure: below the rule, but quarter by quarter it cannot be told.
    # Shown to the user, never a trigger by itself.
    why = "the figure is annual, so two quarters cannot be told apart" if p and p["status"] == "fail" else "no figure for the quarter before to compare"
    return {"rule": label, "state": "unconfirmed", "note": f"{where}; {why}"}


def speed_two(filters, prev_filters, facts, prev_facts) -> list[dict]:
    """The Speed 2 exit rules that can be judged, each clear / warning (one quarter or not confirmable) / confirmed (two quarters)."""
    cur, prev = _by_key(filters), _by_key(prev_filters)
    items: list[dict] = []
    for key, label in (("roce", "ROCE"), ("roe", "ROE")):
        if key in cur:
            items.append(_threshold_item(label, key, cur, prev))
    items.append(_threshold_item("Operating cash flow ÷ profit", "cfo_pat", cur, prev))
    items.append(_threshold_item("Debt / equity", "de", cur, prev))

    sg = cur.get("sales_growth")
    if sg and sg["status"] != "off" and any(g is not None for g in (facts or {}).get("growth_last_4q") or []):
        n = (facts or {}).get("consecutive_quarters_negative_sales_growth") or 0
        items.append({"rule": "Sales growth negative", "state": "confirmed" if n >= 2 else "warning" if n == 1 else "clear",
                      "note": f"sales fell year on year in {n} quarter{'s' if n != 1 else ''} running" if n else None})
    om = cur.get("opm")
    drop = (facts or {}).get("opm_drop_from_peak_bps")
    if om and om["status"] != "off" and drop is not None:
        if drop > OPM_DROP_BPS:
            pdrop = (prev_facts or {}).get("opm_drop_from_peak_bps")
            items.append({"rule": "Operating margin compression", "state": "confirmed" if pdrop is not None and pdrop > OPM_DROP_BPS else "warning",
                          "note": f"operating margin is {drop:,} bps under its 8-quarter peak (the rule is {OPM_DROP_BPS} bps)"})
        else:
            items.append({"rule": "Operating margin compression", "state": "clear", "note": None})
    return [i for i in items if i]


def speed_one(filters) -> dict:
    """Immediate-exit facts that are numbers. Only an exact pledge % counts: a stored 'pledged' flag alone is not a figure."""
    p = _by_key(filters).get("pledge")
    if p and p["status"] == "fail" and p.get("raw") is not None and not (p.get("note") or "").startswith(PLEDGE_FLAG_NOTE):
        age = f" ({p['note']})" if (p.get("note") or "").startswith("carried from") else ""
        return {"status": "triggered", "note": f"promoter pledge is {p['value']}, not below your {p['limit']:g}% limit{age}"}
    return {"status": "clear", "note": None}


def compute_status(filters, prev_filters, facts, prev_facts, *, owned: bool, weight_pct=None, sector_pct=None, sector: str | None = None,
                   sz: dict | None = None, last_lot_date: str | None = None, today: date | None = None) -> dict:
    """The status and why. `filters`/`facts` come from evaluate_framework for the latest quarter, `prev_*` from the same call run on the
    quarters before the latest (None when there is only one). `sz` = the user's sizing numbers (framework.sizing_plan)."""
    from app.framework import sizing_defaults
    sz = {**sizing_defaults(), **(sz or {})}
    today = today or date.today()
    filters = filters or []

    live = [f for f in filters if f["status"] != "off"]
    judged = [f for f in live if f["status"] in ("pass", "fail")]
    failing = [f for f in judged if f["status"] == "fail"]
    applicable = [f for f in live if f.get("value") != "n/a"]
    need = min(MIN_JUDGED, len(applicable))
    enough = len(judged) >= max(need, 1)

    s1 = speed_one(filters)
    s2_items = speed_two(filters, prev_filters, facts, prev_facts)
    confirmed = [i for i in s2_items if i["state"] == "confirmed"]
    warned = [i for i in s2_items if i["state"] == "warning"]
    unconfirmed = [i for i in s2_items if i["state"] == "unconfirmed"]
    s2 = {"status": "two_quarter_confirmed" if confirmed else "one_quarter_warning" if warned else "clear", "items": s2_items}

    reasons: list[dict] = []

    def why(kind, text):
        reasons.append({"kind": kind, "text": text})

    code = None
    if owned and s1["status"] == "triggered":
        code = "EXIT"
        why("speed1", f"Your immediate-exit rule is triggered: {s1['note']}.")
    if owned and confirmed:
        code = "EXIT"
        for i in confirmed:
            why("speed2", f"Your exit rule on {i['rule']} is confirmed over two quarters: {i['note']}.")

    if code is None and owned:
        trim = False
        for i in warned:
            trim = True
            why("speed2", f"Your exit rule on {i['rule']} is failing for one quarter: {i['note']}. A second quarter would confirm it.")
        if weight_pct is not None and weight_pct > sz["size_max"]:
            trim = True
            why("size", f"This is {weight_pct:.1f}% of your book. Your rule: above {sz['size_max']:g}%, trim back toward {sz['size_target']:g}%.")
        if sector_pct is not None and sector_pct > sz["sector_max"]:
            text = f"{sector or 'This sector'} is {sector_pct:.1f}% of your book. Your largest share for one sector is {sz['sector_max']:g}%."
            if weight_pct is not None and weight_pct >= sz["size_target"]:
                trim = True
                why("sector", text + f" This position is {weight_pct:.1f}%, at or above the {sz['size_target']:g}% a trim aims for.")
            else:
                why("note", text + " This position is small, so it is not what drives it.")
        if trim:
            code = "TRIM"

    no_data = False
    if code is None and not enough:
        no_data = True
        code = "WATCH"
        if not live:
            why("data", "None of your rules are switched on, so there is nothing to judge this stock against.")
        else:
            why("data", f"Only {len(judged)} of your rules could be judged with the data we hold; at least {max(need, 1)} are needed for a status.")

    if code is None:
        blockers: list[str] = []
        if failing:
            blockers.append(f"{len(failing)} rule{'s' if len(failing) != 1 else ''} not met ({', '.join(f['label'] for f in failing[:4])}{'…' if len(failing) > 4 else ''})")
        sg = _by_key(filters).get("sales_growth")
        n_ev = int(sz["evidence_quarters"])
        growth = [g for g in (facts or {}).get("growth_last_4q") or [] if g is not None]
        if sg and sg["status"] != "off" and not failing and n_ev > len(growth):
            blockers.append(f"your rules need {n_ev} quarters of sales-growth evidence and we hold {len(growth)}")
        if owned and sz["lot_gap"] and last_lot_date:
            try:
                d = date.fromisoformat(last_lot_date)
                if (d.year, (d.month - 1) // 3) == (today.year, (today.month - 1) // 3):
                    blockers.append(f"you already bought a lot this quarter ({last_lot_date}) and your rule is one lot per quarter")
            except ValueError:
                pass
        if not blockers:
            code = "BUY"
            why("add", f"All {len(judged)} rules that could be judged are met"
                       f"{'' if not owned else ', you hold room under your size limit'}. "
                       f"{len([f for f in live if f['status'] == 'unknown'])} could not be judged for lack of data.")
        elif failing:
            code = "HOLD" if owned else "WATCH"
            why("entry", "Not met: " + "; ".join(f"{f['label']} {f['value'] or 'unknown'} against your rule {f['threshold']}" for f in failing[:6])
                + (". No exit rule is triggered." if owned else "."))
        else:
            code = "HOLD" if owned else "WATCH"
            why("add_blocked", "Your add rules are not yet met: " + "; ".join(blockers) + ".")

    for i in unconfirmed:
        why("note", f"Below your rule but not a trigger: {i['rule']}, {i['note']}.")
    unknown = [f["label"] for f in live if f["status"] == "unknown"]
    label = NO_DATA_LABEL if no_data else CODE_LABEL[code]
    return {
        "code": code, "label": label, "reasons": reasons, "enough_data": enough, "owned": owned,
        "rules": {"judged": len(judged), "passing": len(judged) - len(failing), "failing": [f["label"] for f in failing], "unknown": unknown},
        "speed1": s1, "speed2": s2, "weight_pct": weight_pct, "sector_pct": sector_pct,
    }


# ---------------------------------------------------------------------------------------------------- one stock from the databases

def book_weights(u, user_id: int) -> dict:
    """Each held stock's share of the user's book and each sector's share (%), at latest prices (USD stocks at the live rate), the
    same maths as the story engine's position weight. {"by_symbol": {...}, "by_sector": {...}}."""
    from app import fx
    rate = fx.rate()
    held, total = [], 0.0
    for p in u.execute("""SELECT tp.symbol, tp.quantity, s.sector FROM tracked_positions tp LEFT JOIN market.stocks s ON s.symbol = tp.symbol
                          WHERE tp.user_id = ? AND tp.anchor_type = 'position' AND tp.status = 'active' AND tp.quantity IS NOT NULL""", (user_id,)):
        row = u.execute("SELECT close FROM market.price_history WHERE symbol = ? ORDER BY date DESC LIMIT 1", (p["symbol"],)).fetchone()
        if not row:
            continue
        value = p["quantity"] * row["close"] * (1.0 if p["symbol"].startswith("NSE:") else rate)
        held.append((p["symbol"], p["sector"] or "Unclassified", value))
        total += value
    if not total:
        return {"by_symbol": {}, "by_sector": {}}
    by_symbol, by_sector = {}, {}
    for sym, sec, v in held:
        by_symbol[sym] = by_symbol.get(sym, 0.0) + v / total * 100
        by_sector[sec] = by_sector.get(sec, 0.0) + v / total * 100
    return {"by_symbol": by_symbol, "by_sector": by_sector}


def compute_for_position(u, pos, book: dict | None = None) -> dict:
    """The status of one tracked stock from stored data and the user's CURRENT rules, no AI. `u` = a users-DB connection with market
    attached; `pos` a tracked_positions row. Pass `book` (book_weights) when doing many stocks of one user."""
    from app import framework as framework_rules
    from app.ai.checklist import evaluate_framework
    from app.annual import annual_rows
    from app.db import get_market_conn
    from app.journal import entries as journal_entries
    from app.routes.analytics import _yoy

    sym, uid = pos["symbol"], pos["user_id"]
    owned = pos["anchor_type"] == "position"
    stock = u.execute("SELECT sector FROM market.stocks WHERE symbol = ?", (sym,)).fetchone()
    allq = u.execute("SELECT * FROM market.quarterly_data WHERE symbol = ? ORDER BY period_end_date", (sym,)).fetchall()
    quarterly = [r for r in allq if "annual" not in (r["quarter_label"] or "").lower()]
    if not stock or not quarterly:
        r = compute_status([], None, {}, None, owned=owned)
        r["reasons"] = [{"kind": "data", "text": "No reported quarterly results are stored for this stock yet, so there is nothing to judge."}]
        return r
    by_end = {r["period_end_date"]: r for r in quarterly}
    rows = []
    for r in quarterly[-8:]:
        d = dict(r)
        if d.get("revenue_growth_pct") is None:
            d["revenue_growth_pct"] = _yoy(r, by_end, stored=False)
        rows.append(d)
    with get_market_conn() as m:
        annual = annual_rows(m, sym)
    th = framework_rules.resolve(u, uid, sym)
    sz = framework_rules.sizing_plan(u, uid)
    now = evaluate_framework(rows, stock["sector"], th, annual=annual)
    before = evaluate_framework(rows[:-1], stock["sector"], th, annual=annual) if len(rows) >= 2 else {"filters": [], "facts": None}

    weight = sector_pct = None
    if owned:
        book = book or book_weights(u, uid)
        weight = book["by_symbol"].get(sym)
        weight = None if weight is None else round(weight, 1)
        sec = stock["sector"] or "Unclassified"
        sector_pct = book["by_sector"].get(sec)
        sector_pct = None if sector_pct is None else round(sector_pct, 1)
    lots = [e["event_date"] for e in journal_entries(u, pos["id"]) if e["event_type"] in ("open", "add")]
    result = compute_status(now["filters"], before["filters"], now["facts"], before["facts"], owned=owned, weight_pct=weight,
                            sector_pct=sector_pct, sector=stock["sector"], sz=sz, last_lot_date=max(lots) if lots else None)
    result["rules_hash"] = _rules_hash(th, sz)
    return result


def _rules_hash(th: dict, sz: dict) -> str:
    """A short fingerprint of the user's rule settings, so a change of status can be told apart: new data, or the user edited a rule."""
    import hashlib
    items = sorted((k, v["value"], bool(v["enabled"])) for k, v in th.items()) + sorted(sz.items())
    return hashlib.md5(json.dumps(items, default=str).encode()).hexdigest()[:10]


# ---------------------------------------------------------------------------------------------------- stored history

def _basis(r: dict) -> str:
    """What makes two results the same status: the code, the speed states per rule and the kinds of reasons. Not the numbers, so a
    weight moving from 9.7% to 9.8% does not start a new history row."""
    s2 = ",".join(f"{i['rule']}={i['state']}" for i in r["speed2"]["items"] if i["state"] != "clear")
    kinds = ",".join(sorted({x["kind"] for x in r["reasons"] if x["kind"] != "note"}))
    return f"{r['code']}|{r['label']}|{r['speed1']['status']}|{s2}|{kinds}"


def record(u, position_id: int, user_id: int, result: dict) -> bool:
    """Keep the status in position_status_history: a new row only when the status (or why) changes, otherwise the latest row is
    refreshed in place (its reasons and when it was last confirmed). Returns True when a new row was added. Commit is the caller's."""
    detail = {k: result[k] for k in ("rules", "speed1", "speed2", "weight_pct", "sector_pct", "enough_data")}
    detail["basis"] = _basis(result)
    detail["rules_hash"] = result.get("rules_hash")
    last = u.execute("SELECT id, code, label, detail_json FROM position_status_history WHERE position_id = ? ORDER BY id DESC LIMIT 1", (position_id,)).fetchone()
    same = False
    if last:
        try:
            same = json.loads(last["detail_json"] or "{}").get("basis") == detail["basis"]
        except ValueError:
            same = False
    if same:
        u.execute("UPDATE position_status_history SET reasons_json = ?, detail_json = ?, checked_at = CURRENT_TIMESTAMP WHERE id = ?",
                  (json.dumps(result["reasons"]), json.dumps(detail), last["id"]))
        return False
    cur = u.execute("INSERT INTO position_status_history (position_id, user_id, code, label, reasons_json, detail_json) VALUES (?,?,?,?,?,?)",
                    (position_id, user_id, result["code"], result["label"], json.dumps(result["reasons"]), json.dumps(detail)))
    if last:  # an earlier status existed: tell the user it changed (never from the very first status, and never so it can break the status itself)
        try:
            from app import alerts
            try:
                prev_detail = json.loads(last["detail_json"] or "{}")
            except ValueError:
                prev_detail = {}
            alerts.on_status_change(u, user_id, position_id, {"code": last["code"], "label": last["label"], "detail": prev_detail}, cur.lastrowid, result, detail)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("could not create the alert for position %s", position_id)
    return True


def stored(u, position_id: int) -> dict | None:
    """The latest stored status of a position as {code, label, reasons, since, checked_at, detail}, or None."""
    r = u.execute("SELECT * FROM position_status_history WHERE position_id = ? ORDER BY id DESC LIMIT 1", (position_id,)).fetchone()
    return _row(r)


def _row(r) -> dict | None:
    if not r:
        return None
    return {"code": r["code"], "label": r["label"], "reasons": json.loads(r["reasons_json"] or "[]"), "since": r["computed_at"],
            "checked_at": r["checked_at"], "detail": json.loads(r["detail_json"] or "{}")}


def stored_many(u, user_id: int) -> dict:
    """{position_id: stored status} for one user's positions, one query."""
    rows = u.execute("""SELECT h.* FROM position_status_history h
                        JOIN (SELECT position_id, MAX(id) mid FROM position_status_history WHERE user_id = ? GROUP BY position_id) m ON m.mid = h.id""",
                     (user_id,)).fetchall()
    return {r["position_id"]: _row(r) for r in rows}


def history(u, position_id: int, limit: int = 20) -> list[dict]:
    """The changes of status, newest first: [{code, label, since, reasons}]."""
    return [{"code": r["code"], "label": r["label"], "since": r["computed_at"], "reasons": json.loads(r["reasons_json"] or "[]")}
            for r in u.execute("SELECT * FROM position_status_history WHERE position_id = ? ORDER BY id DESC LIMIT ?", (position_id, limit))]


def refresh_user(u, user_id: int) -> int:
    """Recompute and record the status of every active tracked stock of one user, no AI. Returns how many changed."""
    book = book_weights(u, user_id)
    changed = 0
    for pos in u.execute("SELECT * FROM tracked_positions WHERE user_id = ? AND status = 'active'", (user_id,)).fetchall():
        try:
            if record(u, pos["id"], user_id, compute_for_position(u, pos, book)):
                changed += 1
        except Exception:  # noqa: BLE001 - one stock with odd data must not stop the rest
            logging.getLogger(__name__).exception("status: could not compute %s for user %s", pos["symbol"], user_id)
    u.commit()
    return changed


def refresh_all() -> tuple[int, int]:
    """Every user with active tracked stocks. Returns (users, status changes)."""
    from app.db import get_users_conn
    users = changes = 0
    with get_users_conn() as u:
        ids = [r["user_id"] for r in u.execute("SELECT DISTINCT user_id FROM tracked_positions WHERE status = 'active'")]
        for uid in ids:
            changes += refresh_user(u, uid)
            users += 1
    return users, changes
