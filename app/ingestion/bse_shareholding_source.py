"""Real BSE Shareholding Pattern ingestion — India promoter/FII/DII % (fii_pct,
dii_pct, promoter_pct on quarterly_data). Confirmed genuinely free and public this
session — no BSE paid Corporate Data subscription needed for this field, contrary to
the earlier assumption in STATUS.md's vendor-pricing research (that pricing sheet
covers Financial Results/OHLC/Company Master; shareholding pattern turns out to have
its own free, separately-discoverable endpoint).

Found the same way bse_source.py's endpoints were: inspecting real network calls on a
live BSE page (performance.getEntriesByType, since this SPA's XHR calls don't show up
in a simple fetch-monkeypatch capture the way bse_source.py's did). Two calls per
symbol:
  1. SHPQNewFormat/w?scripcode=X — lists every quarter ever filed, each with its own
     XBRL filename (XbrlFile field).
  2. https://www.bseindia.com/XBRL1/{XbrlFile} — the actual filing, plain XBRL (tag,
     contextRef, value) under BSE's own `in-bse-shp` taxonomy. A different shape than
     bse_source.py's inline-XBRL financial-results filings (in-capmkt / ix:nonFraction
     embedded in an HTML wrapper) — this is real standalone XBRL, no HTML unwrapping
     needed.

Enrichment-only, like finnhub_source.py: UPDATEs fii_pct/dii_pct/promoter_pct on
quarterly_data rows bse_source.py already wrote (matched by nearest period_end_date —
shareholding pattern and financial results are separate filings with their own
deadlines, not guaranteed to land on the same date). Never INSERTs its own row.

Value format is inconsistent across filings — verified directly on two different Wipro
filings: one encoded promoter % as a raw fraction (0.7259), another as an already-
scaled percentage (72.67), same tag, same company, different quarters. Disambiguated
by magnitude: no real shareholding % is a legitimate fraction above 1, so anything
<=1 is treated as a fraction and multiplied by 100."""

import re
import time
from datetime import date

import requests

from app.ingestion.base import DataSource, apply_symbol_filter
from app.ingestion.bse_source import API_BASE, HEADERS, SITE_BASE

RATE_LIMIT_SECONDS = 0.3  # same conservative default as bse_source.py — undocumented endpoint, no published limit
DATE_TOLERANCE_DAYS = 20
# Raised from 8 (~2yrs) to match moneycontrol_source.py's depth treatment. This is
# still enrichment-only — it can only attach FII/DII/Promoter% to a quarterly_data
# row that already EXISTS (matched by nearest period_end_date), never creates one.
# So the real-world effect: symbols with moneycontrol's annual history (period-end
# always March 31) now get FII/DII/Promoter% backfilled onto those old rows too,
# since BSE's own Q4/March shareholding filing naturally lands close to the same
# date. Symbols with only bse_source.py's ~2yr window see no change — there's no
# older row to attach to regardless of how far back this pulls.
MAX_FILINGS_PER_SYMBOL = 60  # ~15 years of quarterly filings — BSE's shareholding archive goes back to 2018+ for most large caps, confirmed earlier this session

PCT_TAG = "ShareholdingAsAPercentageOfTotalNumberOfShares"
PROMOTER_CONTEXT = "ShareholdingOfPromoterAndPromoterGroup_ContextI"
FII_CONTEXT = "InstitutionsForeign_ContextI"
DII_CONTEXT = "InstitutionsDomestic_ContextI"

# Promoter pledge was always NULL — this tag was never looked for. Confirmed live: a
# plain boolean flag (contextRef="MainI", not one of the per-category "_ContextI"
# percentage contexts above), "true"/"false" — verified both values on real filings
# (NSE:ADANIENT and NSE:ZEEL both "true", NSE:RELIANCE and NSE:VEDL both "false",
# matching each company's actual known pledge status). No separate "% pledged" tag
# exists in this taxonomy, so this stays a plain Yes/No summary, matching the
# schema's own documented shape (`promoter_pledge TEXT -- e.g. "No pledge"`) rather
# than fabricating a percentage that was never actually reported.
PROMOTER_PLEDGE_TAG = "WhetherAnySharesHeldByPromotersAreEncumberedUnderPledgedForPromoterAndPromoterGroup"

_INSTANT_RE = re.compile(r'<xbrli:context id="MainI">.*?<xbrli:instant>([^<]+)</xbrli:instant>', re.DOTALL)


def _pct_for_context(xml: str, context_id: str) -> float | None:
    pattern = re.compile(
        r'<in-bse-shp:' + PCT_TAG + r'[^>]*contextRef="' + re.escape(context_id) + r'"[^>]*>([^<]*)<'
    )
    m = pattern.search(xml)
    if not m or not m.group(1).strip():
        return None
    try:
        val = float(m.group(1).strip())
    except ValueError:
        return None
    return round(val * 100, 2) if val <= 1 else round(val, 2)


def _promoter_pledge(xml: str) -> str | None:
    pattern = re.compile(
        r'<in-bse-shp:' + PROMOTER_PLEDGE_TAG + r'[^>]*contextRef="MainI"[^>]*>([^<]*)<'
    )
    m = pattern.search(xml)
    if not m or not m.group(1).strip():
        return None
    return "Pledged" if m.group(1).strip().lower() == "true" else "No pledge"


class BseShareholdingSource(DataSource):
    name = "bse_shareholding"

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("bse_shareholding only enriches fii_pct/dii_pct/promoter_pct on quarterly_data.")

    def _scrip_map(self) -> dict[str, str]:
        resp = requests.get(
            f"{API_BASE}/ListOfScripData/w",
            params={"Group": "", "Scripcode": "", "industry": "", "segment": "Equity", "status": "Active"},
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        return {f"NSE:{r['scrip_id']}": r["SCRIP_CD"] for r in resp.json() if r.get("scrip_id")}

    def _filing_list(self, scrip_cd: str) -> list[dict]:
        resp = requests.get(f"{API_BASE}/SHPQNewFormat/w", params={"scripcode": scrip_cd}, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.json().get("Table") or []

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        scrip_map = self._scrip_map()
        symbols = [r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM quarterly_data WHERE symbol IN "
            "(SELECT symbol FROM stocks WHERE market = 'IN')"
        ).fetchall()]
        symbols = apply_symbol_filter(symbols, limit, symbol)

        if not symbol:
            # Resume, don't restart: this source had NO skip-already-covered logic
            # either (same bug class as fivepaisa_source.py, fixed alongside this) —
            # every re-trigger re-fetched and re-parsed every symbol's full filing
            # history from scratch, most of it wasted re-work. "Already covered"
            # here means the EARLIEST quarterly_data row we know for that symbol
            # already has promoter_pct filled — that's the honest signal that this
            # symbol has been backfilled as deep as it's going to go, whether that's
            # genuinely multi-year (a moneycontrol anchor existed) or just its
            # existing shallow ~2yr window (no older anchor to reach regardless of
            # how many more times this runs). An explicit `symbol` re-ingestion
            # still always runs regardless, same as every other source.
            #
            # Also requires promoter_pledge, not just promoter_pct — a real gap found
            # live: promoter_pledge extraction was added after this resume logic
            # already existed, so ~1,900 symbols that were "already covered" by the
            # old (promoter_pct-only) criterion would otherwise never be revisited by
            # an ordinary bulk run, permanently missing pledge data with no error and
            # no way to self-heal. Requiring both fields means a symbol genuinely
            # needs everything this source can give before it's skipped.
            already_covered = {r["symbol"] for r in conn.execute(
                """
                SELECT q.symbol FROM quarterly_data q
                WHERE q.symbol IN (SELECT symbol FROM stocks WHERE market = 'IN')
                  AND q.period_end_date = (
                      SELECT MIN(q2.period_end_date) FROM quarterly_data q2 WHERE q2.symbol = q.symbol
                  )
                  AND q.promoter_pct IS NOT NULL
                  AND q.promoter_pledge IS NOT NULL
                """
            ).fetchall()}
            symbols = [s for s in symbols if s not in already_covered]

        rows_written = 0

        for sym in symbols:
            scrip_cd = scrip_map.get(sym)
            if not scrip_cd:
                continue
            time.sleep(RATE_LIMIT_SECONDS)
            try:
                filings = self._filing_list(scrip_cd)
            except requests.RequestException:
                continue
            if not filings:
                continue

            existing_periods = [r["period_end_date"] for r in conn.execute(
                "SELECT period_end_date FROM quarterly_data WHERE symbol = ?", (sym,)
            ).fetchall()]
            if not existing_periods:
                continue

            # newest first, same convention as bse_source.py's _pick_latest_per_period
            filings = sorted(filings, key=lambda f: (f.get("yr") or "", f.get("qtrid") or 0), reverse=True)
            for f in filings[:MAX_FILINGS_PER_SYMBOL]:
                xbrl_file = f.get("XbrlFile")
                if not xbrl_file:
                    continue
                time.sleep(RATE_LIMIT_SECONDS)
                try:
                    resp = requests.get(f"{SITE_BASE}/XBRL1/{xbrl_file}", headers=HEADERS, timeout=30)
                    resp.raise_for_status()
                    xml = resp.text
                except requests.RequestException:
                    continue

                instant_match = _INSTANT_RE.search(xml)
                if not instant_match:
                    continue
                try:
                    filing_date = date.fromisoformat(instant_match.group(1).strip())
                except ValueError:
                    continue

                nearest = min(
                    existing_periods,
                    key=lambda p: abs((date.fromisoformat(p) - filing_date).days),
                    default=None,
                )
                if nearest is None or abs((date.fromisoformat(nearest) - filing_date).days) > DATE_TOLERANCE_DAYS:
                    continue

                promoter_pct = _pct_for_context(xml, PROMOTER_CONTEXT)
                fii_pct = _pct_for_context(xml, FII_CONTEXT)
                dii_pct = _pct_for_context(xml, DII_CONTEXT)
                promoter_pledge = _promoter_pledge(xml)
                if promoter_pct is None and fii_pct is None and dii_pct is None and promoter_pledge is None:
                    continue

                conn.execute(
                    "UPDATE quarterly_data SET promoter_pct = COALESCE(?, promoter_pct), "
                    "fii_pct = COALESCE(?, fii_pct), dii_pct = COALESCE(?, dii_pct), "
                    "promoter_pledge = COALESCE(?, promoter_pledge) "
                    "WHERE symbol = ? AND period_end_date = ?",
                    (promoter_pct, fii_pct, dii_pct, promoter_pledge, sym, nearest),
                )
                rows_written += 1
            conn.commit()
            if progress:
                progress(rows_written)

        return rows_written
