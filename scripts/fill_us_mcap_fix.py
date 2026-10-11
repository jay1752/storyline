"""US market cap from Yahoo for the stocks where price x SEC share count disagrees with Nasdaq's size band (multi-class companies such as
BRK-B, stale cover-page share counts). Writes us_mcap_override; build_metrics prefers it only in those cases.
    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/fill_us_mcap_fix.py"""
import datetime as dt
import sqlite3
import time

import yfinance as yf

from app.db import DATA_DIR

FLOOR = {"large": 10_000, "mid": 2_000, "small": 300}
m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=300)
m.execute("CREATE TABLE IF NOT EXISTS us_mcap_override (symbol TEXT PRIMARY KEY, mcap_m REAL, as_of TEXT)")
rows = m.execute("SELECT sm.symbol, s.cap_tier, sm.mcap FROM stock_metrics sm JOIN stocks s ON s.symbol = sm.symbol WHERE sm.market = 'US' AND s.cap_tier IN ('large','mid','small')").fetchall()
bad = [r[0] for r in rows if r[2] is None or r[2] < FLOOR[r[1]] * 0.5]
print(len(bad), "to check", flush=True)
ok = 0
for sym in bad:
    try:
        cap = yf.Ticker(sym.split(":", 1)[1]).fast_info.get("marketCap")
    except Exception:  # noqa: BLE001
        cap = None
    if cap:
        m.execute("INSERT OR REPLACE INTO us_mcap_override (symbol, mcap_m, as_of) VALUES (?,?,?)", (sym, round(cap / 1e6, 1), dt.date.today().isoformat()))
        ok += 1
    time.sleep(0.3)
m.commit()
print("filled", ok, "of", len(bad))
