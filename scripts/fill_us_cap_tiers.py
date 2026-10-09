"""US cap tier (+ sector where still Unclassified) from Nasdaq's public screener list (~7,000 listed US stocks).
Tiers by market cap: large >= $10B, mid >= $2B, small >= $300M, micro below.
    docker compose exec -e PYTHONPATH=/app app python /tmp/fill_us_cap_tiers.py"""
import requests

from app.db import get_market_conn

H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
     "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
rows = requests.get("https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25&offset=0&download=true", headers=H, timeout=60).json()["data"]["rows"]


def tier(cap):
    return "large" if cap >= 10e9 else "mid" if cap >= 2e9 else "small" if cap >= 3e8 else "micro"


by = {}
for r in rows:
    try:
        cap = float(r["marketCap"])
    except (TypeError, ValueError):
        continue
    if cap > 0:
        by[r["symbol"].strip().upper().replace("/", "-")] = (cap, r.get("sector") or None)
with get_market_conn() as m:
    n = s = 0
    for sym, in m.execute("SELECT symbol FROM stocks WHERE market='US'").fetchall():
        t = sym.split(":", 1)[1]
        hit = by.get(t) or by.get(t.replace(".", "-"))
        if not hit:
            continue
        m.execute("UPDATE stocks SET cap_tier=? WHERE symbol=?", (tier(hit[0]), sym)); n += 1
        if hit[1]:
            s += m.execute("UPDATE stocks SET sector=? WHERE symbol=? AND (sector IS NULL OR sector IN ('','Unclassified'))", (hit[1], sym)).rowcount
    m.commit()
    print("screener rows", len(by), "tiers set", n, "sectors filled", s)
    print(m.execute("SELECT cap_tier, COUNT(*) FROM stocks WHERE market='US' GROUP BY 1").fetchall())
