import statistics
from collections import defaultdict
import re
from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db import get_market_conn
from app.ingestion.base import safe_growth_pct
from app.templates_config import templates

router = APIRouter()


_REFRESH = {"running": False}


def _refresh_metrics_if_stale() -> None:
    """The discovery screens read stock_metrics. Build it in the background when it is missing or older than a day and a half (about 30 s)."""
    import threading

    from app import discovery

    age = discovery.metrics_age_days()
    if (age is None or age > 1.5) and not _REFRESH["running"]:
        _REFRESH["running"] = True

        def go():
            try:
                discovery.build_metrics()
            finally:
                _REFRESH["running"] = False

        threading.Thread(target=go, daemon=True).start()


def _why(key: str, r: dict, custom: dict | None = None) -> str:
    fmt = lambda v, s="%": f"{v:.0f}{s}" if v is not None else None
    if key in ("rules", "browse"):
        j = r["judge"]
        return f"{j['passed']} of {j['total']} rules" + (f" · misses {j['failing'][0]}" if j["failing"] else "")
    if key == "custom":
        c = custom or {}
        bits = []
        if "roce" in c: bits.append(f"ROCE {fmt(r['roce'])}")
        if "growth" in c: bits.append(f"sales {r['sales_cagr_3y']:+.0f}%/yr" if r["sales_cagr_3y"] is not None else None)
        if "de" in c and r["de"] is not None: bits.append(f"debt/equity {r['de']:.2f}")
        if "pe" in c and r["pe"]: bits.append(f"P/E {r['pe']:.0f}×")
        if "opm" in c: bits.append(f"margin {fmt(r.get('opm'))}" if r.get("opm") is not None else None)
        if "div" in c: bits.append(f"pays out {fmt(r['div_payout'])}" if r["div_payout"] is not None else None)
        return " · ".join(b for b in bits if b) or "matches your filters"
    parts = {
        "compounders": [f"ROCE {fmt(r['roce_3y'])}+ every year (3y)", f"sales +{fmt(r['sales_cagr_3y'])}/yr"],
        "growers": [f"sales up {fmt(r['growth_min4'])}+ every quarter"],
        "financials": [f"ROE {r['raws']['roe']:.0f}%", f"ROA {r['raws']['roa']:.1f}%", f"{r['raws']['pb']:.1f}× book"] if all(k in r.get("raws", {}) for k in ("roe", "roa", "pb")) else [],
        "debtfree": [f"debt/equity {r['de']:.2f}" if r["de"] is not None else None],
        "cash": [f"cash flow {r['cfo_pat']:.1f}× profit" if r["cfo_pat"] is not None else None],
        "dividend": [f"pays out {fmt(r['div_payout'])} of profit"],
        "big": [f"{(r['cap_tier'] or '').capitalize()} cap"],
        "largest": [f"{(r['cap_tier'] or '').capitalize()} cap" if r["cap_tier"] else None],
        "cheap": [f"P/E {r['pe']:.0f}× vs its range" if r["pe"] else None],
    }.get(key, [])
    return " · ".join(x for x in parts if x)


_GOALS = [  # the "not sure where to start" chooser: plain goal -> screen
    ("safe", "Safe, low-debt businesses", "debtfree"), ("steady", "Steady growth I can hold for years", "compounders"),
    ("fast", "Fast growth", "growers"), ("income", "Regular income", "dividend"), ("known", "Big names I may know", "largest"),
]


@router.get("/analytics", response_class=HTMLResponse)
def analytics_landing(request: Request, screen: str = "rules", market: str = "IN", cap: str = "", sector: str = "",
                      roce: str = "", de: str = "", growth: str = "", pe: str = "", opm: str = "", div: str = "", sort: str = "", n: int = 60):
    from urllib.parse import urlencode

    from app import discovery
    from app import framework as fw
    from app.db import get_users_conn

    market = market if market in ("IN", "US", "ALL") else "IN"
    cap = cap if cap in discovery.CAPS else ""
    custom = {}
    for k, v in (("roce", roce), ("de", de), ("growth", growth), ("pe", pe), ("opm", opm), ("div", div)):
        try:
            if v != "":
                custom[k] = float(v)
        except ValueError:
            pass
    if sort in discovery.CUSTOM_SORTS:
        custom["sort"] = sort
    if screen == "custom" and not any(k in custom for k in discovery.CUSTOM_FIELDS):
        screen = "rules"
    if screen not in discovery.SCREEN_BY_KEY:
        screen = "rules"
    if screen == "rules" and (cap or sector):
        screen = "browse"  # a size/sector pick on the default screen means "browse this group"
    _refresh_metrics_if_stale()
    with get_users_conn() as u:
        th = fw.resolve(u, request.state.user_id)
        tracked = {r["symbol"] for r in u.execute("SELECT symbol FROM tracked_positions WHERE user_id = ? AND status = 'active'", (request.state.user_id,))}
    n = min(max(n, 60), 600)
    res = discovery.run_screen(screen, th, market, limit=n, cap=cap or None, sector=sector or None, custom=custom if screen == "custom" else None)
    for r in res["rows"]:
        r["why"] = _why(screen, r, custom)
        r["tracked"] = r["symbol"] in tracked
    fc = discovery.facets(market)

    def link(**kw):
        q = {"screen": screen, "market": market, "cap": cap, "sector": sector}
        q.update(kw)
        return "/analytics?" + urlencode({k: v for k, v in q.items() if v not in ("", None)})

    with get_market_conn() as conn:
        total_with_data = conn.execute("SELECT COUNT(DISTINCT symbol) c FROM quarterly_data").fetchone()["c"]
    return templates.TemplateResponse(
        "analytics_landing.html",
        {"request": request, "active_nav": "analytics", "total_with_data": total_with_data, "screens": discovery.SCREENS,
         "screen": discovery.SCREEN_BY_KEY[screen], "market": market, "cap": cap, "sector": sector, "custom": custom, "result": res,
         "facets": fc, "goals": _GOALS, "link": link, "preparing": discovery.metrics_age_days() is None, "n": n,
         "more_href": link(n=n + 60, **{k: v for k, v in (("roce", roce), ("de", de), ("growth", growth), ("pe", pe), ("opm", opm), ("div", div), ("sort", sort)) if v != ""}),
         "custom_open": screen == "custom"},
    )

def _delta(cur, prev, unit=""):
    if cur is None or prev is None:
        return None
    d = round(cur - prev, 2)
    sign = "pos" if d > 0 else ("neg" if d < 0 else "neutral")
    arrow = "▲" if d > 0 else ("▼" if d < 0 else "–")
    return {"cls": sign, "text": f"{arrow}{abs(d):g}{unit}"}


def _qoq_fallback(all_rows, rows) -> list:
    """Quarter-on-quarter revenue growth for the displayed columns, ONLY where the stored
    YoY figure is missing — aligned 1:1 with `rows`, None everywhere else.

    Why it exists: YoY needs the same quarter a year earlier, and for most India stocks
    the Jun/Sep/Dec 2025 quarters have no year-ago row anywhere (BSE's filing list only
    reaches back ~6 quarters; MoneyControl is annual-only). A blank tells the reader
    nothing; QoQ is computable from rows we already hold. It's derived here at read time,
    never stored in revenue_growth_pct, and the template labels it "QoQ" — it is a
    different metric (seasonal businesses swing quarter to quarter), so it is never mixed
    into the YoY value or the trend chart.

    Only a true adjacent QUARTER counts as the comparison: both rows quarterly (not a
    MoneyControl annual row, which shares the date key space) and ~one quarter apart."""
    by_date = {r["period_end_date"]: i for i, r in enumerate(all_rows)}
    out = []
    for r in rows:
        if r["revenue_growth_pct"] is not None or not r["revenue_cr"]:
            out.append(None)
            continue
        i = by_date.get(r["period_end_date"], 0)
        prev = all_rows[i - 1] if i > 0 else None
        if (
            prev is None or not prev["revenue_cr"]
            or "annual" in (r["quarter_label"] or "") or "annual" in (prev["quarter_label"] or "")
            or not 80 <= (date.fromisoformat(r["period_end_date"]) - date.fromisoformat(prev["period_end_date"])).days <= 100
        ):
            out.append(None)
            continue
        out.append(safe_growth_pct(r["revenue_cr"], prev["revenue_cr"]))
    return out


# ---------------------------------------------------------------------------------------------
# Raw-data presentation. The Analytics page is for investing decisions, so the underlying numbers
# come first: real period tables (quarterly and annual kept SEPARATE — mixing them created columns
# of dashes), value-labelled bar charts, and a full raw table. A row, column or chart with no data
# is never rendered.
# ---------------------------------------------------------------------------------------------

def _is_annual(r) -> bool:
    return "annual" in (r["quarter_label"] or "").lower()


def _money(v):
    if v is None:
        return None
    a = abs(v)
    return f"{v:,.0f}" if a >= 1000 else (f"{v:,.1f}" if a >= 100 else f"{v:,.2f}")


def _pct1(v):
    return None if v is None else f"{v:,.1f}%"


def _signed_pct(v):
    return None if v is None else f"{v:+,.1f}%"


def _num2(v):
    return None if v is None else f"{v:,.2f}"


def _int(v):
    return None if v is None else f"{v:,.0f}"


def _spark(values, w=84, h=22):
    """Tiny trend line for a table row. Needs 3+ real points; gaps break the line, never interpolated."""
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 3:
        return None
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = (hi - lo) or 1
    n = max(len(values) - 1, 1)
    xy = lambda i, v: (round(1 + (w - 2) * i / n, 1), round(h - 2 - (h - 4) * (v - lo) / span, 1))
    d, prev = [], None
    for i, v in enumerate(values):
        if v is None:
            prev = None
            continue
        x, y = xy(i, v)
        d.append(("L" if prev is not None else "M") + f"{x} {y}")
        prev = i
    lx, ly = xy(*pts[-1])
    return {"d": " ".join(d), "lx": lx, "ly": ly, "up": pts[-1][1] >= pts[0][1], "w": w, "h": h}


_ROW_NOTES = {
    "Revenue": "Net sales / income from operations as reported for the period.",
    "Revenue growth YoY": "This period's revenue ÷ the same period a year earlier − 1. The reported figure where the source gave one, otherwise computed from the rows we hold.",
    "Revenue growth QoQ": "This quarter's revenue ÷ the previous quarter's − 1. Seasonal businesses swing quarter to quarter — not comparable to YoY.",
    "Operating profit": "Revenue minus operating costs, before other income, interest and tax.",
    "Operating margin": "Operating profit ÷ revenue.",
    "Other income": "Non-operating income (treasury income, dividends, gains).",
    "Profit before tax": "Profit after interest and depreciation, before tax.",
    "Tax rate": "Tax expense ÷ profit before tax.",
    "Net profit": "Profit after tax attributable to the company.",
    "Net margin": "Net profit ÷ revenue.",
    "EPS": "Earnings per share; adjusted for verified splits/bonus issues where we have them.",
    "Finance costs": "Interest and other borrowing costs for the period (interest expense).",
    "Interest coverage": "(Profit before tax + finance costs) ÷ finance costs — how many times earnings cover interest. Not shown for banks and lenders.",
    "Operating cash flow": "Cash generated by operations in the period. US: derived quarter by quarter from year-to-date filings. India: reported once a year, so it appears in the annual table only.",
    "Cash flow ÷ profit": "Operating cash flow ÷ net profit for the period — how much of reported profit turned into cash. Not shown for banks and lenders.",
    "Investing cash flow": "Cash spent on (negative) or received from (positive) investments, capital expenditure and asset sales over the fiscal year. Annual only. A normalised figure, which can differ a few per cent from the filed statement.",
    "Financing cash flow": "Cash raised (positive) or returned (negative) through borrowings, repayments, dividends and buybacks over the fiscal year. Annual only.",
    "Free cash flow": "Operating cash flow minus capital expenditure for the fiscal year. Annual only.",
    "Current assets": "Cash, receivables, inventory and other assets expected to turn to cash within a year. India: fiscal year-end balance sheets only.",
    "Current liabilities": "Obligations due within a year. India: fiscal year-end balance sheets only.",
    "Inventory": "Inventories on the balance sheet.",
    "Current ratio": "Current assets ÷ current liabilities.",
    "Quick ratio": "(Current assets − inventory) ÷ current liabilities.",
    "Total debt": "Borrowings reported on the balance sheet.",
    "Cash & equivalents": "Cash and cash equivalents on the balance sheet.",
    "Net debt": "Total debt minus cash.",
    "Equity share capital": "Paid-up equity share capital.",
    "Book value per share": "Shareholders' equity ÷ shares outstanding.",
    "Shares outstanding": "Shares in issue at the period end.",
    "ROE": "Trailing net profit ÷ shareholders' equity (latest period only).",
    "ROCE": "Trailing operating profit ÷ capital employed (latest period only).",
    "Debt / equity": "Total debt ÷ shareholders' equity (latest period only).",
    "Promoter holding": "Share of the company held by promoters.",
    "Promoter pledge": "Share of promoter holding that is pledged.",
    "FII holding": "Share held by foreign institutional investors.",
    "DII holding": "Share held by domestic institutional investors.",
}


def _num1x(v):
    return None if v is None else f"{v:,.1f}"


def _row(label, values, fmt, unit="", bold=False, tone=None, spark=True):
    if all(v is None for v in values):
        return None
    cells = []
    for v in values:
        t = fmt(v) if v is not None else None
        cells.append({"t": t, "tone": ("pos" if v > 0 else "neg" if v < 0 else "") if (tone == "sign" and v is not None) else ""})
    return {"label": label, "unit": unit, "cells": cells, "bold": bold, "spark": _spark(values) if spark else None,
            "values": [None if v is None else round(float(v), 4) for v in values], "note": _ROW_NOTES.get(label)}


def _bars(title, labels, values, fmt, unit=""):
    """Value-labelled bar chart data (rendered as plain divs). None if fewer than 3 real points."""
    if sum(v is not None for v in values) < 3:
        return None
    real = [v for v in values if v is not None]
    lo, hi = min(0, min(real)), max(0, max(real))
    span = (hi - lo) or 1
    bars = []
    for lab, v in zip(labels, values):
        if v is None:
            bars.append({"label": lab, "text": None})
            continue
        top = (hi - max(v, 0)) / span * 100
        bars.append({"label": lab, "text": fmt(v), "top": round(top, 2), "h": round(abs(v) / span * 100, 2), "neg": v < 0})
    return {"title": title, "unit": unit, "bars": bars, "zero": round(hi / span * 100, 2)}


_VENDOR_LABEL = re.compile(r"\s*\((?:MoneyControl|IndianAPI)[^)]*\)", re.I)


def _next_result(symbol: str, ia_company) -> dict | None:
    """Next results date: the exchange-fed results_calendar first (refreshed daily), IndianAPI's board-meeting list as the fallback.
    A date that has just passed and is still 'upcoming' means the filing is being picked up ('updating')."""
    today = date.today()
    try:
        with get_market_conn() as m:
            row = m.execute("SELECT event_date, status FROM results_calendar WHERE symbol = ? AND event_date >= ? AND status = 'upcoming' ORDER BY event_date LIMIT 1",
                            (symbol, (today - timedelta(days=6)).isoformat())).fetchone()
    except Exception:  # noqa: BLE001 - table missing on an old DB: fall through to the fallback
        row = None
    if row:
        d = date.fromisoformat(row["event_date"])
        return {"label": "Results", "days": (d - today).days, "date": row["event_date"], "updating": d < today}
    ne = ia_company.get("next_event") if ia_company else None
    if ne:
        return {"label": "Results" if "esult" in (ne.get("text") or "") else "Board meeting", "days": ne["days"], "date": ne["date"][:10], "updating": False}
    return None


def _public_label(label):
    """Column tooltips never name the data vendor: 'FY2024 (MoneyControl, annual)' -> 'FY2024 (annual)'."""
    return _VENDOR_LABEL.sub(" (annual)", label or "") if label else label


def _yoy(r, by_end, stored=True):
    """YoY revenue growth: the stored figure when present, else computed from our own rows
    (same period one year earlier, +/-10 days)."""
    if stored and r["revenue_growth_pct"] is not None:
        return r["revenue_growth_pct"]
    if not r["revenue_cr"]:
        return None
    want = date.fromisoformat(r["period_end_date"]) - timedelta(days=365)
    for k, prior in by_end.items():
        if abs((date.fromisoformat(k) - want).days) <= 10 and prior["revenue_cr"]:
            return safe_growth_pct(r["revenue_cr"], prior["revenue_cr"])
    return None


def _pl_table(period_rows, peers, money_unit, eps_unit, with_qoq, bank=False):
    """Profit & loss table for a list of rows (all quarterly or all annual). `peers` = every row of
    the same kind, used to look up the year-ago / previous period."""
    by_end = {r["period_end_date"]: r for r in peers}
    ordered = sorted(peers, key=lambda r: r["period_end_date"])
    idx = {r["period_end_date"]: i for i, r in enumerate(ordered)}

    def qoq(r):
        i = idx[r["period_end_date"]]
        if i == 0 or not r["revenue_cr"] or not ordered[i - 1]["revenue_cr"]:
            return None
        gap = (date.fromisoformat(r["period_end_date"]) - date.fromisoformat(ordered[i - 1]["period_end_date"])).days
        return safe_growth_pct(r["revenue_cr"], ordered[i - 1]["revenue_cr"]) if 80 <= gap <= 100 else None

    col = lambda k: [r[k] for r in period_rows]
    rev, net = col("revenue_cr"), col("net_profit_cr")
    margin = [round(n / v * 100, 1) if (v and n is not None) else None for v, n in zip(rev, net)]
    eps = [r["eps_adjusted"] if r["eps_adjusted"] is not None else r["eps"] for r in period_rows]
    fc = col("finance_cost_cr")
    # Derived per period; not shown for banks/lenders (their interest and cash flows are the business, not leverage or earnings quality)
    cover = [None if bank or r["pbt_cr"] is None or not r["finance_cost_cr"] else round((r["pbt_cr"] + r["finance_cost_cr"]) / r["finance_cost_cr"], 1) for r in period_rows]
    cfo = col("cfo_cr")
    scol = lambda k: [(r[k] if k in r.keys() else None) for r in period_rows]  # columns only annual rows carry
    cfo_pat = [None if bank or r["cfo_cr"] is None or not r["net_profit_cr"] or r["net_profit_cr"] <= 0 else round(r["cfo_cr"] / r["net_profit_cr"], 2) for r in period_rows]
    rows = [
        _row("Revenue", rev, _money, money_unit, bold=True),
        _row("Revenue growth YoY", [_yoy(r, by_end) for r in period_rows], _signed_pct, "%", tone="sign"),
        _row("Revenue growth QoQ", [qoq(r) for r in period_rows], _signed_pct, "%", tone="sign") if with_qoq else None,
        _row("Operating profit", col("operating_profit_cr"), _money, money_unit),
        _row("Operating margin", col("opm_pct"), _pct1, "%"),
        _row("Other income", col("other_income_cr"), _money, money_unit),
        _row("Profit before tax", col("pbt_cr"), _money, money_unit),
        _row("Finance costs", fc, _money, money_unit),
        _row("Interest coverage", cover, _num1x, "x"),
        _row("Tax rate", col("tax_pct"), _pct1, "%"),
        _row("Net profit", net, _money, money_unit, bold=True, tone="sign"),
        _row("Net margin", margin, _pct1, "%"),
        _row("EPS", eps, _num2, eps_unit, bold=True, tone="sign"),
        _row("Operating cash flow", cfo, _money, money_unit, tone="sign"),
        _row("Cash flow ÷ profit", cfo_pat, _num2, "x"),
        None if bank else _row("Investing cash flow", scol("cfi_cr"), _money, money_unit, tone="sign"),
        None if bank else _row("Financing cash flow", scol("cff_cr"), _money, money_unit, tone="sign"),
        None if bank else _row("Free cash flow", scol("fcf_cr"), _money, money_unit, tone="sign"),
    ]
    labels = [_lab(r) for r in period_rows]
    fulls = [_public_label(r["quarter_label"]) for r in period_rows]
    charts = [c for c in (
        _bars("Revenue", labels, rev, _money, money_unit),
        _bars("Net profit", labels, net, _money, money_unit),
        _bars("EPS", labels, eps, _num2, eps_unit),
    ) if c]
    return {"columns": labels, "fulls": fulls, "ends": [r["period_end_date"] for r in period_rows], "rows": [x for x in rows if x], "charts": charts}


_BS_FIELDS = [
    ("Total debt", "total_debt_cr", _money, "money"), ("Cash & equivalents", "cash_cr", _money, "money"),
    ("Net debt", "net_debt_cr", _money, "money"), ("Equity share capital", "equity_share_capital_cr", _money, "money"),
    ("Book value per share", "book_value_per_share", _num2, "eps"), ("Shares outstanding", "shares_outstanding", _int, ""),
    ("Current assets", "current_assets_cr", _money, "money"), ("Current liabilities", "current_liabilities_cr", _money, "money"),
    ("Inventory", "inventory_cr", _money, "money"),
    ("ROE", "roe_pct", _pct1, "%"), ("ROCE", "roce_pct", _pct1, "%"), ("Debt / equity", "debt_equity", _num2, "x"),
]


def _balance_table(period_rows, money_unit, eps_unit, bank=False):
    """Balance-sheet items and trailing ratios: only columns (periods) that have at least one value,
    only rows that have at least one value."""
    cols = [r for r in period_rows if any(r[k] is not None for _, k, _, _ in _BS_FIELDS)]
    if not cols:
        return None
    unit = {"money": money_unit, "eps": eps_unit, "%": "%", "x": "x", "": ""}
    rows = [x for x in (_row(lbl, [r[k] for r in cols], fmt, unit[u], spark=False) for lbl, k, fmt, u in _BS_FIELDS) if x]
    if not bank:  # liquidity ratios per balance sheet we hold (banks have no current/non-current split)
        cur = [(r["current_assets_cr"], r["current_liabilities_cr"], r["inventory_cr"]) for r in cols]
        crt = [round(a / l, 2) if a is not None and l else None for a, l, _ in cur]
        qrt = [round((a - (i or 0)) / l, 2) if a is not None and l else None for a, l, i in cur]
        at = next((i for i, x in enumerate(rows) if x["label"] == "Inventory"), None)
        extra = [x for x in (_row("Current ratio", crt, _num2, "x", spark=False), _row("Quick ratio", qrt, _num2, "x", spark=False)) if x]
        rows[(at + 1 if at is not None else len(rows)):(at + 1 if at is not None else len(rows))] = extra
    return {"columns": [_lab(r) for r in cols], "ends": [r["period_end_date"] for r in cols], "rows": rows}


def _half_year_ends(d_from: date, d_to: date, months: set) -> list:
    out = []
    for y in range(d_from.year, d_to.year + 1):
        for m in sorted(months):
            nxt = date(y + (m == 12), m % 12 + 1, 1)
            d = nxt - timedelta(days=1)
            if d_from <= d <= d_to:
                out.append(d)
    return sorted(out)


def _balance_table_half(all_rows, annual_all, money_unit, eps_unit, bank=False, keep=6, filed_only=False):
    """Balance sheet and trailing ratios by HALF-YEAR end (fiscal year-end and the date six months earlier: March and September for most
    Indian companies). Indian companies file a balance sheet only twice a year, so June and December columns can never hold one.
    The "latest" balance figures are stamped on whichever quarter row was newest when they were stored; here each is moved onto the
    half-year end it describes, and years with no filed detail borrow borrowings / equity capital / ROCE from the annual statements."""
    g = lambda r, k: (r[k] if k in r.keys() else None)
    ends_src = annual_all[-1]["period_end_date"] if annual_all else None
    fy_m = int(ends_src[5:7]) if ends_src else 3
    months = {fy_m, (fy_m + 5) % 12 + 1}
    keys = [k for _, k, _, _ in _BS_FIELDS]
    data: dict = {}

    def half_end_on_or_before(d):
        ends = _half_year_ends(date(d.year - 1, 1, 1), d, months)
        return ends[-1] if ends else None

    held = [r for r in all_rows if any(g(r, k) is not None for k in keys)]
    for exact in (False, True):  # a row sitting on its own half-year end wins over figures moved onto it
        for r in sorted(held, key=lambda r: r["period_end_date"]):
            d = date.fromisoformat(r["period_end_date"])
            on_end = bool(_half_year_ends(d, d, months))
            if on_end != exact:
                continue
            target = d if on_end else half_end_on_or_before(d)
            if target is None:
                continue
            slot = data.setdefault(target, {})
            for k in keys:
                v = g(r, k)
                if v is not None and (exact or slot.get(k) is None):
                    slot[k] = v
    for a in annual_all:  # filed half-year detail is scarce: fill the year-end from the annual statements
        t = date.fromisoformat(a["period_end_date"])
        slot = data.setdefault(t, {})
        for dst, src in (("total_debt_cr", "borrowings_cr"), ("equity_share_capital_cr", "equity_capital_cr"), ("roce_pct", "roce_pct")):
            if slot.get(dst) is None and g(a, src) is not None:
                slot[dst] = g(a, src)
    for slot in data.values():
        if slot.get("net_debt_cr") is None and slot.get("total_debt_cr") is not None and slot.get("cash_cr") is not None:
            slot["net_debt_cr"] = slot["total_debt_cr"] - slot["cash_cr"]
    if filed_only:  # the page table: only half-years with a really filed balance sheet (cash, current assets, book value...), not year-ends padded from the annual statements
        filed = [k for k in keys if k not in ("total_debt_cr", "equity_share_capital_cr", "roce_pct")]
        dates = sorted(d for d, slot in data.items() if any(slot.get(k) is not None for k in filed))[-keep:]
        dates = [d for d in dates if dates and (dates[-1] - d).days <= 400]  # a snapshot years older than the latest is not "the latest balance sheet"
    else:
        dates = sorted(d for d, slot in data.items() if any(slot.get(k) is not None for k in keys))[-keep:]
    if not dates:
        return None
    unit = {"money": money_unit, "eps": eps_unit, "%": "%", "x": "x", "": ""}
    rows = [x for x in (_row(lbl, [data[d].get(k) for d in dates], fmt, unit[u], spark=False) for lbl, k, fmt, u in _BS_FIELDS) if x]
    if not bank:
        cur = [(data[d].get("current_assets_cr"), data[d].get("current_liabilities_cr"), data[d].get("inventory_cr")) for d in dates]
        crt = [round(a / l, 2) if a is not None and l else None for a, l, _ in cur]
        qrt = [round((a - (i or 0)) / l, 2) if a is not None and l else None for a, l, i in cur]
        at = next((i for i, x in enumerate(rows) if x["label"] == "Inventory"), None)
        extra = [x for x in (_row("Current ratio", crt, _num2, "x", spark=False), _row("Quick ratio", qrt, _num2, "x", spark=False)) if x]
        rows[(at + 1 if at is not None else len(rows)):(at + 1 if at is not None else len(rows))] = extra
    return {"columns": [d.strftime("%b %Y") for d in dates], "ends": [d.isoformat() for d in dates], "rows": rows} if rows else None


_ANNUAL_BS = [
    ("Equity capital", "equity_capital_cr", _money, "money"), ("Reserves", "reserves_cr", _money, "money"), ("Borrowings", "borrowings_cr", _money, "money"),
    ("Other liabilities", "other_liabilities_cr", _money, "money"), ("Total liabilities", "total_liabilities_cr", _money, "money"),
    ("Fixed assets", "fixed_assets_cr", _money, "money"), ("Capital work in progress", "cwip_cr", _money, "money"), ("Investments", "investments_cr", _money, "money"),
    ("Other assets", "other_assets_cr", _money, "money"), ("Total assets", "total_assets_cr", _money, "money"),
    ("ROCE", "roce_pct", _pct1, "%"), ("Debtor days", "debtor_days", _int, "days"), ("Inventory days", "inventory_days", _int, "days"),
    ("Days payable", "days_payable", _int, "days"), ("Cash conversion cycle", "cash_conversion_cycle", _int, "days"), ("Working capital days", "working_capital_days", _int, "days"),
    ("Dividend payout", "dividend_payout_pct", _pct1, "%"),
]


def _annual_balance_table(annual_rows, money_unit):
    """Fiscal-year balance sheet and efficiency ratios (IndianAPI annual statements). Only years/rows that have a value."""
    g = lambda r, k: r[k] if k in r.keys() else None  # older MoneyControl annual rows (sqlite Rows) have none of these columns
    cols = [r for r in annual_rows if any(g(r, k) is not None for _, k, _, _ in _ANNUAL_BS)]
    if not cols:
        return None
    unit = {"money": money_unit, "%": "%", "days": "days"}
    rows = [x for x in (_row(lbl, [g(r, k) for r in cols], fmt, unit[u], spark=False) for lbl, k, fmt, u in _ANNUAL_BS) if x]
    return {"columns": [_lab(r) for r in cols], "ends": [r["period_end_date"] for r in cols], "rows": rows} if rows else None


def _verdict(request, stock, quarter_rows, annual):
    """Summary of the user's investment rules for this stock (Snapshot strip). None when it can't be computed - the strip is then omitted."""
    try:
        from app import framework as fr
        from app.ai.checklist import evaluate_framework
        from app.db import get_users_conn
        uid = getattr(request.state, "user_id", None)
        with get_users_conn() as u:
            th = fr.resolve(u, uid, stock["symbol"]) if uid else None
        by_end = {r["period_end_date"]: r for r in quarter_rows}
        rows = []
        for r in quarter_rows[-8:]:
            d = dict(r)
            if d.get("revenue_growth_pct") is None:
                d["revenue_growth_pct"] = _yoy(r, by_end, stored=False)
            rows.append(d)
        fw = evaluate_framework(rows, stock["sector"], th, annual=annual)
    except Exception:  # noqa: BLE001 - the verdict is an extra, never worth failing the page
        return None
    ev = [f for f in fw["filters"] if f["status"] in ("pass", "fail")]
    if not ev:
        return None
    failed = [{"label": f["label"], "value": f["value"], "threshold": f["threshold"]} for f in ev if f["status"] == "fail"]
    return {"passed": len(ev) - len(failed), "total": len(ev), "failed": failed[:4], "more": max(0, len(failed) - 4),
            "unknown": sum(1 for f in fw["filters"] if f["status"] == "unknown")}


def _ownership_table(quarter_rows):
    def pct(v):
        return None if v is None else f"{v:,.2f}%"
    rows = [x for x in (
        _row("Promoter holding", [r["promoter_pct"] for r in quarter_rows], pct, "%"),
        _row("Promoter pledge", [r["promoter_pledge_pct"] for r in quarter_rows], pct, "%"),
        _row("FII holding", [r["fii_pct"] for r in quarter_rows], pct, "%"),
        _row("DII holding", [r["dii_pct"] for r in quarter_rows], pct, "%"),
    ) if x]
    return {"columns": [_lab(r) for r in quarter_rows], "ends": [r["period_end_date"] for r in quarter_rows], "rows": rows} if rows else None


def _num(v, suffix="", prefix="", places=None):
    """Format a possibly-None number for display. None -> '—', never crashes."""
    if v is None:
        return "—"
    if places is not None:
        return f"{prefix}{v:,.{places}f}{suffix}"
    return f"{prefix}{v:g}{suffix}"


def _last_non_null(values):
    return next((v for v in reversed(values) if v is not None), None)


def _first_non_null(values):
    return next((v for v in values if v is not None), None)


def _compute_pe(conn, symbol: str, quarterly_rows) -> tuple:
    """P/E needs EPS (from fundamentals) and price (from a separate source, e.g.
    Tiingo) — neither ingestion source has both, so this is combined here at read
    time rather than stored by either ingestion job. Returns (current, low_3yr,
    high_3yr) — any/all None if either side of the data is missing.

    Simplification: the 3-year range divides the 3-year price range by *today's*
    TTM EPS, not the EPS at each historical point in time — a common quick-and-dirty
    approach, but it understates the range for a company whose earnings changed a lot
    over the period. A precise version would need EPS matched to price date, not built.
    """
    eps_values = [r["eps"] for r in quarterly_rows[-4:]]
    if len(quarterly_rows) < 4 or any(v is None for v in eps_values):
        return None, None, None
    ttm_eps = sum(eps_values)
    if not ttm_eps or ttm_eps <= 0:
        return None, None, None  # a P/E on negative/zero trailing earnings isn't meaningful

    latest_price = conn.execute(
        "SELECT close FROM price_history WHERE symbol = ? ORDER BY date DESC LIMIT 1", (symbol,)
    ).fetchone()
    if not latest_price:
        return None, None, None

    since = (date.today() - timedelta(days=3 * 365)).isoformat()
    range_row = conn.execute(
        "SELECT MIN(close) lo, MAX(close) hi FROM price_history WHERE symbol = ? AND date >= ?", (symbol, since)
    ).fetchone()

    pe_current = round(latest_price["close"] / ttm_eps, 1)
    pe_low = round(range_row["lo"] / ttm_eps, 1) if range_row and range_row["lo"] is not None else None
    pe_high = round(range_row["hi"] / ttm_eps, 1) if range_row and range_row["hi"] is not None else None
    return pe_current, pe_low, pe_high


def _period_label(period_end_date: str, quarter_label: str, symbol: str | None = None) -> str:
    """Display label for a column header: "FY2012" for an annual period (keeps the annual/quarterly distinction visible), and for a quarter
    the convention of the company's own market. India (NSE: symbols) reports on an April-March fiscal year, so Apr-Jun 2026 is "Q1 FY27" and
    Jan-Mar 2026 is "Q4 FY26" (what every Indian results announcement says). Other markets keep the calendar quarter ("Q2 2026"): US fiscal
    years end in all twelve months, so no single fiscal convention fits them. Peer comparison still buckets by calendar quarter (_quarter_bucket)."""
    d = date.fromisoformat(period_end_date)
    if "annual" in quarter_label.lower():
        return f"FY{d.year}"
    if symbol and symbol.startswith("NSE:"):
        fy = d.year + 1 if d.month >= 4 else d.year
        return f"Q{((d.month - 4) % 12) // 3 + 1} FY{fy % 100:02d}"
    return f"Q{(d.month - 1) // 3 + 1} {d.year}"


def _lab(r) -> str:
    """Column label for a quarterly_data / annual row (uses its own symbol to pick the fiscal convention)."""
    try:
        sym = r["symbol"]
    except (KeyError, IndexError):
        sym = None
    return _period_label(r["period_end_date"], r["quarter_label"], sym)


def _quarter_bucket(period_end_date: str) -> str:
    """Companies in the same sector rarely share an exact fiscal quarter-end date
    (different fiscal year-ends) — bucket by calendar year + calendar quarter
    instead, so peer comparison has something to actually match against."""
    d = date.fromisoformat(period_end_date)
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def _nearest_on_or_before(sorted_dates: list[str], target: str, tolerance_days: int = 10) -> str | None:
    """Latest date in a sorted list that's <= target, within `tolerance_days` —
    peers rarely have a price on the exact date needed (weekends, holidays,
    listing gaps), same tolerance-window join pattern used elsewhere in this app."""
    import bisect

    idx = bisect.bisect_right(sorted_dates, target) - 1
    if idx < 0:
        return None
    candidate = sorted_dates[idx]
    if (date.fromisoformat(target) - date.fromisoformat(candidate)).days > tolerance_days:
        return None
    return candidate


def _compute_sector_benchmark(conn, symbol: str, sector: str, rows) -> dict:
    """Layer 2 (relative-to-sector, earnings-based) and Layer 4 (sector momentum,
    now price-based) — both computed here, not stored by ingestion: sector
    classification only recently landed at scale, and this is pure aggregation over
    data already in the DB (no new source needed), same pattern as _compute_pe.

    Sector momentum used to approximate from peers' average EARNINGS growth
    (a real, flagged simplification — Layer 4 was always scoped as price-derived).
    Now genuinely price-based: for each of this stock's quarters, takes every
    sector peer's trailing ~3-month price return ending at that quarter's date,
    and classifies the SECTOR's momentum from the peer-average return — i.e. "are
    this stock's peers being bid up or sold off," not "are their earnings growing."
    """
    if not sector or sector == "Unclassified":
        return {}

    market = conn.execute("SELECT market FROM stocks WHERE symbol = ?", (symbol,)).fetchone()["market"]

    peer_rows = conn.execute(
        """
        SELECT qd.period_end_date, qd.revenue_growth_pct, qd.opm_pct
        FROM quarterly_data qd
        JOIN stocks s ON s.symbol = qd.symbol
        WHERE s.sector = ? AND s.market = ? AND qd.symbol != ?
              AND qd.revenue_growth_pct IS NOT NULL
        """,
        (sector, market, symbol),
    ).fetchall()

    buckets: dict[str, dict] = {}
    for r in peer_rows:
        b = _quarter_bucket(r["period_end_date"])
        slot = buckets.setdefault(b, {"growth": [], "margin": []})
        slot["growth"].append(r["revenue_growth_pct"])
        if r["opm_pct"] is not None:
            slot["margin"].append(r["opm_pct"])

    def _sector_momentum(as_of: str) -> str | None:
        """Peer-average trailing ~3-month price return ending at `as_of`. Reads only the two
        prices needed per peer through the (symbol, date) primary-key index — the previous
        version loaded every peer's FULL daily history into memory (sectors have 500+ peers
        with years of prices), ~7s per stock page."""
        lookback = (date.fromisoformat(as_of) - timedelta(days=91)).isoformat()
        px = (
            "(SELECT {col} FROM price_history p WHERE p.symbol = s.symbol AND p.date <= ? "
            "AND p.date >= date(?, '-10 day') ORDER BY p.date DESC LIMIT 1)"
        )
        rows_ = conn.execute(
            f"SELECT {px.format(col='date')} AS end_d, {px.format(col='close')} AS end_px, "
            f"{px.format(col='date')} AS start_d, {px.format(col='close')} AS start_px "
            "FROM stocks s WHERE s.sector = ? AND s.market = ? AND s.symbol != ?",
            (as_of, as_of, as_of, as_of, lookback, lookback, lookback, lookback, sector, market, symbol),
        ).fetchall()
        returns = []
        for r in rows_:
            if r["end_d"] is None or r["start_d"] is None or r["start_d"] == r["end_d"] or not r["start_px"] or r["end_px"] is None:
                continue
            returns.append((r["end_px"] / r["start_px"] - 1) * 100)
        if len(returns) < 3:  # same 3-peer minimum as the earnings-based comparison
            return None
        avg_return = sum(returns) / len(returns)
        return "favor" if avg_return > 5 else ("out" if avg_return < -5 else "neutral")

    result = {}
    for row in rows:
        b = _quarter_bucket(row["period_end_date"])
        slot = buckets.get(b)
        growth_vs = margin_vs = None
        if slot and len(slot["growth"]) >= 3:  # same "3 peers or it's noise" rule as before
            # Median, not mean — real bug found live (SBICARD, "Financial Services"):
            # revenue_growth_pct and opm_pct both blow up for any peer with a near-zero
            # prior-year or current-quarter revenue (common among the tiny, erratic
            # micro-caps that share a broad sector bucket like this one — confirmed
            # peers swinging -900% to +708.6% in the same quarter). A plain mean over
            # 543 such peers came out to a 722.8% "average growth" and a -253.6%
            # "average margin" — both meaningless — which propagated into every real
            # stock's vs-sector delta as a wild, clearly-wrong number (confirmed: this
            # produced the exact -719.4 growth-vs-sector the user saw live). Median is
            # robust to the handful of extreme outliers that always exist in a large,
            # heterogeneous peer pool; same data, with median: 16.8% and 40.6% — both
            # sane — and SBICARD's own delta drops from -719.4 to a believable -13.4.
            avg_growth = statistics.median(slot["growth"])
            avg_margin = statistics.median(slot["margin"]) if slot["margin"] else None
            growth_vs = round(row["revenue_growth_pct"] - avg_growth, 1) if row["revenue_growth_pct"] is not None else None
            margin_vs = round(row["opm_pct"] - avg_margin, 1) if row["opm_pct"] is not None and avg_margin is not None else None

        momentum = _sector_momentum(row["period_end_date"])
        if growth_vs is None and margin_vs is None and momentum is None:
            continue
        result[row["period_end_date"]] = {"growth_vs_sector": growth_vs, "margin_vs_sector": margin_vs, "sector_momentum": momentum}
    return result


def _price_block(conn, symbol: str, market: str):
    """Everything the price chart + KPI strip need, from price_history only. Returns
    (summary, series): `series` is downsampled for the page payload — daily for the last
    ~2 years (so 1M/6M/1Y ranges stay crisp), weekly before that."""
    rows = conn.execute("SELECT date, close FROM price_history WHERE symbol = ? ORDER BY date", (symbol,)).fetchall()
    if len(rows) < 2:  # one point can't draw a line — show the "no price history yet" state instead
        return None, None
    last = rows[-1]
    last_d = date.fromisoformat(last["date"])
    year_ago = (last_d - timedelta(days=365)).isoformat()
    window = [r["close"] for r in rows if r["date"] >= year_ago]
    base = next((r["close"] for r in reversed(rows) if r["date"] <= year_ago), None)
    summary = {
        "last": last["close"],
        "as_of": last["date"],
        "ret_1y": round((last["close"] / base - 1) * 100, 1) if base else None,
        "hi_52w": max(window) if window else None,
        "lo_52w": min(window) if window else None,
        "currency": "₹" if market == "IN" else "$",
    }
    recent_n = 520
    older, recent = rows[:-recent_n], rows[-recent_n:]
    keep = older[::5] + recent
    series = {"d": [r["date"] for r in keep], "c": [round(r["close"], 2) for r in keep]}
    return summary, series


_RANGE_OPTIONS = ["4", "8", "12", "all"]
_DEFAULT_RANGE = "12"


@router.get("/announcements/{symbol:path}", response_class=HTMLResponse)
def announcements_panel(request: Request, symbol: str):
    """Lazy-loaded 'Recent filings' card for the stock page (htmx)."""
    from app.announcements import get_announcements
    with get_market_conn() as m:
        ok = m.execute("SELECT 1 FROM stocks WHERE symbol = ? AND market = 'IN'", (symbol,)).fetchone()
    if not ok:
        return HTMLResponse("")
    return templates.TemplateResponse("_announcements.html", {"request": request, "A": get_announcements(symbol)})


def _delivery_block(conn, symbol: str):
    """Latest delivery %, its 20-day and 1-year averages (NSE). None when the stock has no delivery data."""
    rows = conn.execute("SELECT date, deliv_pct FROM delivery_data WHERE symbol = ? ORDER BY date DESC LIMIT 250", (symbol,)).fetchall()
    if not rows:
        return None
    pcts = [r["deliv_pct"] for r in rows]
    avg = lambda xs: round(sum(xs) / len(xs), 1)
    out = {"latest": round(pcts[0], 1), "date": rows[0]["date"], "avg20": avg(pcts[:20]), "n": len(pcts)}
    if len(pcts) >= 60:
        out["avg250"] = avg(pcts)
        out["vs"] = round(out["avg20"] - out["avg250"], 1)
    return out


@router.get("/analytics/{symbol:path}", response_class=HTMLResponse)
def analytics_screen(request: Request, symbol: str, span: str = _DEFAULT_RANGE):
    with get_market_conn() as conn:
        stock = conn.execute("SELECT * FROM stocks WHERE symbol = ?", (symbol,)).fetchone()
        all_rows = conn.execute(
            "SELECT * FROM quarterly_data WHERE symbol = ? ORDER BY period_end_date", (symbol,)
        ).fetchall()

    if stock is None or not all_rows:
        return HTMLResponse(f"<p>Unknown symbol {symbol}</p>", status_code=404)

    # A stock with real deep history (e.g. moneycontrol's 20+ years) used to dump every
    # period as a column, forcing a very wide horizontal scroll just to read the last
    # few quarters — this windows the matrix/details tables down to a selectable range
    # instead, defaulting to the last 8. The full history is still one click away
    # ("All"), and every downstream computation below keeps using the FULL history where
    # that's actually correct (3yr P/E range, TTM EPS) — only the displayed columns and
    # their per-quarter series are windowed. (Param named `span`, not `range` — a
    # `range` query param would shadow the builtin used all through this function.)
    current_range = span if span in _RANGE_OPTIONS else _DEFAULT_RANGE
    rows = all_rows if current_range == "all" else all_rows[-int(current_range):]

    # Real quarter_label values are wildly inconsistent in verbosity depending on
    # source/vintage ("FY2012 (MoneyControl, annual)" vs "Q ending 2026-06-30") and
    # used to be thrown away entirely in favor of a sequential Q1..Qn across the
    # visible window (same convention the mock fixtures always used) — meaningless
    # on a real stock, and actively misleading for a symbol like SBICARD whose
    # history mixes 12 ANNUAL MoneyControl periods with 6 real BSE quarters: all 18
    # looked like uniform "quarters" even though 12 of them are yearly totals.
    # Derived from period_end_date instead: "FY2012" for an annual period (keeps the
    # annual/quarterly distinction visible instead of hiding it), "Q2 2026" for a
    # real quarter (calendar quarter — this app doesn't try to infer each company's
    # own fiscal-quarter convention, which varies by market and by filer).
    quarters = [_lab(r) for r in rows]
    quarter_full_labels = [_public_label(r["quarter_label"]) for r in rows]

    def series(key):
        return [r[key] for r in rows]

    growth = series("revenue_growth_pct")
    growth_qoq = _qoq_fallback(all_rows, rows)
    margin = series("opm_pct")
    growth_deltas = [None] + [_delta(growth[i], growth[i - 1]) for i in range(1, len(growth))]
    margin_deltas = [None] + [_delta(margin[i], margin[i - 1], "") for i in range(1, len(margin))]

    # Layer 2/4 — stored columns (growth_vs_sector_pp etc.) are only ever populated by
    # mock data; real rows compute this at read time (see _compute_sector_benchmark),
    # since sector classification only recently landed at scale. Stored value wins
    # when present (mock), computed value fills in otherwise (real).
    with get_market_conn() as sconn:
        sector_bench = _compute_sector_benchmark(sconn, symbol, stock["sector"], rows)
    growth_vs_sector = [r["growth_vs_sector_pp"] if r["growth_vs_sector_pp"] is not None
                        else sector_bench.get(r["period_end_date"], {}).get("growth_vs_sector") for r in rows]
    margin_vs_sector = [r["margin_vs_sector_pp"] if r["margin_vs_sector_pp"] is not None
                        else sector_bench.get(r["period_end_date"], {}).get("margin_vs_sector") for r in rows]
    sector_momentum = [r["sector_momentum"] if r["sector_momentum"] is not None
                       else sector_bench.get(r["period_end_date"], {}).get("sector_momentum") for r in rows]

    # Layer 3 (US) — sec_13f.py aggregates across ~10,000 institutional managers'
    # own filings, a coarser cadence than quarterly_data's per-company rows, so this
    # is the latest known snapshot rather than a per-quarter series like the rest of
    # the matrix. India's FII%/promoter fields (below) are a different concept
    # entirely (ownership mix, not manager count) — both can be real at once.
    with get_market_conn() as iconn:
        inst = iconn.execute(
            "SELECT * FROM institutional_holdings WHERE symbol = ? ORDER BY period_end_date DESC LIMIT 1", (symbol,)
        ).fetchone()

    # institutional_holdings.total_shares_held has no % of the company built in — it's
    # a raw share count. shares_outstanding lives on quarterly_data instead (only the
    # latest period, same gating as book_value_per_share — see sec_edgar_source.py),
    # so this is computed at read time, same convention as Layer 2/4 sector benchmarking.
    institutional_pct = None
    if inst and inst["total_shares_held"]:
        shares_out = next((r["shares_outstanding"] for r in rows if r["shares_outstanding"]), None)
        if shares_out:
            institutional_pct = round(inst["total_shares_held"] / shares_out * 100, 1)

    # Layer 5 (Macro) — market-level, not per-symbol or even per-quarter (US/FRED is
    # quarterly-ish, India/World Bank is annual) — latest known snapshot, same
    # approach as Layer 3 above.
    with get_market_conn() as mconn:
        macro = mconn.execute(
            "SELECT * FROM macro_context WHERE market = ? ORDER BY period_end_date DESC LIMIT 1", (stock["market"],)
        ).fetchone()

    matrix = {
        "quarters": quarters,
        "revenue_growth": list(zip(growth, growth_deltas)),
        "revenue_growth_qoq": growth_qoq,
        "opm": list(zip(margin, margin_deltas)),
        "guidance": series("guidance_vs_delivered"),
        "growth_vs_sector": growth_vs_sector,
        "margin_vs_sector": margin_vs_sector,
        "fii": series("fii_pct"),
        "promoter_pct": rows[-1]["promoter_pct"],
        "promoter_pledge": rows[-1]["promoter_pledge"],
        "institutional_holders": inst["holder_count"] if inst else None,
        "institutional_shares": inst["total_shares_held"] if inst else None,
        "institutional_qoq_pct": inst["shares_qoq_change_pct"] if inst else None,
        "institutional_period": inst["period_end_date"] if inst else None,
        "institutional_pct": institutional_pct,
        "sector_momentum": sector_momentum,
        "macro_policy_rate": macro["policy_rate_pct"] if macro else None,
        "macro_cpi_yoy": macro["cpi_yoy_pct"] if macro else None,
        "macro_gdp_growth": macro["gdp_growth_pct"] if macro else None,
        "macro_unemployment": macro["unemployment_pct"] if macro else None,
        "macro_period": macro["period_end_date"] if macro else None,
        # Real gap found live (TMCV): checking growth_vs_sector alone silently hid a
        # stock's real margin_vs_sector values whenever its own revenue_growth_pct
        # happened to be permanently None (margin vs. sector only needs opm_pct, a
        # genuinely independent precondition) — the whole Layer 2 table collapsed to
        # "not sourced" even though half its data was real and ready to show.
        "has_layer2": any(v is not None for v in growth_vs_sector) or any(v is not None for v in margin_vs_sector),
        "has_layer3": any(v is not None for v in series("fii_pct")) or rows[-1]["promoter_pct"] is not None or inst is not None,
        "has_layer5": macro is not None,
    }

    # Per-layer trend data for the table/trend toggle on the Quarterly Quality
    # Matrix — same raw series already computed above, just regrouped by layer and
    # JSON-serialized for the chart-drawing script. A metric is left out entirely
    # (not shown as an empty chart) when every value in the visible window is None,
    # same "only show what's real" rule the table view already follows.
    def _trend_metric(label, unit, values):
        return {"label": label, "unit": unit, "values": values} if any(v is not None for v in values) else None

    trend_data = {
        "periods": quarters,
        "layers": {
            "l1": [m for m in [
                _trend_metric("Revenue growth YoY", "%", growth),
                _trend_metric("Operating margin", "%", margin),
            ] if m],
            "l2": [m for m in [
                _trend_metric("Growth vs. sector", "pp", growth_vs_sector),
                _trend_metric("Margin vs. sector", "pp", margin_vs_sector),
            ] if m],
            "l3": [m for m in [
                _trend_metric("FII holding", "%", matrix["fii"]),
            ] if m],
        },
    }

    # chips: quick-read state for the header — each one only appears if its data exists.
    # Real-ingested rows (e.g. SEC EDGAR) only populate Layer 1, so Layer 2/3 chips are
    # conditional, not assumed present the way they always were with all-mock data.
    chips = []
    last_margin, prev_margin = _last_non_null(margin), _last_non_null(margin[:-1]) if len(margin) > 1 else None
    if last_margin is not None and prev_margin is not None:
        trend = "pos" if last_margin > prev_margin else ("neg" if last_margin < prev_margin else "neutral")
        chips.append({"cls": trend, "text": f"Margin: {'Expanding' if trend == 'pos' else 'Contracting' if trend == 'neg' else 'Flat'}"})

    growth_edge = growth_vs_sector[-1]
    if growth_edge is not None:
        chips.append({"cls": "accent" if growth_edge > 0 else "neutral", "text": f"Growth: {'Beating' if growth_edge > 0 else 'Trailing'} sector"})

    fii_last, fii_first = _last_non_null(matrix["fii"]), _first_non_null(matrix["fii"])
    if fii_last is not None and fii_first is not None:
        rising = fii_last > fii_first
        chips.append({"cls": "pos" if rising else "neutral", "text": f"FII: {'Rising' if rising else 'Falling'} over period"})

    if matrix["institutional_qoq_pct"] is not None:
        qoq = matrix["institutional_qoq_pct"]
        chips.append({"cls": "pos" if qoq > 0 else ("neg" if qoq < 0 else "neutral"),
                      "text": f"Institutions: {'Accumulating' if qoq > 0 else 'Distributing' if qoq < 0 else 'Flat'} QoQ"})

    if not chips:
        chips.append({"cls": "neutral", "text": "Relative/institutional layers not sourced for this stock yet"})

    revenue_raw = series("revenue_cr")
    op_profit_raw = series("operating_profit_cr")
    opm_raw = series("opm_pct")
    other_income_raw = series("other_income_cr")
    net_profit_raw = series("net_profit_cr")

    pnl_rows = [
        ("Revenue (₹ Cr / $ M)", [_num(v, places=0) for v in revenue_raw], True),
        ("Operating profit", [_num(v, places=0) for v in op_profit_raw], False),
        ("OPM %", [_num(v, suffix="%", places=1) for v in opm_raw], False),
        ("Other income", [_num(v, places=0) for v in other_income_raw], False),
        ("Net profit", [_num(v, places=0) for v in net_profit_raw], True),
    ]

    # Prefer eps_adjusted (retroactively adjusted for a LATER verified bonus/split,
    # see app/data/corporate_actions.json) over the raw as-filed eps, so the series
    # reads as one continuous trend instead of dropping off a cliff at a bonus date
    # — a real discontinuity confirmed live this session (HDFC Bank, Wipro, Infosys)
    # that as-filed EPS alone made look like a business collapse. Only ever applies
    # to a symbol with a verified corporate action on file; everyone else's EPS row
    # is unchanged, still the plain as-filed figure.
    eps_values = [(r["eps_adjusted"] if r["eps_adjusted"] is not None else r["eps"]) for r in rows]
    eps_label = "EPS" + (" (split/bonus-adjusted)" if any(r["eps_adjusted"] is not None for r in rows) else "")
    pnl_rows.append((eps_label, [_num(v, places=2) for v in eps_values], False))

    pnl_trend = {
        "periods": quarters,
        "metrics": [m for m in [
            _trend_metric("Revenue", "", revenue_raw),
            _trend_metric("Operating profit", "", op_profit_raw),
            _trend_metric("OPM", "%", opm_raw),
            _trend_metric("Other income", "", other_income_raw),
            _trend_metric("Net profit", "", net_profit_raw),
            _trend_metric(eps_label, "", eps_values),
        ] if m],
    }

    # Balance sheet fields are point-in-time snapshots that not every period has —
    # bse_source.py/sec_edgar_source.py only compute them on a stock's single latest
    # quarter (balance sheet detail only exists in the annual filing), while
    # moneycontrol_source.py's annual rows carry them on every period. A plain
    # Originally quarters[-4:]/series(...)[-4:] — sliced the last 4 periods by
    # POSITION, which for a stock with real MoneyControl history was 3 blank recent
    # BSE quarters and one real one. Fixed to take the periods that actually HAVE a
    # balance-sheet value instead, from any source — but then hard-capped to the
    # last 4 of THOSE, which made sense when balance-sheet data really was only a
    # shallow ~2yr window (db.py's own comment on the column still says as much).
    # That's stale now: the multi-year MoneyControl/BSE backfill this session means
    # real balance-sheet history often runs 15-20+ periods deep (confirmed live —
    # RELIANCE has 21, ICICIBANK 15). No cap now, same as the main Quarterly Matrix
    # above, which was never capped either — this table already scrolls
    # horizontally for the same reason that one does.
    bs_source_rows = [r for r in rows if r["total_debt_cr"] is not None or r["net_debt_cr"] is not None or r["cash_cr"] is not None]
    bs_quarters = [_lab(r) for r in bs_source_rows]
    bs_total_debt_raw = [r["total_debt_cr"] for r in bs_source_rows]
    bs_net_debt_raw = [r["net_debt_cr"] for r in bs_source_rows]
    bs_cash_raw = [r["cash_cr"] for r in bs_source_rows]
    bs_rows = [
        ("Total debt", [_num(v, places=0) for v in bs_total_debt_raw]),
        ("Net debt", [_num(v, places=0) for v in bs_net_debt_raw]),
        ("Cash & equivalents", [_num(v, places=0) for v in bs_cash_raw]),
    ]
    has_balance_sheet = any(v != "—" for row in bs_rows for v in row[1])

    bs_trend = {
        "periods": bs_quarters,
        "metrics": [m for m in [
            _trend_metric("Total debt", "", bs_total_debt_raw),
            _trend_metric("Net debt", "", bs_net_debt_raw),
            _trend_metric("Cash & equivalents", "", bs_cash_raw),
        ] if m],
    }

    latest = rows[-1]
    with get_market_conn() as conn:
        pe_current, pe_low, pe_high = _compute_pe(conn, symbol, rows)
    from app.annual import annual_rows as _annual_rows_for_ratios
    from app.ratios import filings_roce, ia_year_ratios
    with get_market_conn() as _rc:
        _ia_y = ia_year_ratios(_annual_rows_for_ratios(_rc, symbol), latest["period_end_date"]) if symbol.startswith("NSE:") else None
    # One source per stock: IndianAPI's financial-year ROCE / ROE / debt-to-equity where it has the year, otherwise the filings (ROCE Screener-style, India)
    _roe = _ia_y["roe"] if _ia_y and _ia_y["roe"] is not None else latest["roe_pct"]
    _roce = _ia_y["roce"] if _ia_y and _ia_y["roce"] is not None else (filings_roce([dict(r) for r in rows]) if symbol.startswith("NSE:") else None)
    _roce = _roce if _roce is not None else latest["roce_pct"]
    from app.ai.checklist import is_bank_or_financial as _is_fin
    _de = _ia_y["de"] if _ia_y and _ia_y["de"] is not None and not _is_fin(stock["sector"]) else latest["debt_equity"]  # a lender's borrowings are its business: no debt/equity for it
    ratio_stats = [
        ("ROE", _num(_roe, suffix="%", places=1)),
        ("ROCE", _num(_roce, suffix="%", places=1)),
        ("Debt / Equity", _num(_de, places=2)),
        ("P/E (current)", _num(pe_current, suffix="x", places=1)),
        ("P/E 3-yr range", f"{_num(pe_low, suffix='x', places=1)} – {_num(pe_high, suffix='x', places=1)}"),
    ]
    has_ratios = any(v is not None for v in (_roe, _roce, _de, pe_current))

    with get_market_conn() as pconn:
        price_summary, price_series = _price_block(pconn, symbol, stock["market"])
        delivery = _delivery_block(pconn, symbol) if stock["market"] == "IN" else None

    # Key-stats strip: latest real value per headline metric. Revenue growth falls back to the
    # labelled QoQ figure only when no YoY exists at all — same rule as the matrix row.
    kpis = []
    if price_summary:
        r1 = price_summary["ret_1y"]
        kpis.append({
            "label": "Price", "value": f"{price_summary['currency']}{price_summary['last']:,.2f}",
            "tone": "", "sub": (f"{'+' if r1 >= 0 else ''}{r1}% 1Y · " if r1 is not None else "") + f"as of {price_summary['as_of']}",
            "sub_tone": ("pos" if r1 is not None and r1 >= 0 else "neg") if r1 is not None else "",
        })
    g_val = _last_non_null(growth)
    g_tag = "YoY"
    if g_val is None:
        g_val, g_tag = _last_non_null(growth_qoq), "QoQ"
    if g_val is not None:
        kpis.append({"label": "Revenue growth", "value": f"{g_val:+g}%", "tone": "pos" if g_val >= 0 else "neg", "sub": f"{g_tag} · latest quarter"})
    m_val = _last_non_null(margin)
    if m_val is not None:
        kpis.append({"label": "Operating margin", "value": f"{m_val:g}%", "tone": "", "sub": "latest quarter"})
    for label in ("ROCE", "ROE"):
        v = dict(ratio_stats).get(label)
        if v and v != "—":
            kpis.append({"label": label, "value": v, "tone": "", "sub": "latest reported"})
            break
    pe_v = dict(ratio_stats).get("P/E (current)")
    if pe_v and pe_v != "—":
        kpis.append({"label": "P/E", "value": pe_v, "tone": "", "sub": "3-yr " + dict(ratio_stats).get("P/E 3-yr range", "")})


    # ---- Raw-data blocks (see the helpers above) ----
    money_unit = "₹ Cr" if stock["market"] == "IN" else "$ M"
    eps_unit = "₹" if stock["market"] == "IN" else "$"
    quarter_all = [r for r in all_rows if not _is_annual(r)]
    annual_all = [r for r in all_rows if _is_annual(r)]
    from app.annual import annual_rows, merge_annual
    with get_market_conn() as _ac:  # IndianAPI annual statements win per fiscal year; older MoneyControl years fill what it lacks
        annual_all = merge_annual(annual_rows(_ac, stock["symbol"]), annual_all)
    q_window = quarter_all if current_range == "all" else quarter_all[-int(current_range):]
    from app.ai.checklist import is_bank_or_financial
    bank = is_bank_or_financial(stock["sector"])
    raw = {
        "money_unit": money_unit,
        "quarterly": _pl_table(q_window, quarter_all, money_unit, eps_unit, with_qoq=True, bank=bank) if q_window else None,
        "annual": _pl_table(annual_all[-12:], annual_all, money_unit, eps_unit, with_qoq=False, bank=bank) if annual_all else None,
        "balance": (_balance_table_half(all_rows, annual_all, money_unit, eps_unit, bank=bank, keep=1, filed_only=True) if stock["market"] == "IN"  # India files a balance sheet twice a year; US 10-Qs carry one every quarter
                    else _balance_table(all_rows[-14:], money_unit, eps_unit, bank=bank)),
        "annual_balance": _annual_balance_table(annual_all[-10:], money_unit) if stock["market"] == "IN" else None,
        "ownership": _ownership_table(q_window) if q_window else None,
    }

    # The explorer gets the whole stored history, not just the window the page tables show.
    from app.ia_panels import analyst_panel, company_panel  # IndianAPI consensus + company panels (None when not loaded)
    last_px = price_summary["last"] if price_summary else None
    ia_analyst = analyst_panel(stock["symbol"], last_px) if stock["market"] == "IN" else None
    ia_company = company_panel(stock["symbol"]) if stock["market"] == "IN" else None
    next_result = _next_result(stock["symbol"], ia_company) if stock["market"] == "IN" else None
    cf_labels = {"Operating cash flow", "Cash flow ÷ profit", "Investing cash flow", "Financing cash flow", "Free cash flow"}
    if raw["annual"] and stock["market"] == "IN":  # India reports cash flow yearly: its own table under Financial health
        cf_rows = [r for r in raw["annual"]["rows"] if r["label"] in cf_labels]
        raw["cashflow"] = {**{k: raw["annual"][k] for k in ("columns", "fulls", "ends")}, "rows": cf_rows} if cf_rows else None
        if cf_rows:
            raw["annual"] = {**raw["annual"], "rows": [r for r in raw["annual"]["rows"] if r["label"] not in cf_labels]}
    else:
        raw["cashflow"] = None
    verdict = _verdict(request, stock, quarter_all, annual_all)
    pe_stats = [(k, v) for k, v in ratio_stats if k.startswith("P/E") and v not in ("—", "— – —")]
    has_lens = bool(matrix.get("has_layer2") or any(matrix.get("guidance") or []) or any(matrix.get("sector_momentum") or []) or matrix.get("has_layer5"))
    has_own = bool(raw.get("ownership") or matrix.get("institutional_holders") is not None)
    view_groups = [g for g in (
        ("perf", "Performance", "chart-bar", bool(raw["quarterly"] or raw["annual"])),
        ("health", "Financial health", "heartbeat", bool(raw["cashflow"] or raw["annual_balance"] or raw["balance"])),
        ("val", "Valuation", "target", bool(ia_analyst or pe_stats or (ia_company and ia_company["peers"]))),
        ("own", "Ownership", "users", has_own),
        ("co", "Company", "building", bool(ia_company) or stock["market"] == "IN"),
        ("lens", "Quality lens", "microscope", has_lens),
    ) if g[3]]
    perf_panes = [k for k, ok in (("quarterly", raw["quarterly"]), ("annual", raw["annual"])) if ok]
    full_q = _pl_table(quarter_all, quarter_all, money_unit, eps_unit, with_qoq=True, bank=bank) if quarter_all else None
    full_a = _pl_table(annual_all, annual_all, money_unit, eps_unit, with_qoq=False, bank=bank) if annual_all else None

    def _explorer_set(tbl, title, xtitle):
        return None if not tbl else {
            "title": title, "xtitle": xtitle, "columns": tbl["columns"], "ends": tbl["ends"],
            "rows": [{"label": r["label"], "unit": r["unit"], "values": r["values"], "note": r["note"]} for r in tbl["rows"]],
        }

    explorer = {
        "symbol": stock["symbol"], "name": stock["name"], "currency": eps_unit,
        "price": {"currency": price_summary["currency"], "as_of": price_summary["as_of"]} if price_summary else None,
        "sets": {k: v for k, v in {
            "quarterly": _explorer_set(full_q, "Quarterly results", "Quarter"),
            "annual": _explorer_set(full_a, "Annual results", "Fiscal year"),
            "balance": _explorer_set((_balance_table_half(all_rows, annual_all, money_unit, eps_unit, bank=bank, keep=40) if stock["market"] == "IN" else _balance_table(all_rows, money_unit, eps_unit, bank=bank)), "Balance sheet & ratios", "Half-year end" if stock["market"] == "IN" else "Period"),
            "ownership": _explorer_set(_ownership_table(quarter_all) if quarter_all else None, "Ownership", "Quarter"),
        }.items() if v},
    }

    return templates.TemplateResponse(
        "analytics.html",
        {
            "request": request,
            "active_nav": "analytics",
            "default_symbol": symbol,
            "stock": stock,
            "quarters": quarters,
            "quarter_full_labels": quarter_full_labels,
            "range_options": _RANGE_OPTIONS,
            "current_range": current_range,
            "total_quarters": len(all_rows),
            "matrix": matrix,
            "trend_data": trend_data,
            "chips": chips,
            "pnl_rows": pnl_rows,
            "pnl_trend": pnl_trend,
            "bs_quarters": bs_quarters,
            "bs_rows": bs_rows,
            "bs_trend": bs_trend,
            "has_balance_sheet": has_balance_sheet,
            "ratio_stats": ratio_stats,
            "has_ratios": has_ratios,
            "kpis": kpis,
            "raw": raw,
            "ia_analyst": ia_analyst,
            "verdict": verdict, "pe_stats": pe_stats, "has_lens": has_lens, "has_own": has_own,
            "view_groups": [{"key": k, "label": l, "icon": i} for k, l, i, _ in view_groups],
            "perf_panes": perf_panes,
            "ia_company": ia_company, "next_result": next_result, "delivery": delivery,
            "explorer": explorer,
            "n_quarters": len([r for r in all_rows if not _is_annual(r)]),
            "n_annual": len(annual_all),
            "price_summary": price_summary,
            "price_series": price_series,
        },
    )


@router.get("/export/stock/{symbol:path}.csv")
def export_stock_csv(symbol: str):
    """Every stored quarterly_data column for one stock, newest first — the raw data behind the page."""
    import csv
    import io

    from fastapi.responses import Response

    with get_market_conn() as conn:
        rows = conn.execute("SELECT * FROM quarterly_data WHERE symbol = ? ORDER BY period_end_date DESC", (symbol,)).fetchall()
    if not rows:
        return HTMLResponse("Unknown symbol", status_code=404)
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = [k for k in rows[0].keys() if k not in ("id", "quarter_index")]
    w.writerow(cols)
    masked = {"bse": "exchange filing", "nse": "exchange filing", "moneycontrol": "annual history", "indianapi": "normalised"}
    for r in rows:
        w.writerow([masked.get(r[k], r[k]) if k == "source" else r[k] for k in cols])
    safe = symbol.replace(":", "_")
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{safe}.csv"'})
