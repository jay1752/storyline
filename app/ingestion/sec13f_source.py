"""Real SEC Form 13F ingestion — Layer 3 (institutional holdings, US).

A genuinely different shape of problem from every other source here: not one
filing per company, but ~10,000 institutional managers each filing their own
full portfolio, needing aggregation UP to one row per symbol. Free, public, no
API key — same `SEC_EDGAR_USER_AGENT` fair-use requirement as sec_edgar_source.py.

Source file: SEC's quarterly bulk Form 13F data set (a ~100MB ZIP: SUBMISSION,
COVERPAGE, INFOTABLE, etc. — https://www.sec.gov/data-research/sec-markets-data/
form-13f-data-sets). INFOTABLE is the one that matters: one row per manager per
security held, keyed by CUSIP (not ticker). Verified directly this session:
3.8M holding rows, ~9,500 real 13F-HR filings in the latest window.

CUSIP -> ticker: the file's own FIGI column is only ~12% populated, not usable
alone. Resolved instead via OpenFIGI's free mapping API (no key needed — verified
live, correctly resolved Apple's CUSIP to AAPL). Mapping every CUSIP in the file
isn't worth it — most are tiny, obscure positions. Aggregates by CUSIP first
(cheap, local), takes the top N by aggregate dollar value, and only maps those —
covers the vast majority of total institutional value without chasing the long tail.

Deliberately out of scope for v1: percent of shares outstanding (would need a 4th
data source just for share counts). The signal this DOES capture — holder count,
total shares held, and quarter-over-quarter change — is the one analysts actually
use 13F data for ("is smart money accumulating or distributing"), and needs
nothing beyond what's already here.

Filing-window naming: SEC bundles filings into 3-month windows aligned to the
45-day-after-quarter-end deadline (e.g. "01jun2026-31aug2026" captures the filing
season for the quarter ending 2026-06-30). Computed from today's date rather than
hardcoded, so this doesn't go stale next quarter.
"""

import csv
import io
import time
import zipfile
from collections import Counter, defaultdict
from datetime import date

import requests

from app.ingestion.base import DataSource, NotConfiguredError
from app.ingestion.sec_edgar_source import UA

DATASET_BASE = "https://www.sec.gov/files/datastandardsinnovation/data/form-13f-data-sets"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
DEFAULT_TOP_N = 3000
OPENFIGI_BATCH_SIZE = 10  # unauthenticated cap
OPENFIGI_RATE_LIMIT_SECONDS = 2.5  # 25 req/min unauthenticated cap — stay safely under it


def _windows_for_year(year: int) -> list[tuple[date, date]]:
    feb_end = 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28
    return [
        (date(year - 1, 12, 1), date(year, 2, feb_end)),
        (date(year, 3, 1), date(year, 5, 31)),
        (date(year, 6, 1), date(year, 8, 31)),
        (date(year, 9, 1), date(year, 11, 30)),
    ]


def _latest_window(today: date) -> tuple[date, date, str]:
    """The most recently CONCLUDED 3-month filing window as of `today`, and its
    URL slug. Windows: Dec1-Feb28/29, Mar1-May31, Jun1-Aug31, Sep1-Nov30. Checks
    both this year's and last year's windows — needed for dates early in a year,
    where even the Dec-Feb window may not have concluded yet."""
    candidates = _windows_for_year(today.year) + _windows_for_year(today.year - 1)
    concluded = [w for w in candidates if w[1] < today]
    start, end = max(concluded, key=lambda w: w[1])
    slug = f"{start.day:02d}{start.strftime('%b').lower()}{start.year}-{end.day:02d}{end.strftime('%b').lower()}{end.year}"
    return start, end, slug


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


class Sec13FSource(DataSource):
    name = "sec_13f"

    def _headers(self):
        if not UA:
            raise NotConfiguredError("SEC_EDGAR_USER_AGENT not set — same requirement as sec_edgar_source.py.")
        return {"User-Agent": UA}

    def sync_universe(self, conn) -> int:
        raise NotImplementedError("sec_13f reuses the existing US universe from sec_edgar — nothing to sync here.")

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("sec_13f is institutional-holdings only — fundamentals come from sec_edgar.")

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("sec_13f is institutional-holdings only — prices come from tiingo.")

    def _download_and_aggregate(self) -> tuple[str, dict[str, dict]]:
        """Downloads the latest 13F bulk ZIP and returns (period_end_date, {cusip: agg}).
        Streams the ~400MB INFOTABLE.tsv row by row rather than loading it whole —
        this file is too big to hold as a list of dicts comfortably."""
        _, _, slug = _latest_window(date.today())
        url = f"{DATASET_BASE}/{slug}_form13f.zip"
        resp = requests.get(url, headers=self._headers(), timeout=120)
        resp.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(resp.content))

        valid_accessions = {}
        period_counts = Counter()
        with zf.open("SUBMISSION.tsv") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t")
            for row in reader:
                if row["SUBMISSIONTYPE"] not in ("13F-HR", "13F-HR/A"):
                    continue
                valid_accessions[row["ACCESSION_NUMBER"]] = True
                period_counts[row["PERIODOFREPORT"]] += 1
        period_raw = period_counts.most_common(1)[0][0]  # the quarter-end nearly every filing in this window reports as-of
        period_end_date = date(int(period_raw[-4:]), _MONTHS[period_raw[3:6].upper()], int(period_raw[:2])).isoformat()

        agg: dict[str, dict] = defaultdict(lambda: {"shares": 0, "value": 0, "managers": set(), "name": Counter()})
        with zf.open("INFOTABLE.tsv") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t")
            for row in reader:
                if row["ACCESSION_NUMBER"] not in valid_accessions:
                    continue
                cusip = row["CUSIP"]
                a = agg[cusip]
                try:
                    a["shares"] += int(row["SSHPRNAMT"] or 0)
                    a["value"] += int(row["VALUE"] or 0)
                except ValueError:
                    continue
                a["managers"].add(row["ACCESSION_NUMBER"])
                a["name"][row["NAMEOFISSUER"]] += 1

        return period_end_date, agg

    def _map_batch(self, cusips: list[str]) -> dict[str, str]:
        """{cusip: ticker} for one batch (<=10), US composite listings only, via
        OpenFIGI's free API."""
        try:
            resp = requests.post(
                OPENFIGI_URL,
                json=[{"idType": "ID_CUSIP", "idValue": c} for c in cusips],
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            results = resp.json()
        except requests.RequestException:
            return {}
        out = {}
        for cusip, result in zip(cusips, results):
            data = result.get("data") or []
            us_match = next((d for d in data if d.get("exchCode") == "US"), None)
            if us_match and us_match.get("ticker"):
                out[cusip] = us_match["ticker"].upper()
        return out

    def ingest_institutional(self, conn, limit=None, symbol=None, progress=None) -> int:
        top_n = limit or DEFAULT_TOP_N
        period_end_date, agg = self._download_and_aggregate()
        ranked = sorted(agg.items(), key=lambda kv: kv[1]["value"], reverse=True)[:top_n]
        known_symbols = {r["symbol"] for r in conn.execute("SELECT symbol FROM stocks WHERE market = 'US'").fetchall()}
        rows_written = 0

        # Mapped and written per OpenFIGI batch, not all-mapped-then-all-written —
        # a full run is ~300 rate-limited batches (~12 min), and writing only at the
        # very end left rows_written at 0 the whole time, looking hung even though
        # it wasn't (confirmed via low CPU + steady network during a real run).
        for i in range(0, len(ranked), OPENFIGI_BATCH_SIZE):
            batch = ranked[i:i + OPENFIGI_BATCH_SIZE]
            time.sleep(OPENFIGI_RATE_LIMIT_SECONDS)
            cusip_map = self._map_batch([cusip for cusip, _ in batch])

            for cusip, a in batch:
                ticker = cusip_map.get(cusip)
                if not ticker:
                    continue
                sym = f"NASDAQ:{ticker}"
                if sym not in known_symbols:
                    continue
                if symbol and sym != symbol:
                    continue

                prior = conn.execute(
                    "SELECT total_shares_held FROM institutional_holdings WHERE symbol = ? AND period_end_date < ? "
                    "ORDER BY period_end_date DESC LIMIT 1",
                    (sym, period_end_date),
                ).fetchone()
                qoq_pct = None
                if prior and prior["total_shares_held"]:
                    qoq_pct = round((a["shares"] / prior["total_shares_held"] - 1) * 100, 1)

                conn.execute(
                    """INSERT OR REPLACE INTO institutional_holdings
                    (symbol, period_end_date, holder_count, total_shares_held, total_value_usd,
                     shares_qoq_change_pct, source)
                    VALUES (?,?,?,?,?,?,'sec_13f')""",
                    (sym, period_end_date, len(a["managers"]), a["shares"], a["value"], qoq_pct),
                )
                rows_written += 1
                conn.commit()
                if progress:
                    progress(rows_written)

        return rows_written
