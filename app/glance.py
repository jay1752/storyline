"""Growth and returns at a glance: four small cards at the top of a stock page, so a reader sees the shape of the business in one look.

  Compounded Sales Growth   10 years, 5 years, 3 years, trailing twelve months
  Compounded Profit Growth  10 years, 5 years, 3 years, trailing twelve months
  Stock Price CAGR          10 years, 5 years, 3 years, 1 year
  Return on Equity          average of the last 10, 5 and 3 years, and last year

Indian stocks use the fundamentals IndianAPI supplies (sales, profit and ROE) where it has them; anything missing, and every US stock, is worked out here
from the stored annual statements and quarters. Price CAGR is always worked out here from our own split-adjusted prices: the vendor's was computed on an
older date, and next to today's price a stale "1 year" would not agree with the page (our own sales growth agrees with the vendor's within 3 points for
98 to 100% of Indian stocks, profit growth for 65 to 83%, where the vendor counts profit slightly differently). A rate is left blank rather than guessed: a base year with no or negative
profit gives no compounded rate, a stock listed more recently than the horizon gives no price rate, and a sales or profit rate above 100% a year over five
years or more (32 times in five years) is shown as a dash: it is almost always a tiny or wrong base year, not a real compounder. Price CAGR excludes dividends. Values are plain facts;
the card carries no good or bad colouring, because what is good enough is the reader's own rule, shown next to it in the rules strip.

Pure functions plus one assembler that reads the market database (and the IndianAPI file for Indian stocks)."""

from __future__ import annotations

from datetime import date, timedelta

HORIZONS = {"10Y": 10, "5Y": 5, "3Y": 3, "1Y": 1}
IMPLAUSIBLE_PCT = 100.0          # a sales or profit CAGR above this over 5+ years is treated as a base-year artefact
CARDS = [
    ("Compounded Sales Growth", "sales", [("10 Years", "10Y"), ("5 Years", "5Y"), ("3 Years", "3Y"), ("TTM", "TTM")]),
    ("Compounded Profit Growth", "profit", [("10 Years", "10Y"), ("5 Years", "5Y"), ("3 Years", "3Y"), ("TTM", "TTM")]),
    ("Stock Price CAGR", "price", [("10 Years", "10Y"), ("5 Years", "5Y"), ("3 Years", "3Y"), ("1 Year", "1Y")]),
    ("Return on Equity", "roe", [("10 Years", "10Y"), ("5 Years", "5Y"), ("3 Years", "3Y"), ("Last Year", "Last Year")]),
]
IA_METRIC = {"sales": "Compounded Sales Growth", "profit": "Compounded Profit Growth", "price": "Stock Price CAGR", "roe": "Return on Equity"}


def cagr(end, start, years: float) -> float | None:
    """Compound annual rate in percent; None unless both ends are positive."""
    if end is None or start is None or end <= 0 or start <= 0 or years <= 0:
        return None
    return ((end / start) ** (1.0 / years) - 1.0) * 100.0


def _d(s) -> date:
    return date.fromisoformat(str(s)[:10])


def price_cagrs(closes: list[tuple[str, float]]) -> dict[str, float | None]:
    """`closes`: (date, close) oldest first, split-adjusted. A horizon needs a close within 25 days of that many years back; a younger listing gives None."""
    out = {h: None for h in HORIZONS}
    pts = [(d, c) for d, c in closes if c]
    if not pts:
        return out
    last_d, last_c = _d(pts[-1][0]), pts[-1][1]
    dates = [p[0] for p in pts]
    import bisect
    for h, n in HORIZONS.items():
        try:
            target = last_d.replace(year=last_d.year - n)
        except ValueError:                      # 29 February
            target = last_d.replace(year=last_d.year - n, day=28)
        i = bisect.bisect_right(dates, target.isoformat()) - 1
        if i < 0:
            continue
        if (target - _d(dates[i])).days > 25:   # the nearest earlier close is too far back: the stock was not trading then
            continue
        out[h] = cagr(last_c, pts[i][1], n)
    return out


def annual_cagr(series: list[dict], key: str, years: int) -> float | None:
    """`series`: fiscal years oldest first, each {fy_end, sales, profit, roe}. The latest year against the one `years` earlier (within 40 days)."""
    rows = [r for r in series if r.get(key) is not None]
    if not rows:
        return None
    last = rows[-1]
    for r in rows:
        if abs((_d(r["fy_end"]) - (_d(last["fy_end"]) - timedelta(days=round(365.25 * years)))).days) <= 40:
            return cagr(last[key], r[key], years)
    return None


def roe_average(series: list[dict], years: int) -> float | None:
    vals = [r["roe"] for r in series if r.get("roe") is not None][-years:]
    return sum(vals) / len(vals) if len(vals) == years else None


def ttm_growth(quarters: list[dict], key: str) -> float | None:
    """Trailing four quarters against the four before them, in percent. Needs eight consecutive quarters; a gap gives None."""
    qs = [q for q in quarters if q.get(key) is not None]
    if len(qs) < 8:
        return None
    last8 = qs[-8:]
    ends = [_d(q["period_end_date"]) for q in last8]
    for a, b in zip(ends, ends[1:]):
        if not 75 <= (b - a).days <= 106:        # about a quarter apart
            return None
    now, before = sum(q[key] for q in last8[4:]), sum(q[key] for q in last8[:4])
    if before <= 0 or now <= 0:
        return None
    return (now / before - 1.0) * 100.0


def build(own: dict, ia: dict | None) -> list[dict]:
    """The four cards. `own` = {"sales": {horizon: value}, "profit": ..., "price": ..., "roe": ...} worked out here; `ia` = IndianAPI's figures by
    metric name and horizon (or None). For sales, profit and ROE IndianAPI's figure wins where it has one; for price our own always does."""
    cards = []
    for title, kind, rows in CARDS:
        shown, any_value = [], False
        for label, hz in rows:
            vendor = ((ia or {}).get(IA_METRIC[kind]) or {}).get(hz)
            mine = (own.get(kind) or {}).get(hz)
            v = (mine if mine is not None else vendor) if kind == "price" else (vendor if vendor is not None else mine)
            suspect = v is not None and kind in ("sales", "profit") and hz in ("10Y", "5Y") and v > IMPLAUSIBLE_PCT
            if suspect:
                v = None
            shown.append({"label": label, "value": None if v is None else round(v), "suspect": suspect,
                          "from_vendor": v is not None and v == vendor and kind != "price" and vendor is not None})
            any_value = any_value or v is not None
        cards.append({"title": title, "rows": shown, "empty": not any_value})
    return cards


# ---------------------------------------------------------------------------------------------------- the assembler

def for_stock(conn, symbol: str, market: str, ia_growth: dict | None) -> dict | None:
    """The cards for one stock, or None when there is nothing to show. `conn` is the market DB; `ia_growth` the IndianAPI growth figures (India only)."""
    closes = [(r["date"], r["close"]) for r in conn.execute(
        "SELECT date, close FROM price_history WHERE symbol = ? AND close IS NOT NULL AND date >= ? ORDER BY date",
        (symbol, (date.today() - timedelta(days=365 * 10 + 60)).isoformat())).fetchall()]
    own: dict[str, dict] = {"price": price_cagrs(closes), "sales": {}, "profit": {}, "roe": {}}
    if symbol.startswith("NSE:"):
        series = [{"fy_end": r["period_end_date"], "sales": r["revenue_cr"], "profit": r["net_profit_cr"], "roe": r["roe_pct"]} for r in conn.execute(
            "SELECT period_end_date, revenue_cr, net_profit_cr, roe_pct FROM annual_data WHERE symbol = ? ORDER BY period_end_date", (symbol,)).fetchall()]
    else:
        series = []
        for r in conn.execute("SELECT period_end_date, revenue_m, net_profit_m, equity_m FROM us_annual WHERE symbol = ? AND period_end_date != '0000-00-00' "
                              "ORDER BY period_end_date", (symbol,)).fetchall():
            roe = r["net_profit_m"] / r["equity_m"] * 100 if r["net_profit_m"] is not None and r["equity_m"] and r["equity_m"] > 0 else None
            series.append({"fy_end": r["period_end_date"], "sales": r["revenue_m"], "profit": r["net_profit_m"], "roe": roe})
    for h, n in (("10Y", 10), ("5Y", 5), ("3Y", 3)):
        own["sales"][h] = annual_cagr(series, "sales", n)
        own["profit"][h] = annual_cagr(series, "profit", n)
        own["roe"][h] = roe_average(series, n)
    own["roe"]["Last Year"] = next((r["roe"] for r in reversed(series) if r.get("roe") is not None), None)
    qs = [dict(r) for r in conn.execute("SELECT period_end_date, revenue_cr, net_profit_cr FROM quarterly_data WHERE symbol = ? AND quarter_label NOT LIKE '%nnual%' "
                                        "ORDER BY period_end_date", (symbol,)).fetchall()]
    own["sales"]["TTM"] = ttm_growth(qs, "revenue_cr")
    own["profit"]["TTM"] = ttm_growth(qs, "net_profit_cr")
    cards = build(own, ia_growth if market == "IN" else None)
    return None if all(c["empty"] for c in cards) else {"cards": cards, "vendor": any(r["from_vendor"] for c in cards for r in c["rows"])}
