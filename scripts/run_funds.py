"""Build/refresh data/funds.db (ETFs + index funds) and print a data-quality report.
    docker compose exec -d -e PYTHONPATH=/app app sh -c 'python /tmp/run_funds.py [US_SAMPLE_SIZE] > /tmp/funds.log 2>&1'"""
import logging
import random
import sys
import time

from app.funds_db import get_funds_conn
from app.ingestion import funds_source as fs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
US_SAMPLE = int(sys.argv[1]) if len(sys.argv) > 1 else 400
KNOWN = ["SPY", "VOO", "IVV", "VTI", "QQQ", "IWM", "EFA", "EEM", "AGG", "BND", "GLD", "SLV", "VUG", "VTV", "XLK", "XLF", "XLE", "XLV", "ARKK", "SCHD",
         "VNQ", "TLT", "LQD", "HYG", "VIG", "DIA", "IJH", "IJR", "VEA", "VWO", "SOXX", "SMH", "IBIT", "USO", "TQQQ", "SQQQ", "VXUS", "BNDX", "MBB", "VT"]


def step(label, fn):
    t = time.time()
    out = fn()
    print(f"[{time.time() - t:6.0f}s] {label}: {out}", flush=True)
    return out


US_ONLY = "--us-only" in sys.argv
with get_funds_conn() as fc:
    if not US_ONLY:
      step("US ETF list (Nasdaq)", lambda: fs.sync_us_etfs(fc))
      step("India ETF list (NSE)", lambda: fs.sync_in_etfs(fc))
      step("India index mutual funds (AMFI, Direct-Growth)", lambda: fs.sync_in_index_funds(fc))
      step("India holdings from index constituents (funds, rows)", lambda: fs.holdings_in(fc))
      step("India ETF price history (Yahoo .NS) (ETFs found, rows)", lambda: fs.history_in_etfs(fc))
      step("India index-fund NAV history (mfapi) (funds found, rows)", lambda: fs.history_in_index_funds(fc))
    allus = [r["ticker"] for r in fc.execute("SELECT ticker FROM funds WHERE market='US' ORDER BY ticker")]
    random.seed(11)
    sample = [t for t in KNOWN if t in set(allus)] + random.sample([t for t in allus if t not in KNOWN], max(0, US_SAMPLE - len(KNOWN)))
    step(f"US fund facts + history for {len(sample)} ETFs (with facts, with holdings)", lambda: fs.facts_us(fc, sample))
    print("\n=== DATA QUALITY ===")
    for k, v in fs.quality_report(fc).items():
        print(k, v)
print("DONE", flush=True)
