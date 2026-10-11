"""Fill market.db.india_mcap from AMFI's official half-yearly list "Average Market Capitalization of listed companies": every listed company
(about 5,400, NSE and BSE) with its 6-month average market cap in Rs crore and its SEBI category (Large / Mid / Small).
Matched to our stocks by ISIN, then NSE symbol, then BSE symbol. Also refreshes stocks.cap_tier for every matched stock (a stock the older
cap_tiers.json did not cover gets its official tier; nothing is guessed for unmatched ones).

AMFI publishes a new file after each half-year (30 Jun, 31 Dec); pass --url for a newer one.
    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/fill_india_mcap.py [--url ...] [--dry]"""
import argparse
import io
import re
import sqlite3
import sys
import zipfile

import requests

try:  # the file is AMFI's own, but parse untrusted-safe anyway
    from defusedxml import ElementTree as ET
except ImportError:  # pragma: no cover
    import xml.etree.ElementTree as ET

from app.db import DATA_DIR

URL = "https://portal.amfiindia.com/spages/AverageMarketCapitalization30Jun2026.xlsx"
H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIER = {"Large Cap": "large", "Mid Cap": "mid", "Small Cap": "small"}
SCHEMA = """CREATE TABLE IF NOT EXISTS india_mcap (
    symbol TEXT PRIMARY KEY, mcap_cr REAL, tier TEXT, amfi_rank INTEGER, as_of TEXT, isin TEXT)"""


def read_rows(content: bytes) -> list[list]:
    z = zipfile.ZipFile(io.BytesIO(content))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    ss = ["".join(t.itertext()) for t in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns)]
    rows = []
    for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter("{%s}row" % ns["m"]):
        vals = []
        for c in row.findall("m:c", ns):
            v = c.find("m:v", ns)
            vals.append(None if v is None else (ss[int(v.text)] if c.get("t") == "s" else v.text))
        rows.append(vals)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=URL)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    content = requests.get(a.url, headers=H, timeout=120).content
    rows = read_rows(content)
    as_of = (re.search(r"ended (\d+ \w+ \d{4})", rows[0][0] or "") or [None, a.url])[1]
    by_isin, by_sym = {}, {}
    for r in rows:
        if len(r) < 11 or not (r[2] or "").startswith("INE"):
            continue
        try:
            rank, mcap = int(r[0]), float(r[9])
        except (TypeError, ValueError):
            continue
        rec = (mcap, TIER.get((r[10] or "").strip()), rank, r[2].strip())
        by_isin[r[2].strip()] = rec
        for sym in (r[5], r[3]):
            if sym and sym != "-":
                by_sym.setdefault(sym.strip(), rec)
    print(f"AMFI list ({as_of}): {len(by_isin)} companies")
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=120)
    m.execute(SCHEMA)
    stocks = m.execute("SELECT symbol, isin, cap_tier FROM stocks WHERE market='IN'").fetchall()
    matched = changed_tier = newly_tiered = 0
    for sym, isin, tier in stocks:
        rec = by_isin.get(isin) if isin else None
        rec = rec or by_sym.get(sym.split(":", 1)[1])
        if not rec:
            continue
        matched += 1
        if rec[1] and tier != rec[1]:
            if tier is None:
                newly_tiered += 1
            else:
                changed_tier += 1
        if not a.dry:
            m.execute("INSERT OR REPLACE INTO india_mcap (symbol, mcap_cr, tier, amfi_rank, as_of, isin) VALUES (?,?,?,?,?,?)", (sym, rec[0], rec[1], rec[2], as_of, rec[3]))
            if rec[1]:
                m.execute("UPDATE stocks SET cap_tier=? WHERE symbol=?", (rec[1], sym))
    if not a.dry:
        m.commit()
    print(f"matched {matched} of {len(stocks)} India stocks | tier set for {newly_tiered} that had none, changed for {changed_tier}{' (dry run, nothing written)' if a.dry else ''}")


if __name__ == "__main__":
    sys.exit(main())
