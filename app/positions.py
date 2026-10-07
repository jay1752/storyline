"""Turns raw statement rows (extracted by the chat model) into correct positions.

The model only TRANSCRIBES rows (company, side, qty, price, date) — it never does the
arithmetic. Net quantity and average cost are computed here, deterministically, so a
broker statement with several buys and sells of one stock produces one correct position
instead of whichever row happened to come first (the old per-row upload flow created
one position per row and skipped repeats of the same stock).

Method: weighted-average cost, which is what broker holdings screens show. A buy blends
into the average; a sell reduces quantity and leaves the average untouched; when a
position is fully sold the average resets, so a later re-buy starts a fresh cost basis.
A "holding" row is an as-of snapshot (quantity + average cost straight from the
statement) — transactions dated after it are applied on top, earlier ones are ignored
because the snapshot already includes them.

Nothing here writes a tracked position. propose() stores a pending_imports row;
commit() runs only from the user-click endpoint in app/routes/chat.py, never from an
LLM tool call."""

import json
from dataclasses import dataclass
from datetime import date as date_cls
from typing import Optional

from app.db import get_market_conn, get_users_conn
from app.tracking import build_symbol_lookup, create_tracked_position, resolve_symbol

EPS = 1e-9


@dataclass
class Txn:
    company_text: str
    side: str  # buy | sell | holding
    quantity: Optional[float]
    price: Optional[float]
    date: Optional[str]
    symbol: Optional[str] = None  # set when the caller already resolved it


def _aggregate_symbol(rows: list[Txn], statement_date: Optional[str]):
    """Returns (qty, avg_price, anchor_date, warnings, error). `error` set means this
    symbol can't be turned into a trustworthy position from this file."""
    warnings: list[str] = []
    qty, avg, anchor = 0.0, None, None
    price_missing = False

    holdings = [r for r in rows if r.side == "holding" and r.quantity is not None]
    txns = [r for r in rows if r.side in ("buy", "sell")]

    snapshot_date = None
    if holdings:
        snap = max(holdings, key=lambda r: r.date or statement_date or "")
        qty = snap.quantity
        avg = snap.price
        snapshot_date = snap.date or statement_date
        anchor = snapshot_date
        if avg is None:
            price_missing = True
        if txns:
            if snapshot_date is None:
                warnings.append("Holdings snapshot has no date, so the transactions in this file were ignored.")
                txns = []
            else:
                later = [t for t in txns if t.date and t.date > snapshot_date]
                if len(later) != len(txns):
                    warnings.append(
                        f"Ignored {len(txns) - len(later)} transaction(s) dated on/before the holdings snapshot ({snapshot_date})."
                    )
                txns = later

    # Stable sort keeps the statement's own order for same-day trades; undated rows sort first.
    txns = sorted(txns, key=lambda r: r.date or "")
    for t in txns:
        if t.quantity is None or t.quantity <= 0:
            warnings.append(f"Skipped a {t.side} row with no usable quantity.")
            continue
        if t.side == "buy":
            if t.price is None:
                price_missing = True
            if qty <= EPS:
                qty, avg, anchor = t.quantity, t.price, t.date or statement_date
            else:
                if avg is not None and t.price is not None:
                    avg = (qty * avg + t.quantity * t.price) / (qty + t.quantity)
                else:
                    avg = None
                qty += t.quantity
        else:
            if t.quantity > qty + EPS:
                return None, None, None, warnings, (
                    f"Sells {t.quantity:g} but only {qty:g} held at that point — earlier buys are "
                    "likely missing from this file, so the position can't be determined."
                )
            qty -= t.quantity
            if qty <= EPS:
                qty, avg, anchor = 0.0, None, None

    if price_missing or (qty > EPS and avg is None):
        warnings.append("Average price couldn't be determined from the file — add it manually after import.")
    return qty, avg, anchor, warnings, None


def propose(user_id: int, transactions: list[Txn], statement_date: Optional[str] = None) -> dict:
    with get_market_conn() as m:
        lookup, names = build_symbol_lookup(m)
        valid_symbols = set(names)

    resolved: dict[str, list[Txn]] = {}
    skipped: list[dict] = []
    unmatched_seen: dict[str, dict] = {}
    for t in transactions:
        sym = t.symbol if t.symbol in valid_symbols else None
        candidates: list[str] = []
        if sym is None:
            sym, candidates = resolve_symbol(t.company_text, lookup, names)
        if sym is None:
            entry = unmatched_seen.setdefault(t.company_text, {"count": 0, "candidates": candidates})
            entry["count"] += 1
            continue
        resolved.setdefault(sym, []).append(t)

    for label, info in unmatched_seen.items():
        if info["candidates"]:
            options = ", ".join(f"{c} ({names[c]})" for c in info["candidates"])
            reason = (
                f"Ambiguous — could be {options}. Ask the user which one (don't guess), then "
                "resubmit the full list with the `symbol` field set on these rows."
            )
        else:
            reason = (
                f"No matching stock in our universe ({info['count']} row(s)). Try search_stocks, "
                "then resubmit the full list with the `symbol` field set."
            )
        skipped.append({"label": label, "reason": reason})

    with get_users_conn() as u:
        existing = {
            r["symbol"]: dict(r)
            for r in u.execute(
                "SELECT id, symbol, anchor_type, anchor_date, buy_price, quantity FROM tracked_positions "
                "WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ).fetchall()
        }

    rows = []
    for sym, group in resolved.items():
        qty, avg, anchor, warnings, error = _aggregate_symbol(group, statement_date)
        if error:
            skipped.append({"label": names[sym] + f" ({sym})", "reason": error})
            continue
        if qty <= EPS:
            skipped.append({"label": names[sym] + f" ({sym})", "reason": "Fully sold within this file — nothing left to track."})
            continue
        prior = existing.get(sym)
        if prior is None:
            action = "new"
        elif prior["anchor_type"] == "watchlist":
            action = "convert"
        else:
            action = "update"
        rows.append({
            "symbol": sym,
            "company_name": names[sym],
            "quantity": round(qty, 6),
            "avg_price": round(avg, 4) if avg is not None else None,
            "anchor_date": anchor or statement_date or date_cls.today().isoformat(),
            "action": action,
            "existing": {k: prior[k] for k in ("quantity", "buy_price", "anchor_date", "anchor_type")} if prior else None,
            "txn_count": len(group),
            "warnings": warnings,
        })
    rows.sort(key=lambda r: r["symbol"])

    payload = {"rows": rows, "skipped": skipped, "statement_date": statement_date}
    with get_users_conn() as u:
        # A newer upload supersedes any earlier still-pending proposal — two live
        # confirm cards for overlapping data would let the user commit stale numbers.
        u.execute("UPDATE pending_imports SET status = 'superseded' WHERE user_id = ? AND status = 'pending'", (user_id,))
        cur = u.execute(
            "INSERT INTO pending_imports (user_id, payload_json) VALUES (?, ?)", (user_id, json.dumps(payload))
        )
        proposal_id = cur.lastrowid
        u.commit()
    return {"proposal_id": proposal_id, **payload}


def get_proposal(user_id: int, proposal_id: int) -> Optional[dict]:
    with get_users_conn() as u:
        row = u.execute(
            "SELECT id, status, payload_json FROM pending_imports WHERE id = ? AND user_id = ?",
            (proposal_id, user_id),
        ).fetchone()
    if not row:
        return None
    return {"proposal_id": row["id"], "status": row["status"], **json.loads(row["payload_json"])}


def cancel(user_id: int, proposal_id: int) -> bool:
    with get_users_conn() as u:
        cur = u.execute(
            "UPDATE pending_imports SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'pending'",
            (proposal_id, user_id),
        )
        u.commit()
    return cur.rowcount > 0


def commit(user_id: int, proposal_id: int) -> Optional[dict]:
    """Applies a pending proposal. Returns counts, or None if it isn't pending/owned."""
    proposal = get_proposal(user_id, proposal_id)
    if not proposal or proposal["status"] != "pending":
        return None

    created = updated = 0
    with get_users_conn() as u:
        for r in proposal["rows"]:
            latest = u.execute(
                "SELECT close FROM market.price_history WHERE symbol = ? ORDER BY date DESC LIMIT 1", (r["symbol"],)
            ).fetchone()
            last_close = latest["close"] if latest else None

            if r["action"] == "new":
                create_tracked_position(
                    u, user_id, r["symbol"], r["company_name"], "position",
                    r["anchor_date"], r["avg_price"], r["quantity"],
                )
                created += 1
                continue

            pos = u.execute(
                "SELECT id FROM tracked_positions WHERE user_id = ? AND symbol = ? AND status = 'active'",
                (user_id, r["symbol"]),
            ).fetchone()
            if pos is None:  # deleted between proposal and confirm — treat as new
                create_tracked_position(
                    u, user_id, r["symbol"], r["company_name"], "position",
                    r["anchor_date"], r["avg_price"], r["quantity"],
                )
                created += 1
                continue

            u.execute(
                "UPDATE tracked_positions SET anchor_type = 'position', anchor_date = ?, buy_price = ?, quantity = ? "
                "WHERE id = ?",
                (r["anchor_date"], r["avg_price"], r["quantity"], pos["id"]),
            )
            since = (
                round((last_close / r["avg_price"] - 1) * 100, 2)
                if last_close and r["avg_price"] else None
            )
            # New snapshot row so the Portfolio page's "latest snapshot" price/return
            # reflects the corrected cost basis immediately, not the stale pre-import one.
            u.execute(
                "INSERT INTO position_snapshots (position_id, price_at_check, since_anchor_pct, one_yr_pct, "
                "valuation_label, signal, reasoning_json, trigger_type) VALUES (?,?,?,?,?,?,?,?)",
                (pos["id"], last_close or r["avg_price"], since, None, "Not yet assessed", "watch",
                 json.dumps(["Position updated from an imported statement"]), "event"),
            )
            updated += 1

        u.execute("UPDATE pending_imports SET status = 'confirmed' WHERE id = ?", (proposal_id,))
        u.commit()
    return {"created": created, "updated": updated}
