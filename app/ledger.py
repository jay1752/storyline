"""The portfolio ledger: every trade the user made, and the returns worked out from them.

Before this, a statement import collapsed a stock's trades into one average price and one date, the journal changed the position without keeping a
ledger, and a stock sold within a file was thrown away. So there was no true return (XIRR), no realised gains and no honest benchmark comparison.
This module keeps the trades (table `ledger_trades`, see app/db.py) and does the arithmetic:

  * lots are matched first-in first-out; a sale realises the gain on the oldest shares still held;
  * XIRR (the annualised return that the actual dated cash flows earned, with what is held valued at the last price) is shown only once the money
    has been in for a year; under that it is a plain gain on cost, never an annualised figure;
  * the benchmark comparison asks "if the same money had gone into the index on the same dates" (same flows, index units instead of shares);
  * realised gains are grouped by Indian financial year (April to March) and by holding period. The long/short split is indicative: India listed equity
    is long term after 12 months; shares of US companies after 24. It is not tax advice and computes no tax.

What it does not know, and says so: dividends and fees (not stored), splits and bonuses after a trade (a price far from the adjusted price on that day
is flagged), and the true date of an opening balance imported as a holdings snapshot (that lot is marked estimated).

Pure functions on plain data, so the maths is testable without a database. The storage and reconciliation helpers at the bottom use the users DB."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

EPS = 1e-6
LONG_TERM_DAYS = {"IN": 365, "US": 730}    # held MORE than this long => long term (India listed equity 12 months; shares of US companies 24 months)
MIN_XIRR_DAYS = 365                         # below this the money has not been in a year: no annualised figure


@dataclass
class Trade:
    date: str
    side: str                 # buy | sell
    qty: float
    price: float
    estimated: bool = False   # an opening balance whose real purchase date and price are not known
    source: str = ""          # import | journal | manual | opening
    id: int | None = None


def _d(s) -> date:
    return date.fromisoformat(str(s)[:10])


# ---------------------------------------------------------------------------------------------------- lots

def fifo(trades: list[Trade], market: str = "IN") -> dict:
    """Match sales to the oldest shares still held. Returns open quantity and cost, remaining lots, each realised leg, and any sold quantity that
    no earlier buy covers (a data gap: earlier buys are missing from the ledger)."""
    lots: list[list] = []        # [date, qty_left, price, estimated]
    realised: list[dict] = []
    unmatched = 0.0
    for t in sorted(trades, key=lambda x: (x.date, x.side == "sell")):   # a day's buys before its sells
        if t.qty <= EPS:
            continue
        if t.side == "buy":
            lots.append([t.date, t.qty, t.price, t.estimated])
            continue
        left = t.qty
        while left > EPS and lots:
            lot = lots[0]
            take = min(left, lot[1])
            days = (_d(t.date) - _d(lot[0])).days
            cost, proceeds = take * lot[2], take * t.price
            realised.append({"sell_date": t.date, "buy_date": lot[0], "qty": take, "buy_price": lot[2], "sell_price": t.price, "cost": cost, "proceeds": proceeds,
                             "gain": proceeds - cost, "days": days, "term": "long" if days > LONG_TERM_DAYS.get(market, 365) else "short", "estimated": lot[3]})
            lot[1] -= take
            left -= take
            if lot[1] <= EPS:
                lots.pop(0)
        if left > EPS:
            unmatched += left
    open_qty = sum(l[1] for l in lots)
    open_cost = sum(l[1] * l[2] for l in lots)
    return {"open_qty": open_qty, "open_cost": open_cost, "avg_cost": open_cost / open_qty if open_qty > EPS else None,
            "lots": [{"date": l[0], "qty": l[1], "price": l[2], "estimated": l[3]} for l in lots], "realised": realised,
            "realised_gain": sum(r["gain"] for r in realised), "unmatched_sell_qty": unmatched}


def financial_year(d: str) -> str:
    """Indian financial year of a date: April 2025 to March 2026 is 'FY2025-26'."""
    x = _d(d)
    start = x.year if x.month >= 4 else x.year - 1
    return f"FY{start}-{(start + 1) % 100:02d}"


def realised_by_year(realised: list[dict]) -> list[dict]:
    """[{fy, short, long, total, legs}] newest year first. Gains net of losses within each bucket."""
    out: dict[str, dict] = {}
    for r in realised:
        y = out.setdefault(financial_year(r["sell_date"]), {"fy": financial_year(r["sell_date"]), "short": 0.0, "long": 0.0, "legs": 0})
        y[r["term"]] += r["gain"]
        y["legs"] += 1
    for y in out.values():
        y["total"] = y["short"] + y["long"]
    return sorted(out.values(), key=lambda y: y["fy"], reverse=True)


# ---------------------------------------------------------------------------------------------------- XIRR

def xirr(flows: list[tuple[str, float]]) -> float | None:
    """Annualised rate of the dated cash flows (money out negative, money in positive, the value still held counted as money in on its date), or None when
    it is not defined: no flow of each sign, or a span under MIN_XIRR_DAYS (an annualised return over weeks is not meaningful). Actual/365, bisection."""
    flows = [(d, a) for d, a in flows if a]
    if not flows or not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        return None
    t0 = min(_d(d) for d, _ in flows)
    if (max(_d(d) for d, _ in flows) - t0).days < MIN_XIRR_DAYS:
        return None
    pts = [((_d(d) - t0).days / 365.0, a) for d, a in flows]

    def npv(r):
        return sum(a / (1.0 + r) ** t for t, a in pts)

    lo, hi = -0.99, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-7:
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


# ---------------------------------------------------------------------------------------------------- one holding, and a book

def summarise(trades: list[Trade], last_price: float | None, as_of: str, market: str = "IN") -> dict:
    """Everything the Portfolio page shows for one stock (or, given every stock's trades pooled, for a book): invested, value now, unrealised and realised
    gain, gain on cost, XIRR when the money has been in a year, and the cash flows for a benchmark comparison."""
    f = fifo(trades, market)
    buys = [t for t in trades if t.side == "buy" and t.qty > EPS]
    invested = sum(t.qty * t.price for t in buys)
    value = f["open_qty"] * last_price if last_price is not None else None
    unrealised = value - f["open_cost"] if value is not None else None
    realised = f["realised_gain"]
    total = (unrealised or 0.0) + realised if (unrealised is not None or f["open_qty"] <= EPS) else None
    flows = [(t.date, -(t.qty * t.price) if t.side == "buy" else t.qty * t.price) for t in sorted(trades, key=lambda x: x.date) if t.qty > EPS]
    terminal = [(as_of, value)] if value else []
    first = min((t.date for t in trades), default=None)
    span = (_d(as_of) - _d(first)).days if first else 0
    return {"invested": invested, "open_qty": f["open_qty"], "open_cost": f["open_cost"], "avg_cost": f["avg_cost"], "value": value, "unrealised": unrealised,
            "realised": realised, "total_gain": total, "gain_on_cost_pct": (total / invested * 100) if total is not None and invested > EPS else None,
            "xirr": xirr(flows + terminal), "span_days": span, "flows": flows, "realised_legs": f["realised"], "lots": f["lots"],
            "unmatched_sell_qty": f["unmatched_sell_qty"], "has_estimated": any(t.estimated for t in trades), "first_date": first}


def benchmark(flows: list[tuple[str, float]], index_at, last_index: float | None, as_of: str) -> dict | None:
    """Put the same dated money into an index instead: a flow out buys index units at that day's close, a flow in sells units. `index_at(date)` -> close
    on or before that day (or None). Returns {value, xirr, units} where value is what those units are worth at `last_index`, or None when the index
    cannot price a flow date or the units left are not positive."""
    if not flows or not last_index:
        return None
    units = 0.0
    out_flows = []
    for d, a in flows:
        p = index_at(d)
        if not p:
            return None
        units += -a / p            # money out (negative) buys units
        out_flows.append((d, a))
    if units <= EPS:
        return None
    value = units * last_index
    return {"value": value, "units": units, "xirr": xirr(out_flows + [(as_of, value)])}


# ---------------------------------------------------------------------------------------------------- corporate actions

_SPLIT_FACTORS = (2.0, 3.0, 4.0, 5.0, 10.0, 20.0)


def split_suspect(trade_price: float, adjusted_close: float | None, tolerance: float = 0.18) -> float | None:
    """The split or bonus factor a trade price hints at, or None. Stored prices are split-adjusted, so a purchase made before a 1:1 bonus (factor 2)
    shows a price about twice the adjusted close on that day. Only a hint: a real price move that big, or a wrong price, looks the same."""
    if not trade_price or not adjusted_close or adjusted_close <= 0:
        return None
    r = trade_price / adjusted_close
    for f in _SPLIT_FACTORS:
        if abs(r / f - 1) <= tolerance:
            return f
    return None


# ---------------------------------------------------------------------------------------------------- reconciling with a tracked position

def reconcile(explicit: list[Trade], pos: dict) -> tuple[list[Trade], list[str]]:
    """The trades to compute with for one tracked position: the stored trades, plus an estimated opening lot for any shares the position holds that the
    stored trades do not account for (older holdings, or a statement that was only a snapshot). A closed position without stored trades becomes an
    estimated buy and an estimated sale. Returns (trades, notes); notes explain anything estimated or inconsistent."""
    notes: list[str] = []
    qty = pos.get("quantity")
    buy_price = pos.get("buy_price")
    anchor = pos.get("anchor_date")
    closed = pos.get("status") == "exited"
    net = sum(t.qty if t.side == "buy" else -t.qty for t in explicit)
    out = list(explicit)
    if not qty or not buy_price or not anchor:
        return out, notes
    if closed:
        if not explicit and pos.get("closed_at") and pos.get("exit_price"):
            out = [Trade(anchor, "buy", qty, buy_price, True, "opening"), Trade(pos["closed_at"], "sell", qty, pos["exit_price"], True, "opening")]
            notes.append("Closed position: dates and prices come from the position record, not from stored trades.")
        return out, notes
    gap = qty - net
    if gap > EPS:
        first = min([anchor] + [t.date for t in explicit]) if explicit else anchor
        # The opening lot is the OLDEST shares, so first-in first-out sells it first. Its price is the one that makes what is left after the stored trades
        # cost what the position records (qty x average cost). The remaining cost is linear in that price, so two evaluations solve it exactly.
        def left_cost(p):
            return fifo(explicit + [Trade(first, "buy", gap, p, True, "opening")])["open_cost"]
        c0, c1 = left_cost(0.0), left_cost(1.0)
        price = (qty * buy_price - c0) / (c1 - c0) if c1 - c0 > EPS else buy_price
        if price <= 0:
            price = buy_price
        out.append(Trade(first, "buy", gap, price, True, "opening"))
        notes.append(f"{gap:g} of {qty:g} shares come from your recorded average cost and start date; no individual trades are stored for them.")
    elif gap < -EPS:
        notes.append(f"The stored trades add up to {net:g} shares but the position records {qty:g}. Check the position or the trades.")
    return out, notes


# ---------------------------------------------------------------------------------------------------- storage (users DB)

def load_trades(u, user_id: int, symbol: str | None = None) -> list[dict]:
    q = "SELECT * FROM ledger_trades WHERE user_id = ?" + (" AND symbol = ?" if symbol else "") + " ORDER BY date, id"
    return [dict(r) for r in u.execute(q, (user_id, symbol) if symbol else (user_id,)).fetchall()]


def add_trade(u, user_id: int, symbol: str, side: str, qty: float, price: float, when: str, source: str, ref: str | None = None,
              estimated: bool = False, position_id: int | None = None) -> int | None:
    """Store one trade. The same trade imported twice is stored once (same stock, day, side, quantity, price and source). Returns the new id, or None
    when it was already there. The caller commits."""
    if side not in ("buy", "sell") or not qty or qty <= 0 or price is None or price <= 0:
        return None
    cur = u.execute("""INSERT OR IGNORE INTO ledger_trades (user_id, position_id, symbol, date, side, qty, price, source, ref, estimated)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""", (user_id, position_id, symbol, str(when)[:10], side, float(qty), float(price), source, ref, int(estimated)))
    return cur.lastrowid if cur.rowcount else None


def delete_for_ref(u, user_id: int, source: str, ref: str) -> int:
    return u.execute("DELETE FROM ledger_trades WHERE user_id = ? AND source = ? AND ref = ?", (user_id, source, ref)).rowcount


def to_trades(rows: list[dict]) -> list[Trade]:
    return [Trade(r["date"], r["side"], r["qty"], r["price"], bool(r.get("estimated")), r.get("source") or "", r.get("id")) for r in rows]
