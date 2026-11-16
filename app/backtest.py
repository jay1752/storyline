"""Rules over time: how the user's CURRENT rules would have read for one stock at each past result date.

The same rule checks and the same status code as today (app/ai/checklist.py, app/ai/status.py), run on the figures that were public on each
date and on nothing later:
  - a quarter counts from about 45 days after it ends (60 for the March quarter, which carries the annual results);
  - an annual figure (ROCE, debt, total assets) counts from 90 days after its year end;
  - price-to-book needs the price then, which is not stored, so it reads "not judged" in the past.
A figure the database did not hold on a date is "not judged", never filled in. It is read as if the stock were held (the exit-speed rules apply) and
leaves the position-size and sector limits out, because past weights are not stored.

What the price did afterwards is shown as plain figures. It is not a verdict on the rules, and past readings do not predict the future."""

from __future__ import annotations

import bisect
import html
from datetime import date, timedelta

from app.ai.checklist import evaluate_framework
from app.ai.status import compute_status

QUARTER_LAG_DAYS = 45
MARCH_QUARTER_LAG_DAYS = 60
ANNUAL_LAG_DAYS = 90
HORIZONS = (("3 months", 91), ("6 months", 182), ("12 months", 365))
MAX_POINTS = 12
MIN_QUARTERS = 4   # a reading needs at least four reported quarters behind it

NOTES = [
    "These are the rules you hold today, applied to the figures that were public at each date. A figure the database did not hold then reads \"not judged\".",
    "A quarter counts from about 45 days after it ended (60 for March); an annual figure from 90 days after its year end. Price to book is not judged in the past (no price history of net worth).",
    "Read as if you held the stock. Your position-size and sector limits are left out because past weights are not stored.",
    "The price columns are what the price did afterwards, nothing more. Past readings do not predict future results. Companies that were delisted are not in the data.",
]


def _d(s) -> date:
    return date.fromisoformat(str(s)[:10])


def public_on(period_end: str) -> date:
    """The first day a quarter's figures can be taken as public."""
    d = _d(period_end)
    return d + timedelta(days=MARCH_QUARTER_LAG_DAYS if d.month == 3 else QUARTER_LAG_DAYS)


class Prices:
    """Closing prices, oldest first, with 'the close on or before this day'."""

    def __init__(self, rows):
        self.dates = [str(r[0])[:10] for r in rows]
        self.closes = [float(r[1]) for r in rows]

    @property
    def last(self) -> date | None:
        return _d(self.dates[-1]) if self.dates else None

    def at(self, d: date):
        i = bisect.bisect_right(self.dates, d.isoformat()) - 1
        return (self.dates[i], self.closes[i]) if i >= 0 else None

    def monthly(self, start: date) -> list[tuple[str, float]]:
        """The last close of each month from `start`, for the chart."""
        out: dict[str, tuple[str, float]] = {}
        for ds, c in zip(self.dates, self.closes):
            if ds >= start.isoformat():
                out[ds[:7]] = (ds, c)
        return list(out.values())


def _fin_as_of(symbol: str, annual_known: list) -> dict:
    """The balance-sheet pieces the financials rules read, from the latest annual row that was public. India only; no P/B (no price history of net worth)."""
    if not str(symbol).startswith("NSE:"):
        return {}
    for a in reversed(annual_known):
        ta = a.get("total_assets_cr")
        if not ta:
            continue
        out = {"assets": ta, "as_of": a["period_end_date"]}
        ec, rs = a.get("equity_capital_cr"), a.get("reserves_cr")
        if ec is not None and rs is not None and ec + rs > 0:
            out["equity"] = ec + rs
        return out
    return {}


def replay(symbol: str, rows: list, annual: list, sector: str | None, th: dict, sz: dict, prices: Prices, *,
           today: date | None = None, owned: bool = True, max_points: int = MAX_POINTS) -> dict:
    """`rows`: every reported quarter, oldest first (growth filled in). Returns {points, rules, series, notes}; points are oldest first."""
    today = today or date.today()
    idxs = [k for k in range(len(rows)) if k >= MIN_QUARTERS - 1][-max_points:]
    points: list[dict] = []
    for k in idxs:
        asof = min(public_on(rows[k]["period_end_date"]), today)
        rk = rows[:k + 1]
        known = [a for a in (annual or []) if _d(a["period_end_date"]) + timedelta(days=ANNUAL_LAG_DAYS) <= asof]
        fin = _fin_as_of(symbol, known)
        now = evaluate_framework(rk, sector, th, annual=known, fin_inputs=fin)
        before = evaluate_framework(rk[:-1], sector, th, annual=known, fin_inputs=fin) if len(rk) >= 2 else {"filters": [], "facts": None}
        st = compute_status(now["filters"], before["filters"], now["facts"], before["facts"], owned=owned, sz=sz, today=asof)
        p0 = prices.at(asof)
        ret = {}
        for name, days in HORIZONS:
            tgt = asof + timedelta(days=days)
            pt = prices.at(tgt) if prices.last and tgt <= prices.last else None
            ret[name] = round((pt[1] / p0[1] - 1) * 100, 1) if pt and p0 and p0[1] else None
        points.append({
            "asof": asof.isoformat(), "period_end": rows[k]["period_end_date"], "code": st["code"], "label": st["label"],
            "reasons": [x["text"] for x in st["reasons"] if x["kind"] != "note"][:3],
            "judged": st["rules"]["judged"], "passing": st["rules"]["passing"], "failing": st["rules"]["failing"], "unknown": len(st["rules"]["unknown"]),
            "price": p0[1] if p0 else None, "price_date": p0[0] if p0 else None, "after": ret,
            "filters": [{"key": f["key"], "label": f["label"], "status": f["status"], "value": f["value"], "threshold": f["threshold"]} for f in now["filters"]],
            "changed": False,
        })
    for i in range(1, len(points)):
        points[i]["changed"] = points[i]["label"] != points[i - 1]["label"]
    labels, seen = [], set()
    for p in reversed(points):          # the rule list of the newest reading, in its order
        for f in p["filters"]:
            if f["key"] not in seen:
                seen.add(f["key"])
                labels.append({"key": f["key"], "label": f["label"], "threshold": f["threshold"]})
        break
    for p in points:                    # plus any rule an older reading had that the newest does not
        for f in p["filters"]:
            if f["key"] not in seen:
                seen.add(f["key"])
                labels.append({"key": f["key"], "label": f["label"], "threshold": f["threshold"]})
    series = prices.monthly(_d(points[0]["asof"]) - timedelta(days=90)) if points else []
    notes = list(NOTES)
    if not points:
        notes.insert(0, f"Fewer than {MIN_QUARTERS} reported quarters are stored for this stock, so there is nothing to replay yet.")
    return {"symbol": symbol, "points": points, "rules": labels, "series": series, "notes": notes, "owned": owned}


# ---------------------------------------------------------------------------------------------------- one tracked stock

def timeline_for_position(u, pos, max_points: int = MAX_POINTS) -> dict:
    """The replay for one tracked stock, from stored data and the user's CURRENT rules. `u` = users-DB connection with market attached."""
    from app import framework as framework_rules
    from app.annual import annual_rows
    from app.db import get_market_conn
    from app.routes.analytics import _yoy

    sym, uid = pos["symbol"], pos["user_id"]
    stock = u.execute("SELECT sector FROM market.stocks WHERE symbol = ?", (sym,)).fetchone()
    allq = u.execute("SELECT * FROM market.quarterly_data WHERE symbol = ? ORDER BY period_end_date", (sym,)).fetchall()
    quarterly = [r for r in allq if "annual" not in (r["quarter_label"] or "").lower()]
    by_end = {r["period_end_date"]: r for r in quarterly}
    rows = []
    for r in quarterly:
        d = dict(r)
        if d.get("revenue_growth_pct") is None:
            d["revenue_growth_pct"] = _yoy(r, by_end, stored=False)
        rows.append(d)
    th = framework_rules.resolve(u, uid, sym)
    sz = framework_rules.sizing_plan(u, uid)
    first_idx = [k for k in range(len(rows)) if k >= MIN_QUARTERS - 1][-max_points:]
    start = (public_on(rows[first_idx[0]]["period_end_date"]) - timedelta(days=120)) if first_idx else date.today()
    with get_market_conn() as m:
        annual = annual_rows(m, sym)
        px = m.execute("SELECT date, close FROM price_history WHERE symbol = ? AND date >= ? ORDER BY date", (sym, start.isoformat())).fetchall()
    return replay(sym, rows, annual, stock["sector"] if stock else None, th, sz, Prices([(r["date"], r["close"]) for r in px]), max_points=max_points)


# ---------------------------------------------------------------------------------------------------- the chart (server-drawn SVG)

_BAND = {"BUY": ("var(--pos)", 1.0), "HOLD": ("var(--pos)", 0.45), "TRIM": ("var(--accent)", 0.85), "EXIT": ("var(--neg)", 0.9)}


def _band(p: dict) -> tuple[str, float]:
    if p["label"] == "Not enough data":
        return ("var(--muted)", 0.35)
    return _BAND.get(p["code"], ("var(--muted)", 0.5))


def chart_svg(tl: dict, width: int = 720, height: int = 230) -> str:
    pts, series = tl["points"], tl["series"]
    if not pts or len(series) < 2:
        return ""
    left, right, top, bottom, band_h = 6, 6, 12, 46, 14
    x0, x1 = _d(series[0][0]).toordinal(), _d(series[-1][0]).toordinal()
    span = max(x1 - x0, 1)
    lo, hi = min(c for _, c in series), max(c for _, c in series)
    rng = (hi - lo) or 1.0
    pw, ph = width - left - right, height - top - bottom

    def X(ds):
        return left + (_d(ds).toordinal() - x0) / span * pw

    def Y(c):
        return top + (1 - (c - lo) / rng) * ph

    line = " ".join(f"{X(d):.1f},{Y(c):.1f}" for d, c in series)
    by = height - bottom + 6
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="Price with your rule status at each past result date; the table below gives the same facts" '
           f'style="max-width:{width}px;height:auto;display:block">',
           f'<polyline points="{line}" fill="none" stroke="var(--text, currentColor)" stroke-width="1.6" stroke-linejoin="round" opacity=".85"/>']
    for i, p in enumerate(pts):
        x = X(p["asof"])
        nxt = X(pts[i + 1]["asof"]) if i + 1 < len(pts) else left + pw
        colour, op = _band(p)
        out.append(f'<rect x="{x:.1f}" y="{by}" width="{max(nxt - x, 1):.1f}" height="{band_h}" fill="{colour}" opacity="{op}"><title>{html.escape(p["asof"])}: {html.escape(p["label"])}</title></rect>')
        out.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{by}" stroke="{colour}" stroke-width="1" stroke-dasharray="3 3" opacity=".6"/>')
        if p["price"]:
            out.append(f'<circle cx="{x:.1f}" cy="{Y(p["price"]):.1f}" r="3.4" fill="{colour}" stroke="var(--surface, #fff)" stroke-width="1"><title>{html.escape(p["asof"])}: {html.escape(p["label"])}</title></circle>')
        d = _d(p["asof"])
        out.append(f'<text x="{x:.1f}" y="{height - 6}" font-size="9" text-anchor="middle" fill="var(--muted)">{d.strftime("%b %y")}</text>')
    out.append("</svg>")
    return "".join(out)
