"""India inputs for the scan's coverage / liquidity rows, from BSE's own XBRL filings (same endpoints bse_source.py uses).

  * Finance costs  — from each of the last 5 QUARTERLY filings (₹ Cr), for interest coverage = (PBT + finance costs) / finance costs.
  * Current assets, current liabilities, inventories — from the latest YEARLY filing's balance sheet (BSE's quarterly filings
    don't carry a balance sheet), stored on the row for that fiscal year-end; for current and quick ratios.
  * Operating cash flow — NOT in these filings (checked on TCS, MCX, Dixon, Bajaj Finance), so cfo_cr stays NULL for India.

It only ever UPDATEs those four columns on rows bse_source.py already wrote, so nothing else on a row can change.
Banks and lenders have no classified balance sheet (no CurrentAssets tag): their current/quick ratio stays NULL, correctly."""

import time

import requests

from app.ingestion.base import DataSource, apply_symbol_filter
from app.ingestion.bse_source import BseSource, _num, _period_end_from_filing

RATE_LIMIT_SECONDS = 0.3
QUARTERS = 5


class BseRatioInputsSource(DataSource):
    name = "bse_ratios"

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("bse_ratios only fills ratio inputs on quarterly_data.")

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None, force=False) -> int:
        bse = BseSource()
        scrip = {f"NSE:{r['scrip_id']}": r["SCRIP_CD"] for r in bse._scrip_rows() if r.get("scrip_id")}
        symbols = [r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM quarterly_data WHERE symbol IN (SELECT symbol FROM stocks WHERE market = 'IN')")]
        symbols = apply_symbol_filter(symbols, limit, symbol)
        if not symbol and not force:  # resume: a symbol whose newest quarter already has its finance cost has been done
            done = {r[0] for r in conn.execute(
                "SELECT q.symbol FROM quarterly_data q WHERE q.period_end_date = (SELECT MAX(period_end_date) FROM quarterly_data q2 WHERE q2.symbol = q.symbol) "
                "AND q.finance_cost_cr IS NOT NULL")}
            symbols = [s for s in symbols if s not in done]
        written = 0
        for sym in symbols:
            cd = scrip.get(sym)
            if not cd:
                continue
            time.sleep(RATE_LIMIT_SECONDS)
            try:
                filings = bse._fetch_filings(cd)
            except requests.RequestException:
                continue
            existing = {r[0] for r in conn.execute("SELECT period_end_date FROM quarterly_data WHERE symbol = ?", (sym,))}
            for f in bse._pick_latest_per_period(filings, "Qtr")[:QUARTERS]:
                end = _period_end_from_filing(f)
                if not end or end not in existing or not f.get("XbrlFile"):
                    continue
                time.sleep(RATE_LIMIT_SECONDS)
                facts = bse._fetch_xbrl(f["XbrlFile"])
                fc = _num((facts or {}).get("FinanceCosts"))
                if fc is not None:
                    conn.execute("UPDATE quarterly_data SET finance_cost_cr = ? WHERE symbol = ? AND period_end_date = ? AND source != 'indianapi'", (fc, sym, end))
                    written += 1
            for f in bse._pick_latest_per_period(filings, "Yearly")[:1]:
                end = _period_end_from_filing(f)
                if not end or end not in existing or not f.get("XbrlFile"):
                    continue
                time.sleep(RATE_LIMIT_SECONDS)
                facts = bse._fetch_xbrl(f["XbrlFile"]) or {}
                ca, cl, inv = _num(facts.get("CurrentAssets")), _num(facts.get("CurrentLiabilities")), _num(facts.get("Inventories"))
                if ca is not None and cl:
                    conn.execute("UPDATE quarterly_data SET current_assets_cr = ?, current_liabilities_cr = ?, inventory_cr = ? WHERE symbol = ? AND period_end_date = ?",
                                 (ca, cl, inv, sym, end))
                    written += 1
            conn.commit()
            if progress:
                progress(written)
        return written
