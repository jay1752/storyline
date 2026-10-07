"""NSE's corporate financial-results archive — fills the missing YoY revenue growth on
India quarters that bse_source.py couldn't compute.

Why it's needed (measured live): 15,536 of 24,500 BSE quarter rows (63%, 4,222 stocks)
had revenue but no revenue_growth_pct. A quarter's YoY growth needs the SAME quarter a
year earlier, and BSE can't supply it: each quarterly BSE filing's XBRL carries only the
current period (verified: contexts are OneD/OneI only — the prior-period FourD appears
only in yearly filings), and BSE's filing list is capped at ~20 entries (~5 quarters for
most filers), so last year's quarter has usually scrolled out of reach. yfinance (2-6
quarters from Mar-2025) and MoneyControl (annual only) don't have it either.

NSE does. `/api/corporates-financial-results` lists every quarterly filing back to 2005
per company (verified live: RELIANCE 130, TCS 162, INFY 144 entries), each linking to its
XBRL on nsearchives.nseindia.com — an archive host that has never blocked us. Same
`in-bse-fin` taxonomy as BSE's filings, so the same tag names apply. Coverage seen live
stops around Dec-2024, which is the complement of BSE's window (Mar-2025 onward): exactly
the prior-year quarters the Mar-2025..Dec-2025 rows need.

What this writes: ONLY revenue_growth_pct, ONLY where it's currently NULL, computed
from the row's own stored revenue and NSE's reported revenue for the quarter ~365 days
earlier. It does not insert quarters: a March-quarter row would collide with
MoneyControl's March annual (FY) row, which shares the (symbol, period_end_date) key,
and mixing the two granularities in one row is the corruption risk already flagged
for this table.

Basis (consolidated vs standalone) must be VERIFIED, not assumed — measured live on a
45-symbol sample: for the 12 symbols where both sources cover the same quarter, BSE's
stored revenue matched an NSE basis for only 5; for 7 it matched neither (e.g. NSIL
Sep-2024: BSE 21.3 vs NSE 59.7 consolidated / 41.6 standalone; the BSE file is a Nov-2025
re-upload, so a revision is plausible, but which side is right can't be told from here).
Computing growth from a BSE value that disagrees with the official filing would produce a
confident wrong number, which is worse than a blank. So per symbol:
  - if a quarter exists in both sources, NSE's revenue must match BSE's stored revenue
    within 1% for one basis, and that basis is used; no match -> skip the symbol;
  - if they don't overlap (typical: NSE ends ~Dec-2024, BSE starts ~Mar-2025), NSE's
    latest quarter must flow into BSE's first within +/-30% (adjacent quarters), and the
    basis closer to BSE's level is used; otherwise skip.

Headers are deliberately bare (User-Agent only): same lesson as bse_source.py — a full
browser-mimicking header set is what Akamai flags, not a plain client. nseindia.com's
HOME page 403s, but the /api/ path answers without any cookie handshake (verified)."""

import math
import re
import time
from datetime import date, datetime, timedelta

import requests

from app.ingestion.base import DataSource, apply_symbol_filter, safe_growth_pct

API_URL = "https://www.nseindia.com/api/corporates-financial-results"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
}
RATE_LIMIT_SECONDS = 0.4
MAX_RUN_SECONDS = 2700  # 45 min — resumable: finished symbols are recorded in nse_results_attempts
RETRY_AFTER_DAYS = 30  # don't re-query a symbol whose NSE lookup already ran this recently
LOOKBACK_START = "01-01-2022"
COMPARATOR_TOLERANCE_DAYS = 10
OVERLAP_TOLERANCE = 0.01  # BSE vs NSE revenue for the SAME quarter must agree within 1%
CONTINUITY_MIN, CONTINUITY_MAX = 0.75, 1.33  # adjacent-quarter sanity band when there's no shared quarter

# Same preference order as bse_source.REVENUE_TAGS — a bank's "revenue" is InterestEarned.
REVENUE_TAGS = ["RevenueFromOperations", "InterestEarned", "NetPremiumIncome", "GrossPremiumIncome"]
_FACT_RE = {
    tag: re.compile(r"<[\w\-]+:%s\b[^>]*?contextRef=\"OneD\"[^>]*>([^<]+)<" % tag) for tag in REVENUE_TAGS
}
CRORE = 1e7  # NSE's plain XBRL carries full rupees (verified: RELIANCE Q4 FY24 = 2,407,150,000,000)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nse_results_attempts (
    symbol       TEXT PRIMARY KEY,
    attempted_at TEXT NOT NULL,
    filled       INTEGER NOT NULL DEFAULT 0,
    note         TEXT
)
"""


def _to_iso(d: str) -> str | None:
    """'31-Dec-2024' -> '2024-12-31'."""
    try:
        return datetime.strptime(d, "%d-%b-%Y").date().isoformat()
    except (ValueError, TypeError):
        return None


def _index_filings(filings: list[dict]) -> dict[str, dict[str, dict]]:
    """{period_end_iso: {basis: filing}} where basis is "Consolidated" or
    "Non-Consolidated". Only usable filings (a real .xml link — some entries carry a
    placeholder ending in "/-") and non-cumulative quarterlies are kept; for a revised
    filing the latest broadcast wins."""
    index: dict[str, dict[str, dict]] = {}
    for f in filings:
        if f.get("period") != "Quarterly" or f.get("cumulative") not in (None, "Non-cumulative"):
            continue
        end = _to_iso(f.get("toDate"))
        if not end or not str(f.get("xbrl") or "").lower().endswith(".xml"):
            continue
        basis = "Consolidated" if f.get("consolidated") == "Consolidated" else "Non-Consolidated"
        cur = index.setdefault(end, {}).get(basis)
        if cur is None or (f.get("broadCastDate") or "") > (cur.get("broadCastDate") or ""):
            index[end][basis] = f
    return index


def _revenue_cr(xbrl_text: str) -> float | None:
    for tag in REVENUE_TAGS:
        m = _FACT_RE[tag].search(xbrl_text)
        if m:
            try:
                return float(m.group(1).replace(",", "")) / CRORE
            except ValueError:
                continue
    return None


class NseResultsSource(DataSource):
    name = "nse_results"

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("nse_results is fundamentals-only (revenue growth enrichment).")

    def _list_filings(self, ticker: str) -> list[dict]:
        resp = requests.get(
            API_URL,
            params={
                "index": "equities", "symbol": ticker, "period": "Quarterly",
                "from_date": LOOKBACK_START, "to_date": date.today().strftime("%d-%m-%Y"),
            },
            headers=HEADERS, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def _fetch_revenue(self, xbrl_url: str) -> float | None:
        resp = requests.get(xbrl_url, headers=HEADERS, timeout=40)
        resp.raise_for_status()
        return _revenue_cr(resp.text)

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        conn.execute(_SCHEMA)

        # Symbols with at least one BSE quarter that has revenue but no growth.
        needy = [r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM quarterly_data WHERE source = 'bse' AND revenue_cr IS NOT NULL "
            "AND revenue_growth_pct IS NULL AND quarter_label NOT LIKE '%annual%'"
        ).fetchall()]
        needy = apply_symbol_filter(needy, limit, symbol)

        if not symbol:
            cutoff = (datetime.now() - timedelta(days=RETRY_AFTER_DAYS)).isoformat()
            recently = {r[0] for r in conn.execute(
                "SELECT symbol FROM nse_results_attempts WHERE attempted_at > ?", (cutoff,)
            ).fetchall()}
            needy = [s for s in needy if s not in recently]

        rows_written = 0
        deadline = time.monotonic() + MAX_RUN_SECONDS

        for sym in needy:
            if time.monotonic() > deadline:
                break
            ticker = sym.split(":", 1)[1]
            stored = {
                r["period_end_date"]: r["revenue_cr"] for r in conn.execute(
                    "SELECT period_end_date, revenue_cr FROM quarterly_data WHERE symbol = ? AND source = 'bse' "
                    "AND revenue_cr IS NOT NULL AND quarter_label NOT LIKE '%annual%'",
                    (sym,),
                ).fetchall()
            }
            targets = [r for r in conn.execute(
                "SELECT period_end_date, revenue_cr FROM quarterly_data WHERE symbol = ? AND source = 'bse' "
                "AND revenue_cr IS NOT NULL AND revenue_growth_pct IS NULL AND quarter_label NOT LIKE '%annual%'",
                (sym,),
            ).fetchall()]

            time.sleep(RATE_LIMIT_SECONDS)
            try:
                index = _index_filings(self._list_filings(ticker))
            except (requests.RequestException, ValueError):
                continue  # transient — not recorded as attempted, so the next run retries it

            filled, note = 0, None
            cache: dict[str, float | None] = {}

            def revenue(filing):
                url = filing["xbrl"]
                if url not in cache:
                    time.sleep(RATE_LIMIT_SECONDS)
                    try:
                        cache[url] = self._fetch_revenue(url)
                    except requests.RequestException:
                        cache[url] = None
                return cache[url]

            basis = None
            if not index:
                note = "no NSE quarterly filings listed"
            else:
                basis, note = self._verified_basis(index, stored, revenue)

            if basis:
                for t in targets:
                    want = date.fromisoformat(t["period_end_date"]) - timedelta(days=365)
                    prior_end = next(
                        (p for p, by in index.items()
                         if basis in by and abs((date.fromisoformat(p) - want).days) <= COMPARATOR_TOLERANCE_DAYS),
                        None,
                    )
                    if prior_end is None:
                        continue
                    growth = safe_growth_pct(t["revenue_cr"], revenue(index[prior_end][basis]))
                    if growth is None:
                        continue
                    conn.execute(
                        "UPDATE quarterly_data SET revenue_growth_pct = ? "
                        "WHERE symbol = ? AND period_end_date = ? AND revenue_growth_pct IS NULL",
                        (growth, sym, t["period_end_date"]),
                    )
                    filled += 1
                    rows_written += 1

            conn.execute(
                "INSERT OR REPLACE INTO nse_results_attempts (symbol, attempted_at, filled, note) VALUES (?,?,?,?)",
                (sym, datetime.now().isoformat(), filled, note),
            )
            conn.commit()
            if progress:
                progress(rows_written)

        return rows_written

    @staticmethod
    def _verified_basis(index, stored, revenue):
        """Returns (basis, note). basis is None when it can't be established safely."""
        overlap = sorted((p for p in index if p in stored), reverse=True)
        if overlap:
            p = overlap[0]
            matches = [
                b for b, f in index[p].items()
                if (v := revenue(f)) and stored[p] and abs(v / stored[p] - 1) <= OVERLAP_TOLERANCE
            ]
            if not matches:
                return None, "BSE and NSE revenue disagree for the same quarter - skipped as unverifiable"
            # Both bases matching means they're equal (no separate subsidiaries), so
            # either gives the same answer — prefer Consolidated, like bse_source.
            return ("Consolidated" if "Consolidated" in matches else matches[0]), None

        # No shared quarter: NSE's latest quarter must flow into BSE's first one.
        latest = max(index)
        later = sorted(p for p in stored if p > latest)
        if not later or not stored[later[0]]:
            return None, "no BSE quarter after NSE's coverage to verify against"
        first_bse = stored[later[0]]
        best = None
        for b, f in index[latest].items():
            v = revenue(f)
            if not v:
                continue
            ratio = v / first_bse
            if not (CONTINUITY_MIN <= ratio <= CONTINUITY_MAX):
                continue
            score = abs(math.log(ratio))
            if best is None or score < best[0]:
                best = (score, b)
        if best is None:
            return None, "NSE's last quarter doesn't flow into BSE's first - basis unverifiable"
        return best[1], None
