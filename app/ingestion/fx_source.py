"""USD/INR exchange rate, refreshed hourly by the in-app scheduler (job fx_usd_inr; Admin > Scheduled jobs).

Sources, tried in order until one answers with a sane number:
  1. Frankfurter (frankfurter.dev) - European Central Bank reference rates; free, no key, one fixing per working day.
  2. open.er-api.com - free daily-updated rates.
  3. Yahoo Finance (INR=X via yfinance) - intraday, but unofficial and personal-use only, so last.
A quote is rejected if it is outside 50-200 INR/USD or differs by more than 8% from the last stored rate (a bad feed must not move
every US holding in a portfolio by that much); the previous rate then stays in force. Each fetch is stored, so the history is kept."""
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from app.ingestion.base import DataSource

PAIR = "USDINR"
MAX_JUMP = 0.08


def _frankfurter():
    j = requests.get("https://api.frankfurter.dev/v1/latest", params={"base": "USD", "symbols": "INR"}, timeout=12).json()
    return float(j["rates"]["INR"]), j.get("date")


def _er_api():
    j = requests.get("https://open.er-api.com/v6/latest/USD", timeout=12).json()
    return float(j["rates"]["INR"]), j.get("time_last_update_utc")


def _yahoo():
    import yfinance as yf
    h = yf.Ticker("INR=X").history(period="1d", interval="1h")
    if h is None or h.empty:
        raise ValueError("no Yahoo data")
    return float(h["Close"].iloc[-1]), str(h.index[-1])


SOURCES = [("frankfurter", _frankfurter), ("open.er-api.com", _er_api), ("yfinance", _yahoo)]


class FxSource(DataSource):
    name = "fx_usd_inr"

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("fx_usd_inr only provides the exchange rate.")

    def ingest_prices(self, conn, limit: Optional[int] = None, symbol: Optional[str] = None, progress: Optional[Callable[[int], None]] = None) -> int:
        last = conn.execute("SELECT rate FROM fx_rates WHERE pair = ? ORDER BY fetched_at DESC LIMIT 1", (PAIR,)).fetchone()
        errors = []
        for name, fn in SOURCES:
            try:
                rate, as_of = fn()
            except Exception as e:  # noqa: BLE001 - any failure just moves to the next source
                errors.append(f"{name}: {type(e).__name__}")
                continue
            if not (50 <= rate <= 200):
                errors.append(f"{name}: {rate:g} out of range")
                continue
            if last and abs(rate / last["rate"] - 1) > MAX_JUMP:
                errors.append(f"{name}: {rate:g} is >{MAX_JUMP:.0%} from the stored {last['rate']:g}")
                continue
            conn.execute("INSERT OR REPLACE INTO fx_rates (pair, rate, source, as_of, fetched_at) VALUES (?,?,?,?,?)",
                         (PAIR, round(rate, 4), name, as_of, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            from app import fx
            fx.reset_cache()
            return 1
        raise RuntimeError("No usable USD/INR quote - " + "; ".join(errors))
