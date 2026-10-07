"""NSE's own daily bhavcopy (end-of-day price) archive — found this session to attack
the India price gap yfinance can't close: ~2,000 India symbols (mostly delisted or
renamed micro-caps) that yfinance's live quote lookup returns genuinely empty for.
Live-tested before building: nsearchives.nseindia.com serves one free, no-key ZIP per
trading day, confirmed back to 1999 (23,693 bytes, real OHLC rows) through to today.

Fundamentally different shape from every other price source here: one file covers
EVERY symbol that traded that day (thousands of rows), not one fetch per symbol. A
delisted/renamed ticker that yfinance can't resolve by its current name is still in
here under whatever code it traded under on its last live day — bhavcopy just
records the tape, it doesn't care what happened to the company afterward. This also
means the request budget is tiny compared to a per-symbol source: one request per
CALENDAR day walked, not one per symbol.

Two URL/column schemas, confirmed live by fetching both: NSE switched to a new
"UDiFF" format at some point in H2 2024 (both still answered 200 on 2024-07-01;
legacy 404'd by 2025-01-01). Rather than pin the exact cutover date, every day tries
the UDiFF URL first and falls back to the legacy one — robust across the boundary
without needing to know exactly where it sits, and read-only web code with no DB
dependency so a 404 is always a plain two-sub-second fallback.

Scope deliberately narrow: only writes for symbols this DB has ZERO price_history
rows for (the actual gap), not a full re-pull of NSE's whole history for every
symbol — yfinance/5paisa already cover the rest and there's no reason to re-fetch
27 years of daily bars for a symbol that's already covered. Walks backward from
today one calendar day at a time (weekends/holidays are just 404s, no India holiday
calendar needed), stopping early once every still-needed symbol has been found at
least once or the run's time budget runs out — resumable the same way as every
other source here: next trigger picks up from the earliest date already reached via
this source and keeps walking further back, so a symbol delisted a decade ago still
gets found eventually across repeated runs."""

import csv
import io
import time
import zipfile
from datetime import date, timedelta

import requests

from app.ingestion.base import DataSource

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
}

UDIFF_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
LEGACY_URL = "https://nsearchives.nseindia.com/content/historical/EQUITIES/{yyyy}/{mon}/cm{ddmonyyyy}bhav.csv.zip"

RATE_LIMIT_SECONDS = 0.3  # undocumented, no published limit — same conservative default as every other source here
MAX_RUN_SECONDS = 2700  # 45 min, same self-terminating convention as yfinance_source.py
EARLIEST_DATE = date(1999, 1, 1)  # oldest day confirmed live to still return real data

# Real gap found live: EQ-only excluded a large share of real equity rows — a single
# recent day's file had 2,662 EQ rows but ALSO 246 BE + 26 BZ (trade-for-trade/under-
# surveillance equity, same real shares, just restricted settlement) + 389 SM + 87 ST
# (SME-platform equity). After walking this source's ENTIRE available 1999-2026 range
# with EQ-only, 2,011 of 2,074 target symbols were never found at all — strong signal
# many of them simply never traded under "EQ". GS/GB/TB/N0-N6 (bonds, gilts, T-bills)
# deliberately excluded — those are genuinely not equity.
EQUITY_SERIES = {"EQ", "BE", "BZ", "BL", "SM", "ST"}


def _fetch_day(d: date) -> tuple[str, list[dict]] | None:
    """Returns (schema, rows) for one trading day, or None on a holiday/weekend (both
    URL attempts 404). Tries UDiFF first since it's the current format; legacy is the
    fallback for anything before the H2-2024 cutover."""
    ymd = d.strftime("%Y%m%d")
    resp = requests.get(UDIFF_URL.format(ymd=ymd), headers=HEADERS, timeout=20)
    if resp.status_code == 200:
        return "udiff", _parse_zip(resp.content)

    mon = d.strftime("%b").upper()
    legacy_url = LEGACY_URL.format(yyyy=d.year, mon=mon, ddmonyyyy=f"{d.day:02d}{mon}{d.year}")
    resp = requests.get(legacy_url, headers=HEADERS, timeout=20)
    if resp.status_code == 200:
        return "legacy", _parse_zip(resp.content)

    return None


def _parse_zip(content: bytes) -> list[dict]:
    z = zipfile.ZipFile(io.BytesIO(content))
    text = z.read(z.namelist()[0]).decode()
    return list(csv.DictReader(io.StringIO(text)))


def _num(v):
    try:
        return float(v) if v not in (None, "", "-") else None
    except ValueError:
        return None


class NseBhavcopySource(DataSource):
    name = "nse_bhavcopy"

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("nse_bhavcopy is prices-only — fundamentals come from bse_source.py/moneycontrol_source.py.")

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        needed = {r["symbol"] for r in conn.execute(
            "SELECT symbol FROM stocks s WHERE s.market = 'IN' "
            "AND NOT EXISTS (SELECT 1 FROM price_history ph WHERE ph.symbol = s.symbol)"
        ).fetchall()}
        if symbol:
            needed &= {symbol}
        elif limit:
            needed = set(list(needed)[:limit])
        if not needed:
            return 0

        earliest_reached = conn.execute(
            "SELECT MIN(date) FROM price_history WHERE source = 'nse_bhavcopy'"
        ).fetchone()[0]
        start_day = (date.fromisoformat(earliest_reached) - timedelta(days=1)) if earliest_reached else date.today()

        rows_written = 0
        run_deadline = time.monotonic() + MAX_RUN_SECONDS
        d = start_day

        while d >= EARLIEST_DATE and needed:
            if time.monotonic() > run_deadline:
                break
            time.sleep(RATE_LIMIT_SECONDS)
            try:
                result = _fetch_day(d)
            except Exception:  # noqa: BLE001 — a bad/corrupt day's ZIP must never kill a multi-year walk
                result = None

            if result is not None:
                schema, rows = result
                for row in rows:
                    if schema == "udiff":
                        sym, series = row.get("TckrSymb"), row.get("SctySrs")
                        o, h, l, c, v = row.get("OpnPric"), row.get("HghPric"), row.get("LwPric"), row.get("ClsPric"), row.get("TtlTradgVol")
                        ts = row.get("TradDt")  # already YYYY-MM-DD
                    else:
                        sym, series = row.get("SYMBOL"), row.get("SERIES")
                        o, h, l, c, v = row.get("OPEN"), row.get("HIGH"), row.get("LOW"), row.get("CLOSE"), row.get("TOTTRDQTY")
                        ts = d.isoformat()  # legacy TIMESTAMP is "DD-MON-YYYY"; we already know the real date we fetched

                    if series not in EQUITY_SERIES:
                        continue
                    full_sym = f"NSE:{sym}"
                    if full_sym not in needed:
                        continue

                    conn.execute(
                        "INSERT OR REPLACE INTO price_history (symbol, date, open, high, low, close, volume, source) "
                        "VALUES (?,?,?,?,?,?,?,'nse_bhavcopy')",
                        (full_sym, ts, _num(o), _num(h), _num(l), _num(c), _num(v)),
                    )
                    rows_written += 1
                    needed.discard(full_sym)
                conn.commit()
                if progress:
                    progress(rows_written)

            d -= timedelta(days=1)

        return rows_written
