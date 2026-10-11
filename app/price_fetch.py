"""On-demand prices: when a stock has no (or stale) price history, fetch it from the internet right now instead of
waiting for a nightly job. Used when a position is proposed/confirmed, and by the chat's price tool.

Source order: Yahoo Finance via yfinance (NSE `.NS`, then BSE `.BO` for India; the plain ticker for US) and, for US
tickers, Tiingo when a key is set. Rows are written with INSERT OR IGNORE and a `source` tag, so a price a scheduled job
already stored is never overwritten. Every call is time-boxed — a slow provider must never hang a chat request."""

import logging
import os
import time
from datetime import date, datetime, timedelta

import requests

from app.db import get_market_conn

logger = logging.getLogger("app.price_fetch")

STALE_AFTER_DAYS = 6          # a price older than this (weekend + holidays) is refreshed
FETCH_BUDGET_SECONDS = 25.0   # total wall-clock a single caller may spend fetching


def _yahoo_tickers(symbol: str) -> list[str]:
    ticker = symbol.split(":", 1)[-1]
    return [f"{ticker}.NS", f"{ticker}.BO"] if symbol.startswith("NSE:") else [ticker]


def _store(conn, symbol: str, rows, source: str, replace: bool = False) -> int:
    n = 0
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    for d, o, h, l, c, v in rows:
        if c is None or c != c:  # NaN
            continue
        n += conn.execute(
            f"{verb} INTO price_history (symbol, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            (symbol, d, o, h, l, c, v, source),
        ).rowcount
    conn.commit()
    return n


def _from_yahoo(symbol: str):
    import yfinance as yf

    for tk in _yahoo_tickers(symbol):
        try:
            df = yf.Ticker(tk).history(period="max", timeout=12, auto_adjust=False)  # raw close, split-adjusted only: matches the exchange prints and the daily top-up
        except Exception as e:  # noqa: BLE001 — yfinance fails with assorted internal types for unknown tickers
            logger.info("yahoo %s failed: %s", tk, e)
            continue
        if df is not None and not df.empty:
            return [(i.date().isoformat(), r.get("Open"), r.get("High"), r.get("Low"), r.get("Close"), r.get("Volume"))
                    for i, r in df.iterrows()]
    return None


def _from_tiingo(symbol: str):
    key = os.environ.get("TIINGO_API_KEY")
    if not key or symbol.startswith("NSE:"):
        return None
    try:
        r = requests.get(f"https://api.tiingo.com/tiingo/daily/{symbol.split(':', 1)[-1]}/prices",
                         params={"startDate": "1990-01-01", "token": key, "format": "json"}, timeout=15)
        body = r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(body, list) or not body:
        return None
    return [(x["date"][:10], x.get("open"), x.get("high"), x.get("low"), x.get("close"), x.get("volume")) for x in body]


def ensure_prices(symbol: str, deadline: float | None = None) -> dict:
    """Make sure `symbol` has recent price history. Returns {"ok": bool, "fetched": rows_added, "source": ..., "last": date}.
    No-ops (and costs nothing) when the stored history is fresh. `deadline` is a time.monotonic() value shared by a caller
    that is fetching several symbols, so the whole batch stays inside its budget."""
    with get_market_conn() as conn:
        row = conn.execute("SELECT MAX(date) AS d FROM price_history WHERE symbol = ?", (symbol,)).fetchone()
        last = row["d"]
        if last and (date.today() - date.fromisoformat(last)).days <= STALE_AFTER_DAYS:
            return {"ok": True, "fetched": 0, "source": "stored", "last": last}
        if deadline is not None and time.monotonic() > deadline:
            return {"ok": bool(last), "fetched": 0, "source": "skipped (time budget)", "last": last}
        for name, fn in (("yfinance", _from_yahoo), ("tiingo", _from_tiingo)):
            rows = fn(symbol)
            if rows:
                added = _store(conn, symbol, rows, name)
                newest = conn.execute("SELECT MAX(date) AS d FROM price_history WHERE symbol = ?", (symbol,)).fetchone()["d"]
                return {"ok": True, "fetched": added, "source": name, "last": newest}
    return {"ok": bool(last), "fetched": 0, "source": "none found", "last": last}


def close_near(conn, symbol: str, on: str, max_gap_days: int = 7):
    """Closing price on `on` (or the latest session before it, within max_gap_days). None if the history doesn't reach."""
    row = conn.execute("SELECT date, close FROM price_history WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1", (symbol, on)).fetchone()
    if not row or not row["close"]:
        return None
    if (date.fromisoformat(on) - date.fromisoformat(row["date"])).days > max_gap_days:
        return None
    return row["close"]


def refresh_history(conn, symbol: str) -> int:
    """Re-pull a symbol's FULL history from Yahoo and overwrite what is stored. For a stock that has just split or issued
    bonus shares: Yahoo restates every old price to the new basis, and our stored (older) rows would otherwise show a cliff."""
    rows = _from_yahoo(symbol)
    return _store(conn, symbol, rows, "yfinance", replace=True) if rows else 0
