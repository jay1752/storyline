"""A stock's own P/E history: what its P/E was at each of the last 36 month-ends, so "cheap or dear against its own past" has a true basis.

The old "3-year P/E range" divided the 3-year PRICE range by today's earnings, which only stretches the price range and ignores that earnings
moved. A proper history needs the earnings in force at each date. Per-share EPS history cannot be trusted here (a bonus or split restates old
quarters and the stored adjustment is sometimes wrong), so the history is anchored on today's P/E and moves with two ratios that do not depend on
share counts or splits:

    P/E at date d  =  P/E now  x  (price at d / price now)  x  (trailing 4-quarter net profit now / trailing 4-quarter net profit then)

Prices are the split-adjusted closes, so the price ratio is clean. The trailing profit "then" uses only quarters public at d (about 45 days after
a quarter ends, 60 for March), the profit an investor could have seen. Approximation, said on the page: the share count is taken as unchanged
(a large buyback or issue in the window moves the true P/E a little). A date whose trailing profit was not positive, or whose four quarters are not
consecutive, is left out: a P/E on a loss is not meaningful. Fewer than MIN_POINTS month-ends gives no range at all, and every range says how many
month-ends it covers (up to 36), never a bare "3-year"."""

from __future__ import annotations

import bisect
import statistics
from datetime import date, timedelta

MONTHS = 36
MIN_POINTS = 24          # month-ends needed (of up to 36): the stored quarters reach back 3 to 4 years, so most stocks give 24 to 34. Every label carries the real count
QUARTER_LAG_DAYS = 45
MARCH_QUARTER_LAG_DAYS = 60


def _d(s) -> date:
    return date.fromisoformat(str(s)[:10])


def public_on(period_end: str) -> date:
    d = _d(period_end)
    return d + timedelta(days=MARCH_QUARTER_LAG_DAYS if d.month == 3 else QUARTER_LAG_DAYS)


def month_ends(prices: list[tuple[str, float]], months: int = MONTHS) -> list[tuple[str, float]]:
    """The last trading day's close of each of the last `months` calendar months present in `prices` (oldest first)."""
    last: dict[str, tuple[str, float]] = {}
    for ds, c in prices:
        if c is not None and c > 0:
            last[str(ds)[:7]] = (str(ds)[:10], float(c))
    return list(last.values())[-months:]


def _ttm_profit(quarters: list[dict], as_of: date) -> float | None:
    """Net profit of the latest four consecutive quarters public on `as_of`, or None."""
    known = [q for q in quarters if public_on(q["period_end_date"]) <= as_of]
    last4 = known[-4:]
    if len(last4) < 4 or any(q.get("net_profit_cr") is None for q in last4):
        return None
    span = (_d(last4[-1]["period_end_date"]) - _d(last4[0]["period_end_date"])).days
    if not 240 <= span <= 300:        # four quarters in a row span about 9 months; a gap means a missing quarter
        return None
    return float(sum(q["net_profit_cr"] for q in last4))


def with_derived_march(quarters: list[dict], annual: list[dict] | None) -> list[dict]:
    """Adds a March quarter that is missing from the quarterly rows, worked out as the financial year's profit minus the first three quarters. A
    March quarter is often stored only inside the annual figures, which broke the four-in-a-row trailing profit for most stocks. Only when all of
    Apr-Dec's quarters are present, and only when the result is plausible beside the December quarter (a quarter of it to four times it): a standalone
    versus consolidated mismatch or a one-off would otherwise leak in. Derived rows carry derived=True. India's March year end only."""
    by = {q["period_end_date"]: q for q in quarters}
    out = list(quarters)
    for a in annual or []:
        m = str(a["period_end_date"])[:10]
        if not m.endswith("-03-31") or m in by or a.get("net_profit_cr") is None:
            continue
        y = int(m[:4])
        q123 = [by.get(k) for k in (f"{y - 1}-06-30", f"{y - 1}-09-30", f"{y - 1}-12-31")]
        if any(x is None or x.get("net_profit_cr") is None for x in q123):
            continue
        q4 = float(a["net_profit_cr"]) - sum(float(x["net_profit_cr"]) for x in q123)
        dec = float(q123[-1]["net_profit_cr"])
        if dec <= 0 or not 0.25 * dec <= q4 <= 4 * dec:
            continue
        out.append({"period_end_date": m, "net_profit_cr": q4, "derived": True})
    return sorted(out, key=lambda q: q["period_end_date"])


def pe_history(prices: list[tuple[str, float]], quarters: list[dict], pe_now: float | None, months: int = MONTHS, annual: list[dict] | None = None) -> dict | None:
    """`prices`: (date, close) oldest first (split-adjusted). `quarters`: reported quarters with period_end_date and net_profit_cr, oldest first,
    the latest four being the ones `pe_now` was computed from. Returns {points: [(date, pe)], low, high, median, pct, now, months} or None when the
    history is too short or today's figures cannot anchor it. `pct` = today's percentile rank among the month-end readings (0 = cheapest, 100 = dearest);
    ties count half, so a flat history reads 50 and a stock sitting at its lowest level reads low."""
    quarters = with_derived_march(quarters, annual)
    if not pe_now or pe_now <= 0 or not prices or len(quarters) < 4:
        return None
    ends = month_ends(prices, months)
    if not ends:
        return None
    price_now = ends[-1][1]
    last = prices[-1]
    if last[1] and last[1] > 0:
        price_now = float(last[1])      # today's price, not the last month-end's
    ttm_now = _ttm_profit(quarters, date.max)
    if not ttm_now or ttm_now <= 0:
        return None
    points: list[tuple[str, float]] = []
    for ds, close in ends:
        ttm = _ttm_profit(quarters, _d(ds))
        if not ttm or ttm <= 0:
            continue
        points.append((ds, pe_now * (close / price_now) * (ttm_now / ttm)))
    if len(points) < MIN_POINTS:
        return None
    vals = [p for _, p in points]
    return {"points": [(d, round(p, 1)) for d, p in points], "low": round(min(vals), 1), "high": round(max(vals), 1),
            "median": round(statistics.median(vals), 1), "pct": round((sum(1 for v in vals if v < pe_now) + 0.5 * sum(1 for v in vals if v == pe_now)) / len(vals) * 100), "now": round(pe_now, 1),
            "months": len(points)}
