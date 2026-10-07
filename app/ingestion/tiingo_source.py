"""Real Tiingo ingestion — US daily OHLC price history. Free-tier API key, self-serve
signup at tiingo.com, no KYC. Covers US stocks only.

Root-caused this session, twice. First: a full-universe run (10,438 symbols, ~3hrs)
completed with status='success' but only netted ~300 distinct symbols — a bare
`except requests.RequestException: continue` swallowed 429s exactly like any other
failure, so the job kept "succeeding" while quietly dropping most of the universe.

Second, after adding naive retry-with-backoff for that: confirmed directly against the
user's real Tiingo account dashboard that the free tier is a genuinely tiny **50
requests/hour** (1,000/day). A run that hits that wall still had ~9,900 remaining
symbols to attempt — the old retry logic burned up to 4 requests per throttled symbol
(1 + 3 retries) trying to wait out an HOURLY cap with a 90-SECOND backoff, achieving
nothing, and even without retries, blindly continuing to the next symbol on every 429
would burn through the scarce DAILY budget in minutes (thousands of guaranteed-429
requests at 0.1s apart) for zero benefit, since none of them can succeed until the
hourly window resets regardless of how many are attempted.

Fix: no retries, and a 429 stops the WHOLE run immediately (not just that symbol) —
preserves whatever's left of the daily budget for a later trigger instead of draining
it chasing a wall that won't move. Combined with the resume logic below (skip symbols
already covered), this is the only sane shape for a 50/hour cap: a little more
coverage every time it's triggered, spread across many runs over many hours/days."""

import os
import time

import requests

from app.ingestion.base import DataSource, NotConfiguredError, apply_symbol_filter

API_KEY = os.environ.get("TIINGO_API_KEY")
BASE_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
RATE_LIMIT_SECONDS = 0.1

_RATE_LIMITED = object()  # sentinel: distinguishes "stop the whole run" from "skip this one symbol"


class TiingoSource(DataSource):
    name = "tiingo"

    def _require_configured(self):
        if not API_KEY:
            raise NotConfiguredError("TIINGO_API_KEY not set — sign up free at tiingo.com, no KYC needed.")

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("Tiingo is a price source — fundamentals come from SEC EDGAR.")

    def _fetch(self, ticker: str, start: str):
        """One symbol's full history. Returns the parsed body, `_RATE_LIMITED` if
        Tiingo's hourly/daily cap has been hit (caller must stop the whole run, not
        just this symbol — see module docstring), or None for any other failure
        (this one symbol should just be skipped)."""
        try:
            resp = requests.get(
                BASE_URL.format(ticker=ticker),
                params={"startDate": start, "token": API_KEY, "format": "json", "resampleFreq": "daily"},
                headers={"Content-Type": "application/json"},
                timeout=20,
            )
        except requests.RequestException:
            return None
        if resp.status_code == 429:
            return _RATE_LIMITED
        try:
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            return None

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        self._require_configured()
        symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM stocks WHERE market = 'US'").fetchall()]
        symbols = apply_symbol_filter(symbols, limit, symbol)

        if not symbol:
            # Resume, don't restart: skip symbols this source already covers, so a
            # re-triggered run spends its (tiny) rate-limit budget on what's still
            # missing instead of re-fetching the same early alphabetical slice every
            # time. An explicit `symbol` re-ingestion always runs regardless.
            already_covered = {r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM price_history WHERE source = 'tiingo'"
            ).fetchall()}
            symbols = [s for s in symbols if s not in already_covered]

        start = "1990-01-01"  # full history — Tiingo clamps to whatever's actually available per ticker
        rows_written = 0

        for sym in symbols:
            time.sleep(RATE_LIMIT_SECONDS)
            ticker = sym.split(":")[1]
            body = self._fetch(ticker, start)
            if body is _RATE_LIMITED:
                break  # stop the whole run — every remaining symbol would 429 too; preserve the daily budget
            # Tiingo returns a dict (e.g. {"detail": "Error: ..."}) instead of a list
            # for invalid/delisted tickers — truthy, so `if not body` doesn't catch it.
            if not body or not isinstance(body, list):
                continue
            for row in body:
                conn.execute(
                    "INSERT OR REPLACE INTO price_history (symbol, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,'tiingo')",
                    (sym, row["date"][:10], row.get("open"), row.get("high"), row.get("low"), row.get("close"), row.get("volume")),
                )
                rows_written += 1
            conn.execute("DELETE FROM price_history WHERE symbol = ? AND source = 'mock'", (sym,))
            conn.commit()
            if progress:
                progress(rows_written)

        return rows_written
