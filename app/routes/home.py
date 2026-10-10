"""Public homepage at "/".

Everything numeric on it is real: coverage counts and the sample stock cards come from market.db (public
market data, nothing user-specific), cached for a few minutes so the page never costs a scan.
Anything that is only an illustration of a feature is labelled "Example" in the template."""

import os
import threading
import time
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db import get_market_conn
from app.routes.analytics import _is_annual, _yoy
from app.templates_config import templates

router = APIRouter()

# First symbol with enough data wins, per market.
_CANDIDATES = {"IN": ["NSE:RELIANCE", "NSE:TCS", "NSE:HDFCBANK", "NSE:INFY"], "US": ["NASDAQ:AAPL", "NASDAQ:MSFT", "NASDAQ:NVDA"]}
_TTL = 600
_cache: dict = {"at": 0.0, "value": None}
_lock = threading.Lock()


def _indian_group(n: int) -> str:
    s = f"{abs(n)}"
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return ("-" if n < 0 else "") + s


def _compact(v: float, market: str) -> str:
    """Bar labels: 3.12L Cr style is unfamiliar abroad, so keep plain grouped integers with local grouping."""
    n = round(v)
    return _indian_group(n) if market == "IN" else f"{n:,}"


def _price_text(v: float, market: str) -> str:
    whole, frac = f"{v:.2f}".split(".")
    return f"₹{_indian_group(int(whole))}.{frac}" if market == "IN" else f"${int(whole):,}.{frac}"


def _line_paths(values, w, h, pad=6):
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    n = len(values) - 1 or 1
    pts = [(pad + (w - 2 * pad) * i / n, pad + (h - 2 * pad) * (1 - (v - lo) / span)) for i, v in enumerate(values)]
    line = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    area = f"{line} L{pts[-1][0]:.1f} {h} L{pts[0][0]:.1f} {h} Z"
    return {"line": line, "area": area, "end": (round(pts[-1][0], 1), round(pts[-1][1], 1))}


def _featured(conn, symbol: str):
    stock = conn.execute("SELECT symbol, name, market FROM stocks WHERE symbol = ?", (symbol,)).fetchone()
    if not stock:
        return None
    prices = conn.execute(
        "SELECT date, close FROM price_history WHERE symbol = ? ORDER BY date DESC LIMIT 252", (symbol,)
    ).fetchall()[::-1]
    rows = [r for r in conn.execute("SELECT * FROM quarterly_data WHERE symbol = ? ORDER BY period_end_date", (symbol,)).fetchall()]
    quarters = [r for r in rows if not _is_annual(r)]
    if len(prices) < 60 or len(quarters) < 6 or sum(1 for r in quarters[-8:] if r["revenue_cr"]) < 5:
        return None

    cur = "₹" if stock["market"] == "IN" else "$"
    unit = "₹ Cr" if stock["market"] == "IN" else "$ M"
    closes = [p["close"] for p in prices]
    ret = (closes[-1] / closes[0] - 1) * 100
    price = _line_paths(closes, 560, 150)

    last8 = quarters[-8:]
    by_end = {r["period_end_date"]: r for r in quarters}
    revs = [r["revenue_cr"] for r in last8]
    top = max(v for v in revs if v) or 1
    bars = []
    for r in last8:
        d = date.fromisoformat(r["period_end_date"])
        v = r["revenue_cr"]
        bars.append({
            "label": f"Q{(d.month - 1) // 3 + 1} {str(d.year)[2:]}",
            "h": round(v / top * 100, 1) if v else 0,
            "text": _compact(v, stock["market"]) if v else "—",
            "last": r is last8[-1],
        })
    latest = last8[-1]
    yoy = _yoy(latest, by_end)
    chips = []
    if yoy is not None:
        chips.append({"label": "Revenue growth", "value": f"{yoy:+.1f}%", "sub": "vs same quarter last year", "tone": "pos" if yoy >= 0 else "neg"})
    if latest["opm_pct"] is not None:
        chips.append({"label": "Operating margin", "value": f"{latest['opm_pct']:.1f}%", "sub": "latest quarter", "tone": ""})
    if latest["roce_pct"] is not None:
        chips.append({"label": "ROCE", "value": f"{latest['roce_pct']:.1f}%", "sub": "latest reported", "tone": ""})
    elif latest["roe_pct"] is not None:
        chips.append({"label": "ROE", "value": f"{latest['roe_pct']:.1f}%", "sub": "latest reported", "tone": ""})

    # Mini matrix: three rows x last four quarters, each with the sparkline of all eight.
    def row(label, key, fmt):
        vals = [r[key] for r in last8]
        real = [v for v in vals if v is not None]
        if len(real) < 5:
            return None
        sp = _line_paths([v if v is not None else real[0] for v in vals], 84, 22, pad=2)
        return {"label": label, "cells": [fmt(r[key]) if r[key] is not None else "—" for r in last8[-4:]], "spark": sp, "up": real[-1] >= real[0]}

    matrix = [m for m in (
        row("Revenue", "revenue_cr", lambda v: _compact(v, stock["market"])),
        row("Operating margin", "opm_pct", lambda v: f"{v:.1f}%"),
        row("Net profit", "net_profit_cr", lambda v: _compact(v, stock["market"])),
    ) if m]
    heads = []
    for r in last8[-4:]:
        d = date.fromisoformat(r["period_end_date"])
        heads.append(f"Q{(d.month - 1) // 3 + 1} {str(d.year)[2:]}")

    return {
        "symbol": stock["symbol"], "name": stock["name"], "market": stock["market"], "cur": cur, "unit": unit,
        "last": _price_text(closes[-1], stock["market"]),
        "ret": f"{ret:+.1f}%", "ret_tone": "pos" if ret >= 0 else "neg", "as_of": prices[-1]["date"],
        "price": price, "bars": bars, "chips": chips, "matrix": matrix, "heads": heads, "tone_pos": ret >= 0,
        "first_date": prices[0]["date"],
    }


def _build():
    with get_market_conn() as conn:
        covered = conn.execute("SELECT COUNT(DISTINCT symbol) c FROM quarterly_data").fetchone()["c"]
        periods = conn.execute("SELECT COUNT(*) c FROM quarterly_data").fetchone()["c"]
        by_market = {r["market"]: r["c"] for r in conn.execute(
            "SELECT s.market, COUNT(*) c FROM stocks s WHERE EXISTS (SELECT 1 FROM quarterly_data q WHERE q.symbol = s.symbol) GROUP BY s.market"
        )}
        featured = {}
        for market, symbols in _CANDIDATES.items():
            for sym in symbols:
                f = _featured(conn, sym)
                if f:
                    featured[market] = f
                    break
    return {"covered": covered, "periods": periods, "by_market": by_market, "featured": featured}


def _data():
    with _lock:
        if _cache["value"] is None or time.time() - _cache["at"] > _TTL:
            try:
                _cache["value"] = _build()
            except Exception:  # noqa: BLE001 — the homepage must render even if the sample cards can't
                _cache["value"] = _cache["value"] or {"covered": 0, "periods": 0, "by_market": {}, "featured": {}}
            _cache["at"] = time.time()
        return _cache["value"]


def _covered_text() -> str:
    """'11,000+' style count of stocks we hold reported data for (rounded down, so it is never an overstatement)."""
    try:
        with get_market_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM stock_metrics").fetchone()[0] or conn.execute("SELECT COUNT(DISTINCT symbol) FROM quarterly_data").fetchone()[0]
    except Exception:  # noqa: BLE001
        n = 0
    return f"{n // 1000 * 1000:,}+" if n >= 2000 else "Thousands of"


@router.get("/", response_class=HTMLResponse)
def home(request: Request, v: str | None = None):
    """The public home page. `?v=classic` (or HOME_VARIANT=classic in the environment) serves the previous home page, kept as a backup."""
    d = _data()
    variant = (v or os.environ.get("HOME_VARIANT", "")).lower()
    if variant == "classic":
        return templates.TemplateResponse("home_classic.html", {"request": request, "active_nav": "", **d})
    from app.routes.google_auth import google_enabled
    return templates.TemplateResponse("home.html", {"request": request, "cta_href": "/auth/google" if google_enabled() else "/login",
                                                    "covered_text": _covered_text(), "active_nav": ""})
