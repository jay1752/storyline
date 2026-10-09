"""Fill stocks.isin for Indian stocks from NSE's EQUITY_L.csv and AMFI's market-cap list (both carry ISIN + NSE symbol).
ISIN is what broker statements print next to every security, so it is the one unambiguous key for importing positions.
    docker compose exec -T -e PYTHONPATH=/app app python /tmp/fill_isin.py"""
import csv
import io
import io as _io
import xml.etree.ElementTree as ET
import zipfile

import requests

from app.db import get_market_conn

H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
isin_by = {}

r = requests.get("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv", headers=H, timeout=40)
for row in csv.DictReader(io.StringIO(r.text)):
    sym, isin = row.get("SYMBOL", "").strip(), row.get(" ISIN NUMBER", "").strip()
    if sym and isin:
        isin_by[sym] = isin
print("NSE EQUITY_L:", len(isin_by))

r = requests.get("https://portal.amfiindia.com/spages/AverageMarketCapitalization30Jun2026.xlsx", headers=H, timeout=90)
z = zipfile.ZipFile(_io.BytesIO(r.content))
ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
ss = ["".join(t.itertext()) for t in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns)]
amfi = 0
for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter("{%s}row" % ns["m"]):
    vals = []
    for c in row.findall("m:c", ns):
        v = c.find("m:v", ns)
        vals.append(None if v is None else (ss[int(v.text)] if c.get("t") == "s" else v.text))
    if len(vals) > 5 and vals[2] and str(vals[2]).startswith("INE"):
        for sym in (vals[5], vals[3]):
            if sym and sym != "-":
                if isin_by.setdefault(sym.strip(), vals[2].strip()) == vals[2].strip():
                    amfi += 1
print("AMFI added:", amfi, "| total symbols with ISIN:", len(isin_by))

with get_market_conn() as m:
    if "isin" not in {r[1] for r in m.execute("PRAGMA table_info(stocks)")}:
        m.execute("ALTER TABLE stocks ADD COLUMN isin TEXT")
    n = 0
    for (sym,) in m.execute("SELECT symbol FROM stocks WHERE market='IN'").fetchall():
        isin = isin_by.get(sym.split(":", 1)[1])
        if isin:
            m.execute("UPDATE stocks SET isin=? WHERE symbol=?", (isin, sym)); n += 1
    m.execute("CREATE INDEX IF NOT EXISTS idx_stocks_isin ON stocks(isin) WHERE isin IS NOT NULL")
    m.commit()
    print("stocks updated with ISIN:", n, "of", m.execute("SELECT COUNT(*) FROM stocks WHERE market='IN'").fetchone()[0])
    print(m.execute("SELECT symbol, isin FROM stocks WHERE symbol IN ('NSE:ANGELONE','NSE:GOLDBEES','NSE:SBIN','NSE:RELIANCE')").fetchall())
