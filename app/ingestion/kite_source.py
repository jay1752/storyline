"""Real Kite Connect ingestion — India price/OHLC, fallback behind 5paisa.

Verified against kite.trade/docs/connect/v3 on 2026-09-19.

Kite's access_token is session-based and expires daily — it can't be obtained inside
a stateless ingestion run because getting the first request_token requires an
interactive browser login. Run `python -m scripts.kite_login` once (each trading day,
if you use this) to mint KITE_ACCESS_TOKEN, then set it in .env.

Full-universe note: the historical/day endpoint caps at 1 year per call, so full
history means looping year by year per symbol — expensive. `limit` caps how many
symbols a run touches; there's no year-count cap here, so a full-history run against
many symbols is genuinely long — test with `limit` first.
"""

import csv
import io
import os
import time
from datetime import datetime, timedelta

import requests

from app.ingestion.base import DataSource, NotConfiguredError, apply_symbol_filter

API_KEY = os.environ.get("KITE_API_KEY")
ACCESS_TOKEN = os.environ.get("KITE_ACCESS_TOKEN")
BASE_URL = "https://api.kite.trade"
RATE_LIMIT_SECONDS = 0.35  # Kite's documented limit is ~3 req/sec for historical data
YEARS_BACK = 10


class KiteSource(DataSource):
    name = "kite"

    def _headers(self):
        if not API_KEY or not ACCESS_TOKEN:
            raise NotConfiguredError(
                "KITE_API_KEY / KITE_ACCESS_TOKEN not set. Access tokens expire daily — "
                "run `python -m scripts.kite_login` to mint one (needs an interactive browser login step, "
                "can't be automated inside an ingestion run)."
            )
        return {"Authorization": f"token {API_KEY}:{ACCESS_TOKEN}", "X-Kite-Version": "3"}

    def _instrument_token_map(self) -> dict[str, str]:
        resp = requests.get(f"{BASE_URL}/instruments", headers=self._headers(), timeout=30)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        return {row["tradingsymbol"]: row["instrument_token"] for row in reader if row.get("exchange") == "NSE"}

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("Kite doesn't provide fundamentals — quarterly data needs NSE/BSE filings, separately.")

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        self._headers()
        symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM stocks WHERE market = 'IN'").fetchall()]
        symbols = apply_symbol_filter(symbols, limit, symbol)
        token_map = self._instrument_token_map()
        rows_written = 0
        now = datetime.now()

        for sym in symbols:
            ticker = sym.split(":")[1]
            token = token_map.get(ticker)
            if not token:
                continue

            symbol_rows = 0
            for y in range(YEARS_BACK):
                to_dt = now - timedelta(days=365 * y)
                from_dt = to_dt - timedelta(days=365)
                time.sleep(RATE_LIMIT_SECONDS)
                try:
                    resp = requests.get(
                        f"{BASE_URL}/instruments/historical/{token}/day",
                        params={"from": from_dt.strftime("%Y-%m-%d %H:%M:%S"), "to": to_dt.strftime("%Y-%m-%d %H:%M:%S")},
                        headers=self._headers(),
                        timeout=30,
                    )
                    resp.raise_for_status()
                    candles = resp.json().get("data", {}).get("candles", [])
                except requests.RequestException:
                    continue
                if not candles:
                    break  # no more data further back — stop walking this symbol
                for candle in candles:
                    ts, o, h, l, c, v = candle[:6]
                    conn.execute(
                        "INSERT OR REPLACE INTO price_history (symbol, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,'kite')",
                        (sym, ts[:10], o, h, l, c, v),
                    )
                    rows_written += 1
                    symbol_rows += 1

            if symbol_rows:
                conn.execute("DELETE FROM price_history WHERE symbol = ? AND source = 'mock'", (sym,))
            conn.commit()
            if progress:
                progress(rows_written)

        return rows_written
