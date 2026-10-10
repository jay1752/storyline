import numpy as np
import pandas as pd
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import fx as fx_mod
from app.db import get_market_conn, get_users_conn
from app.routes.analytics import _spark
from app.templates_config import templates

router = APIRouter()


BENCHMARKS = {"nifty50": 4.2, "sp500": 5.8}  # still illustrative — no real index-level
# price feed sourced yet, separate task from wiring this page to real positions

# A full correlation matrix is O(n^2) to both compute and render — fine for a handful
# of holdings, unusable (and slow) once a real imported portfolio has 100+ positions.
# Cap to the largest holdings by weight, same principle as 13F's "top ~3000 by value"
# scope decision elsewhere in this codebase: the biggest positions are the ones whose
# correlation actually matters for concentration risk.
TOP_N_FOR_CORRELATION = 12
CORR_LOOKBACK_DAYS = 180
MIN_OVERLAP_DAYS = 30  # below this, a correlation number is noise, not signal


def _compute_correlation(conn, symbols: list[str]) -> dict:
    """Real pairwise correlation of daily returns, trailing ~180 days. Returns
    {(a, b): corr}. A pair is simply absent from the dict (never fabricated as 0 or
    guessed) when there isn't enough overlapping price history to mean anything —
    same honesty convention as Layer 2's "needs 3+ real peers" sector benchmark."""
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    rows = conn.execute(
        f"""SELECT symbol, date, close FROM price_history
            WHERE symbol IN ({placeholders}) AND date >= date('now', '-{CORR_LOOKBACK_DAYS} days')
            ORDER BY date""",
        symbols,
    ).fetchall()
    if not rows:
        return {}

    df = pd.DataFrame([dict(r) for r in rows])
    pivot = df.pivot_table(index="date", columns="symbol", values="close")
    returns = pivot.pct_change().dropna(how="all")

    result = {}
    for i, a in enumerate(symbols):
        result[(a, a)] = 1.0
        if a not in returns.columns:
            continue
        for b in symbols[i + 1:]:
            if b not in returns.columns:
                continue
            paired = returns[[a, b]].dropna()
            if len(paired) < MIN_OVERLAP_DAYS:
                continue
            corr = paired[a].corr(paired[b])
            if corr is not None and not np.isnan(corr):
                result[(a, b)] = result[(b, a)] = round(float(corr), 2)
    return result


def _group_inr(n: int) -> str:
    """Indian digit grouping: 1234567 -> 12,34,567."""
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def _fmt_money(v, cur: str, places: int = 0, signed: bool = False) -> str:
    """₹ amounts use lakh/crore grouping, $ amounts use thousands."""
    if v is None:
        return "—"
    sign = "-" if v < 0 else ("+" if signed else "")
    q = f"{abs(v):.{places}f}"
    whole, _, frac = q.partition(".")
    whole = _group_inr(int(whole)) if cur == "₹" else f"{int(whole):,}"
    return f"{sign}{cur}{whole}" + (f".{frac}" if frac else "")


def _fmt_pct(v, signed: bool = True) -> str:
    return "—" if v is None else (f"{v:+.2f}%" if signed else f"{v:.2f}%")


def _donut(items: list[tuple[str, float]], max_items: int = 7) -> list[dict]:
    """Segments for an SVG donut (circumference normalised to 100). Small tail folds into 'Others'."""
    items = sorted((x for x in items if x[1] > 0), key=lambda x: -x[1])
    if len(items) > max_items:
        items = items[:max_items - 1] + [("Others", sum(v for _, v in items[max_items - 1:]))]
    total = sum(v for _, v in items) or 1
    out, cum = [], 0.0
    for i, (label, v) in enumerate(items):
        pct = v / total * 100
        out.append({"label": label, "pct": round(pct, 1), "dash": round(pct, 3), "offset": round(25 - cum, 3), "color": f"var(--s{i % 8 + 1})", "value": v})
        cum += pct
    return out


def _summary(hs: list[dict], cur: str, fx: float) -> dict:
    """Totals for a set of holdings in one display currency (`fx` converts each holding's value
    into it: 1.0 for same-currency, the live USD→INR rate in the combined view)."""
    conv = lambda h, k: (h[k] * (fx if h["market"] == "US" and cur == "₹" and fx != 1.0 else 1.0)) if h[k] is not None else None
    value = sum(conv(h, "value") for h in hs)
    costed = [h for h in hs if h["invested"] is not None]
    invested = sum(conv(h, "invested") for h in costed)
    value_costed = sum(conv(h, "value") for h in costed)
    pnl = value_costed - invested if costed else None
    pnl_pct = (pnl / invested * 100) if costed and invested else None
    daying = [h for h in hs if h["day"] is not None]
    day = sum(conv(h, "day") for h in daying) if daying else None
    prev_value = sum(conv(h, "value") - conv(h, "day") for h in daying) if daying else 0
    day_pct = (day / prev_value * 100) if daying and prev_value else None
    return {
        "cur": cur, "count": len(hs), "costed": len(costed),
        "value": _fmt_money(value, cur), "invested": _fmt_money(invested, cur) if costed else "—",
        "pnl": _fmt_money(pnl, cur, signed=True) if pnl is not None else "—", "pnl_pct": _fmt_pct(pnl_pct), "pnl_tone": "" if pnl is None else ("pos" if pnl >= 0 else "neg"),
        "day": _fmt_money(day, cur, signed=True) if day is not None else "—", "day_pct": _fmt_pct(day_pct), "day_tone": "" if day is None else ("pos" if day >= 0 else "neg"),
    }


@router.get("/portfolio", response_class=HTMLResponse)
def portfolio_screen(request: Request):
    fx_info = fx_mod.usd_inr()  # live USD->INR from the hourly job (Admin > Scheduled jobs)
    FX = fx_info["rate"]
    user_id = request.state.user_id
    with get_users_conn() as u:
        rows = u.execute(
            """
            SELECT tp.symbol, tp.company_name, tp.quantity, tp.buy_price, tp.anchor_date,
                   market.stocks.sector, market.stocks.market
            FROM tracked_positions tp
            JOIN market.stocks ON market.stocks.symbol = tp.symbol
            WHERE tp.user_id = ? AND tp.anchor_type = 'position' AND tp.status = 'active'
            ORDER BY tp.symbol
            """,
            (user_id,),
        ).fetchall()

    holdings, needs_details = [], []
    as_of = None
    with get_market_conn() as m:
        for r in rows:
            closes = m.execute(
                "SELECT date, close FROM price_history WHERE symbol = ? ORDER BY date DESC LIMIT 32", (r["symbol"],)
            ).fetchall()
            if r["quantity"] is None or not closes:
                needs_details.append({"symbol": r["symbol"], "name": r["company_name"],
                                      "why": "no quantity yet" if r["quantity"] is None else "no price history yet"})
                continue
            cur = "₹" if r["market"] == "IN" else "$"
            ltp, ltp_date = closes[0]["close"], closes[0]["date"]
            prev = closes[1]["close"] if len(closes) > 1 else None
            qty, avg = r["quantity"], r["buy_price"]
            value = qty * ltp
            invested = qty * avg if avg else None
            pnl = value - invested if invested is not None else None
            day = qty * (ltp - prev) if prev else None
            as_of = max(as_of, ltp_date) if as_of else ltp_date
            places = 2 if ltp < 1000 else 0
            holdings.append({
                "symbol": r["symbol"], "ticker": r["symbol"].split(":")[1], "name": r["company_name"], "market": r["market"],
                "sector": r["sector"] or "Unclassified", "cur": cur, "qty": qty, "avg": avg, "ltp": ltp, "prev": prev,
                "value": value, "invested": invested, "pnl": pnl, "pnl_pct": (pnl / invested * 100) if invested else None,
                "day": day, "day_pct": ((ltp / prev - 1) * 100) if prev else None, "anchor_date": r["anchor_date"],
                "spark": _spark([c["close"] for c in reversed(closes)], w=72, h=24),
                "qty_s": f"{qty:g}", "avg_s": _fmt_money(avg, cur, 2) if avg else "—", "ltp_s": _fmt_money(ltp, cur, places),
                "value_s": _fmt_money(value, cur), "invested_s": _fmt_money(invested, cur) if invested is not None else "—",
                "pnl_s": _fmt_money(pnl, cur, signed=True) if pnl is not None else "—",
            })

    for h in holdings:
        h["value_inr"] = h["value"] * (FX if h["market"] == "US" else 1.0)
    total_inr = sum(h["value_inr"] for h in holdings)
    for h in holdings:
        h["weight_pct"] = round(h["value_inr"] / total_inr * 100, 1) if total_inr else 0
        h["pnl_pct_s"] = _fmt_pct(h["pnl_pct"])
        h["day_pct_s"] = _fmt_pct(h["day_pct"])
        h["pnl_tone"] = "" if h["pnl"] is None else ("pos" if h["pnl"] >= 0 else "neg")
        h["day_tone"] = "" if h["day"] is None else ("pos" if h["day"] >= 0 else "neg")
    holdings.sort(key=lambda h: -h["value_inr"])
    max_weight = holdings[0]["weight_pct"] if holdings else 1

    india = [h for h in holdings if h["market"] == "IN"]
    us = [h for h in holdings if h["market"] == "US"]
    summaries = {
        "all": _summary(holdings, "₹", FX),
        "in": _summary(india, "₹", 1.0) if india else None,
        "us": _summary(us, "$", 1.0) if us else None,
    }

    def donut_for(key):
        agg: dict[str, float] = {}
        for h in holdings:
            label = {"class": "Indian stocks" if h["market"] == "IN" else "US stocks", "sector": h["sector"], "holding": h["ticker"]}[key]
            agg[label] = agg.get(label, 0) + h["value_inr"]
        segs = _donut(list(agg.items()))
        for sg in segs:
            sg["value_s"] = _fmt_money(sg["value"], "₹")
        return segs
    donuts = {k: donut_for(k) for k in ("class", "sector", "holding")}

    with_day = [h for h in holdings if h["day_pct"] is not None]
    movers = {
        "up": sorted(with_day, key=lambda h: -h["day_pct"])[:3],
        "down": sorted(with_day, key=lambda h: h["day_pct"])[:3],
    }

    # --- Risk & diversification (unchanged views, now driven by real prices) ---
    top_symbols = [h["symbol"] for h in holdings[:TOP_N_FOR_CORRELATION]]
    with get_market_conn() as m:
        corr_lookup = _compute_correlation(m, top_symbols)
    corr_matrix = [[corr_lookup.get((a, b)) for b in top_symbols] for a in top_symbols]
    short_labels = [s.split(":")[1][:6] for s in top_symbols]
    attribution_all = sorted(
        ({"symbol": h["symbol"], "pp": round(h["weight_pct"] * h["pnl_pct"] / 100, 2)} for h in holdings if h["pnl_pct"] is not None),
        key=lambda a: -a["pp"],
    )
    portfolio_return = round(sum(a["pp"] for a in attribution_all), 1)
    attribution = (attribution_all[:10] + attribution_all[-10:]) if len(attribution_all) > 20 else attribution_all

    return templates.TemplateResponse(
        "portfolio.html",
        {
            "request": request,
            "active_nav": "portfolio",
            "holdings": holdings,
            "summaries": summaries,
            "donuts": donuts,
            "movers": movers,
            "needs_details": needs_details,
            "as_of": as_of,
            "fx": FX, "fx_info": fx_info,
            "has_us": bool(us),
            "has_in": bool(india),
            "max_weight": max_weight,
            "symbols": top_symbols,
            "short_labels": short_labels,
            "corr_matrix": corr_matrix,
            "attribution": attribution,
            "attribution_total": len(attribution_all),
            "portfolio_return": portfolio_return,
        },
    )
