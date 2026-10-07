"""Real Finnhub ingestion — "guidance vs. delivered" (beat/met/miss) for US stocks,
plus a small, explicitly-verified set of India large-caps that also carry an active
US ADR listing.

Free-tier API key, self-serve signup at finnhub.io, no KYC. Free tier gives 4 quarters
of pre-computed EPS surprise (actual vs. consensus estimate) per symbol — raw
estimates themselves are paid-only, but we don't need those, just the verdict.

This is an enrichment source, not a primary one: it only UPDATEs the
guidance_vs_delivered column on quarterly_data rows that SEC EDGAR/BSE already wrote
(matched by nearest period_end_date within a tolerance, since Finnhub's own period-end
dates don't always land on the exact same day as the primary source's). It never
INSERTs a row itself.

Root-caused this session: two separate full runs (hours each) wrote almost identical
row counts (10,222 then 10,238) — no resume/skip-already-covered logic meant every
re-trigger started over from the same unordered `SELECT DISTINCT symbol` query and
re-covered roughly the same ~2,334 symbols, never advancing into the other ~2,755.
Verified live this wasn't a data-availability or rate-limit issue — sampled "missing"
symbols (including AbbVie, a massive well-covered stock) all returned real data
instantly when queried directly. Fixed with the same skip-already-covered pattern
already used in tiingo_source.py/bse_source.py/moneycontrol_source.py.

India coverage, via US ADR ticker: querying Finnhub with an NSE-suffixed symbol
directly (e.g. "RELIANCE.NS") returns 403 on the free tier — no access. But querying
via the SAME company's US ADR ticker works and returns data labeled with the real NSE
symbol (e.g. querying "WIT" returns rows tagged "symbol": "WIPRO.NS"). This only covers
India companies with a still-active US ADR — confirmed live one at a time below; do NOT
add an entry without verifying it returns real data the same way (Tata Motors' old ADR
"TTM" was delisted in 2024 and returns an empty list, for example)."""

import os
import time
from datetime import date, timedelta

import requests

from app.ingestion.base import DataSource, NotConfiguredError, apply_symbol_filter

API_KEY = os.environ.get("FINNHUB_API_KEY")
BASE_URL = "https://finnhub.io/api/v1/stock/earnings"
RATE_LIMIT_SECONDS = 1.1  # free tier: 60 calls/minute
DATE_TOLERANCE_DAYS = 15

# NSE symbol -> US ADR ticker to query Finnhub with. Each entry verified live (real,
# non-empty response) before being added — see module docstring. Small and manual on
# purpose: guessing a ticker wrong here would silently attach one company's earnings
# verdict to a different company's rows.
INDIA_ADR_TICKERS = {
    "NSE:INFY": "INFY",
    "NSE:WIPRO": "WIT",
    "NSE:HDFCBANK": "HDB",
    "NSE:ICICIBANK": "IBN",
}


class FinnhubSource(DataSource):
    name = "finnhub"

    def _require_configured(self):
        if not API_KEY:
            raise NotConfiguredError("FINNHUB_API_KEY not set — sign up free at finnhub.io, no KYC needed.")

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("Finnhub is used here for earnings surprises only — prices come from Tiingo.")

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        self._require_configured()
        us_symbols = [r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM quarterly_data WHERE symbol IN "
            "(SELECT symbol FROM stocks WHERE market = 'US')"
        ).fetchall()]
        # {internal symbol: ticker Finnhub is actually queried with} — identity for US,
        # the ADR alias for the handful of India symbols above.
        query_ticker = {s: s.split(":")[1] for s in us_symbols}
        india_covered = [r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM quarterly_data WHERE symbol IN "
            "(SELECT symbol FROM stocks WHERE market = 'IN')"
        ).fetchall()]
        for sym in india_covered:
            if sym in INDIA_ADR_TICKERS:
                query_ticker[sym] = INDIA_ADR_TICKERS[sym]

        if not symbol:
            # Resume, don't restart: skip symbols that already have a guidance verdict
            # on at least one row. Finnhub's free tier always returns the same "last 4
            # quarters" snapshot, so re-querying an already-covered symbol wouldn't add
            # meaningfully new data anyway — same principle as the other sources' fixes.
            # An explicit `symbol` re-ingestion always runs regardless.
            already_covered = {r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM quarterly_data WHERE guidance_vs_delivered IS NOT NULL"
            ).fetchall()}
            query_ticker = {s: t for s, t in query_ticker.items() if s not in already_covered}

        symbols = apply_symbol_filter(list(query_ticker.keys()), limit, symbol)
        rows_written = 0

        for sym in symbols:
            time.sleep(RATE_LIMIT_SECONDS)
            ticker = query_ticker[sym]
            try:
                resp = requests.get(
                    BASE_URL, params={"symbol": ticker, "token": API_KEY}, timeout=20,
                )
                resp.raise_for_status()
                surprises = resp.json()
            except requests.RequestException:
                continue
            if not surprises:
                continue

            existing_periods = [r["period_end_date"] for r in conn.execute(
                "SELECT period_end_date FROM quarterly_data WHERE symbol = ?", (sym,)
            ).fetchall()]

            for item in surprises:
                actual, estimate = item.get("actual"), item.get("estimate")
                if actual is None or estimate is None:
                    continue
                verdict = "beat" if actual > estimate else "miss" if actual < estimate else "met"
                fh_date = date.fromisoformat(item["period"])
                nearest = min(
                    existing_periods,
                    key=lambda p: abs((date.fromisoformat(p) - fh_date).days),
                    default=None,
                )
                if nearest is None or abs((date.fromisoformat(nearest) - fh_date).days) > DATE_TOLERANCE_DAYS:
                    continue
                conn.execute(
                    "UPDATE quarterly_data SET guidance_vs_delivered = ? WHERE symbol = ? AND period_end_date = ?",
                    (verdict, sym, nearest),
                )
                rows_written += 1
            conn.commit()
            if progress:
                progress(rows_written)

        return rows_written
