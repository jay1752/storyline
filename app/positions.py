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
import math
import re
import statistics
import time
from dataclasses import dataclass
from datetime import date as date_cls
from typing import Optional

from app.db import get_market_conn, get_users_conn
from app.price_fetch import close_near, ensure_prices
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
    isin: Optional[str] = None    # printed next to every security on Indian broker statements — the unambiguous key
    amount: Optional[float] = None  # the row's printed trade value, used to catch mis-read quantity/price


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


# A trade price is "consistent" with a stock when it lies within this band of that day's close (intraday range,
# slippage, tick size). Anything outside says the statement line is NOT this stock — the failure that put a US muni
# fund, a US gold company and a textile maker into a portfolio instead of an ETF, an ETF and Angel One.
PRICE_BAND = (0.75, 1.33)
PASS_RATE = 0.6
FETCH_BUDGET_SECONDS = 25.0


def _price_check(conn, sym: str, group: list) -> dict:
    """Do this statement's prices fit THIS stock? Transactions are compared with the close on their trade date; a
    'holding' row carries an average COST (not a market price), so it is checked against the stock's whole price range."""
    ratios: list[float] = []
    strong = 0  # lines compared with the close on their own trade date (a holding's average cost can only be range-checked)
    for t in group:
        if t.price is None or t.price <= 0:
            continue
        if t.side == "holding" or not t.date:
            r = conn.execute("SELECT MIN(close) lo, MAX(close) hi FROM price_history WHERE symbol = ? AND close > 0", (sym,)).fetchone()
            if r and r["lo"] and r["hi"]:
                lo, hi = r["lo"] * PRICE_BAND[0], r["hi"] * PRICE_BAND[1]
                ratios.append(1.0 if lo <= t.price <= hi else (t.price / r["hi"] if t.price > hi else t.price / r["lo"]))
        else:
            c = close_near(conn, sym, t.date)
            if c:
                ratios.append(t.price / c)
                strong += 1
    if not ratios:
        return {"status": "unverified", "n": 0, "rate": None, "err": None, "strong": False, "text": "No price history to verify against."}
    ok = [x for x in ratios if PRICE_BAND[0] <= x <= PRICE_BAND[1]]
    rate = len(ok) / len(ratios)
    err = statistics.median(abs(math.log(x)) for x in ratios)
    if rate >= PASS_RATE:
        return {"status": "verified", "n": len(ratios), "rate": rate, "err": err, "strong": strong > 0,
                "text": f"Prices verified: {len(ok)} of {len(ratios)} line(s) within {round((math.exp(err) - 1) * 100)}% of the market price."}
    if err <= 0.45 and strong > 0:  # 25-45% off a dated close: typical of a split/bonus adjusting the price history
        return {"status": "near", "n": len(ratios), "rate": rate, "err": err, "strong": True,
                "text": f"Prices are ~{round((math.exp(err) - 1) * 100)}% off this stock's adjusted history — a split or bonus issue can cause that."}
    return {"status": "mismatch", "n": len(ratios), "rate": rate, "err": err, "strong": strong > 0,
            "text": f"Prices do NOT match: only {len(ok)} of {len(ratios)} line(s) are near this stock's market price."}


def _extended_candidates(conn, text: str, limit: int = 40) -> list[str]:
    """Other stocks the printed text could mean, best name evidence first: exact bare ticker, ticker starting with the
    text's first word, name starting with it, then names merely containing it. NSE before everything else."""
    tok = (re.split(r"[^A-Z0-9&]+", (text or "").upper()) or [""])[0]
    if len(tok) < 3:
        return []
    bare = "UPPER(substr(symbol, instr(symbol, ':') + 1))"
    rows = conn.execute(
        f"SELECT symbol FROM stocks WHERE {bare} LIKE ? OR UPPER(name) LIKE ? "
        f"ORDER BY CASE WHEN {bare} = ? THEN 0 WHEN {bare} LIKE ? THEN 1 WHEN UPPER(name) LIKE ? THEN 2 ELSE 3 END, "
        "(symbol LIKE 'NSE:%') DESC, length(symbol) LIMIT ?",
        (f"{tok}%", f"%{tok}%", tok, f"{tok}%", f"{tok}%", limit),
    ).fetchall()
    return [r["symbol"] for r in rows]


def _amount_warning(group: list) -> Optional[str]:
    bad = [t for t in group if t.amount and t.quantity and t.price and abs(t.quantity * t.price - t.amount) / abs(t.amount) > 0.05]
    if bad:
        return (f"{len(bad)} line(s): quantity × price doesn't equal the printed amount — a number may have been misread, "
                "please check against your statement.")
    return None


def _group_key(t: Txn) -> str:
    return "S:" + t.symbol if t.symbol else ("I:" + t.isin.strip().upper() if t.isin else "T:" + (t.company_text or "").strip().upper())


def propose(user_id: int, transactions: list[Txn], statement_date: Optional[str] = None) -> dict:
    deadline = time.monotonic() + FETCH_BUDGET_SECONDS
    with get_market_conn() as m:
        lookup, names = build_symbol_lookup(m)
        isin_map = {r["isin"].upper(): r["symbol"] for r in m.execute("SELECT symbol, isin FROM stocks WHERE isin IS NOT NULL")}
    valid_symbols = set(names)

    groups: dict[str, list[Txn]] = {}
    for t in transactions:
        groups.setdefault(_group_key(t), []).append(t)

    resolved: dict[str, dict] = {}   # symbol -> {"group": [...], "match": {...}, "check": {...}}
    skipped: list[dict] = []

    with get_market_conn() as m:
        def check(sym: str, grp: list) -> dict:
            ensure_prices(sym, deadline)  # a stock with no history yet is fetched from the internet right now
            return _price_check(m, sym, grp)

        for key, grp in groups.items():
            label = grp[0].company_text
            first = grp[0]
            sym, method, candidates = None, None, []
            if first.symbol and first.symbol in valid_symbols:
                sym, method = first.symbol, "chosen"
            elif first.isin and first.isin.strip().upper() in isin_map:
                sym, method = isin_map[first.isin.strip().upper()], "ISIN"
            else:
                sym, candidates = resolve_symbol(label, lookup, names)
                method = "name/ticker" if sym else None

            chk, note, confidence = None, "", "medium"
            if sym:
                chk = check(sym, grp)
                if method in ("ISIN", "chosen"):
                    confidence = "high" if chk["status"] != "mismatch" else "low"
                    note = f"Matched by {method if method == 'ISIN' else 'your choice'}."
                elif chk["status"] == "verified":
                    confidence, note = "high", "Matched by name/ticker and confirmed by prices."
                elif chk["status"] == "unverified":
                    confidence, note = "medium", "Matched by name/ticker; couldn't verify prices."

            # The name/ticker match failed the price test (or there was no unique match): weigh the other stocks the text
            # could mean by how closely the statement's own prices fit each one. Corporate actions (splits, bonuses) shift
            # old prices, so a close-but-not-perfect fit is only ever a SUGGESTION flagged for review; an unmistakable
            # fit is applied; several similar fits are put to the user as a choice.
            if sym is None or (chk and chk["status"] in ("mismatch", "near") and method == "name/ticker"):
                pool = list(dict.fromkeys(([sym] if sym else []) + candidates + _extended_candidates(m, label)))
                fetched, scored = 0, []
                for c in pool:
                    if c == sym:
                        scored.append((c, chk))
                        continue
                    has_px = m.execute("SELECT 1 FROM price_history WHERE symbol = ? LIMIT 1", (c,)).fetchone()
                    if not has_px:
                        if fetched >= 4:
                            continue
                        fetched += 1
                    r = check(c, grp)
                    if r["err"] is not None:
                        scored.append((c, r))
                scored.sort(key=lambda x: x[1]["err"] if x[1] and x[1]["err"] is not None else 9)
                orig_err = chk["err"] if (chk and chk["err"] is not None) else 9.0
                others = [x for x in scored if x[0] != sym]
                best = others[0] if others else None
                runner = others[1] if len(others) > 1 else None

                def describe(c, r):
                    return f"{c} ({names.get(c, '?')}, prices fit within ~{round((math.exp(r['err']) - 1) * 100)}%)"

                if best and best[1]["status"] == "verified" and best[1].get("strong") and best[1]["err"] <= 0.03 \
                        and (runner is None or runner[1]["err"] >= 4 * best[1]["err"] or runner[1]["err"] > 0.25):
                    sym, chk = best
                    note = (f"Switched from {label!r} to {sym}: the statement's prices fit {sym} closely and nothing else does."
                            if method else "Matched using the statement's prices.")
                    method, confidence = "price match", "medium"
                elif best and best[1].get("strong") and best[1]["err"] <= 0.45 and orig_err >= 3 * best[1]["err"] \
                        and (runner is None or runner[1]["err"] >= 1.5 * best[1]["err"]):
                    sym, chk = best
                    also = [describe(c, r) for c, r in others[1:3] if r["err"] <= 0.30]
                    note = (f"Probably {sym} ({names.get(sym, '?')}): the statement's prices are within ~{round((math.exp(chk['err']) - 1) * 100)}% of it "
                            "(splits/bonuses can explain a gap) and far from the name match. Please confirm."
                            + (f" Also close: {'; '.join(also)}." if also else ""))
                    chk = dict(chk, status="near", text=f"Prices are within ~{round((math.exp(chk['err']) - 1) * 100)}% of {sym}'s adjusted history.")
                    method, confidence = "price match", "low"
                else:
                    close = [x for x in scored if x[1]["err"] is not None and x[1]["err"] <= 0.30 and x[0] != sym][:5]
                    if close:
                        opts = "; ".join(describe(c, r) for c, r in close)
                        skipped.append({"label": label, "reason": f"Ambiguous — several stocks fit these prices: {opts}. Ask the user which one, "
                                        "then resubmit the full list with the `symbol` field set on these rows."})
                        continue
                    if sym:
                        confidence, note = "low", "Name matched but prices don't — please verify this is the right stock."
                    else:
                        near = ", ".join(f"{c} ({names.get(c, '?')})" for c, _ in scored[:4])
                        skipped.append({"label": label, "reason": "No stock in our list matches this name and these prices"
                                        + (f" (closest: {near})" if near else "") + ". Use search_stocks, then resubmit with `symbol` set."})
                        continue

            entry = resolved.setdefault(sym, {"group": [], "match": None, "check": None, "notes": []})
            entry["group"].extend(grp)
            entry["match"], entry["check"] = {"method": method, "confidence": confidence, "note": note}, chk
            aw = _amount_warning(grp)
            if aw:
                entry["notes"].append(aw)

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
    with get_market_conn() as m:
        for sym, info in resolved.items():
            group = info["group"]
            qty, avg, anchor, warnings, error = _aggregate_symbol(group, statement_date)
            if error:
                skipped.append({"label": names[sym] + f" ({sym})", "reason": error})
                continue
            if qty <= EPS:
                skipped.append({"label": names[sym] + f" ({sym})", "reason": "Fully sold within this file — nothing left to track."})
                continue
            warnings = warnings + info["notes"]
            ensure_prices(sym, deadline)
            last = m.execute("SELECT date, close FROM price_history WHERE symbol = ? ORDER BY date DESC LIMIT 1", (sym,)).fetchone()
            if not last:
                warnings.append("No price data found for this stock anywhere online — it will show without a current value.")
            chk, match = info["check"], info["match"]
            if chk and chk["status"] in ("mismatch", "near"):
                warnings.append(chk["text"])
            prior = existing.get(sym)
            action = "new" if prior is None else ("convert" if prior["anchor_type"] == "watchlist" else "update")
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
                "match": match,
                "price_check": {"status": chk["status"], "text": chk["text"]} if chk else None,
                "last_price": round(last["close"], 2) if last else None,
                "last_price_date": last["date"] if last else None,
                "return_pct": round((last["close"] / avg - 1) * 100, 1) if last and avg else None,
                "needs_review": bool((match and match["confidence"] == "low") or (chk and chk["status"] == "mismatch")
                                     or info["notes"] or not last),
            })
    rows.sort(key=lambda r: (not r["needs_review"], r["symbol"]))

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
    deadline = time.monotonic() + FETCH_BUDGET_SECONDS
    for r in proposal["rows"]:  # a stock with no/stale prices is fetched now, so its returns work immediately
        ensure_prices(r["symbol"], deadline)
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
