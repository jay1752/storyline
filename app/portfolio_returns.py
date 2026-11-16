"""The returns page's data: each holding's and each market's true return from the ledger (app/ledger.py), what was realised by financial year, and the
same money measured against an index. India (₹) and US ($) are kept apart: a return in two currencies cannot be added without a dated exchange rate.

`u` is a users-DB connection with market attached. Nothing here writes."""

from __future__ import annotations

import bisect
from datetime import date

from app import ledger as L

BENCH = {"IN": ("^NSEI", "Nifty 50"), "US": ("^GSPC", "S&P 500")}
CUR = {"IN": "₹", "US": "$"}
STALE_INDEX_DAYS = 10


def market_of(symbol: str) -> str:
    return "IN" if symbol.startswith("NSE:") else "US"


class _Series:
    def __init__(self, rows):
        self.dates = [str(r[0])[:10] for r in rows if r[1] is not None]
        self.closes = [float(r[1]) for r in rows if r[1] is not None]

    def at(self, d: str):
        i = bisect.bisect_right(self.dates, str(d)[:10]) - 1
        return self.closes[i] if i >= 0 else None

    @property
    def last(self):
        return (self.dates[-1], self.closes[-1]) if self.dates else (None, None)


def _prices(u, symbol: str) -> _Series:
    return _Series(u.execute("SELECT date, close FROM market.price_history WHERE symbol = ? AND close IS NOT NULL ORDER BY date", (symbol,)).fetchall())


def assemble(u, user_id: int, today: date | None = None) -> dict:
    today = today or date.today()
    pos_rows = u.execute("""SELECT id, symbol, company_name, anchor_type, anchor_date, buy_price, quantity, status, closed_at, exit_price
                            FROM tracked_positions WHERE user_id = ? ORDER BY (status = 'active') DESC, id DESC""", (user_id,)).fetchall()
    positions: dict[str, dict] = {}
    for r in pos_rows:                      # one record per stock: the active position, else the latest closed one
        positions.setdefault(r["symbol"], dict(r))
    explicit: dict[str, list[dict]] = {}
    for r in L.load_trades(u, user_id):
        explicit.setdefault(r["symbol"], []).append(r)
    names = {r["symbol"]: r["name"] for r in u.execute("SELECT symbol, name FROM market.stocks WHERE symbol IN (%s)" % ",".join("?" * len(set(positions) | set(explicit))),
                                                      tuple(set(positions) | set(explicit))).fetchall()} if (positions or explicit) else {}

    holdings: list[dict] = []
    by_market: dict[str, dict] = {}
    for sym in sorted(set(positions) | set(explicit)):
        pos = positions.get(sym)
        trades = L.to_trades(explicit.get(sym, []))
        notes: list[str] = []
        if pos:
            if pos["anchor_type"] == "watchlist" and not trades:
                continue
            trades, notes = L.reconcile(trades, pos)
        if not trades:
            continue
        mkt = market_of(sym)
        px = _prices(u, sym)
        last_date, last_close = px.last
        s = L.summarise(trades, last_close, last_date or today.isoformat(), mkt)
        hint = None
        for t in sorted(trades, key=lambda x: x.date):
            f = L.split_suspect(t.price, px.at(t.date))
            if f:
                hint = f"Your {t.side} on {t.date} at {CUR[mkt]}{t.price:g} is about {f:g}x that day's split-adjusted price. If a split or bonus followed, the quantity and price need adjusting."
                break
        holdings.append({"symbol": sym, "name": (pos or {}).get("company_name") or names.get(sym) or sym, "market": mkt, "closed": s["open_qty"] <= L.EPS, "summary": s,
                         "notes": notes, "split_hint": hint, "tracked": bool(pos and pos["status"] == "active"), "px": px})
        b = by_market.setdefault(mkt, {"holdings": [], "trades": []})
        b["holdings"].append(holdings[-1])
        b["trades"].extend((sym, t) for t in trades)

    books: dict[str, dict] = {}
    for mkt, b in by_market.items():
        hs = [h["summary"] for h in b["holdings"]]
        flows = sorted((f for s in hs for f in s["flows"]), key=lambda x: x[0])
        value = sum(s["value"] or 0.0 for s in hs)
        unknown_price = [h["symbol"] for h in b["holdings"] if h["summary"]["open_qty"] > L.EPS and h["summary"]["value"] is None]
        as_of = max((h["px"].last[0] for h in b["holdings"] if h["px"].last[0]), default=today.isoformat())
        invested = sum(s["invested"] for s in hs)
        realised = sum(s["realised"] for s in hs)
        unrealised = sum(s["unrealised"] or 0.0 for s in hs)
        total = realised + unrealised
        legs = [{**leg, "symbol": h["symbol"]} for h in b["holdings"] for leg in h["summary"]["realised_legs"]]
        books[mkt] = {
            "market": mkt, "cur": CUR[mkt], "count": len(hs), "open_count": sum(1 for s in hs if s["open_qty"] > L.EPS), "invested": invested, "value": value,
            "unrealised": unrealised, "realised": realised, "total_gain": total, "gain_on_cost_pct": total / invested * 100 if invested > L.EPS else None,
            "xirr": L.xirr(flows + ([(as_of, value)] if value else [])), "span_days": max((s["span_days"] for s in hs), default=0), "as_of": as_of,
            "has_estimated": any(s["has_estimated"] for s in hs), "unpriced": unknown_price, "by_year": L.realised_by_year([l for l in legs]), "legs": legs,
            "benchmark": _benchmark(u, mkt, b["holdings"], flows, today),
        }
    return {"holdings": holdings, "books": books, "today": today.isoformat()}


def _benchmark(u, mkt: str, holdings: list[dict], flows: list[tuple[str, float]], today: date) -> dict | None:
    """The same dated money in the market's index, both measured to the index's last date so the two numbers cover the same period."""
    sym, name = BENCH[mkt]
    idx = _Series(u.execute("SELECT date, close FROM market.index_prices WHERE symbol = ? ORDER BY date", (sym,)).fetchall())
    cmp_date, last_idx = idx.last
    if not cmp_date or not flows:
        return None
    upto = [(d, a) for d, a in flows if d <= cmp_date]
    if not upto:
        return None
    own_value = 0.0
    for h in holdings:
        q = h["summary"]["open_qty"]
        if q > L.EPS:
            p = h["px"].at(cmp_date)
            if p is None:
                return None
            own_value += q * p
    b = L.benchmark(upto, idx.at, last_idx, cmp_date)
    if not b:
        return None
    own_xirr = L.xirr(upto + [(cmp_date, own_value)]) if own_value else None
    stale = (today - date.fromisoformat(cmp_date)).days > STALE_INDEX_DAYS
    return {"name": name, "as_of": cmp_date, "stale": stale, "own_value": own_value, "bench_value": b["value"], "own_xirr": own_xirr, "bench_xirr": b["xirr"]}
