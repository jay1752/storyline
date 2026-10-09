"""India EPS where the filing didn't report one: net profit / share count implied by the neighbouring quarters'
reported EPS. Only when >=2 of the 6 nearest reported quarters imply the same share count (within 3%), so a split,
bonus or buyback in between blocks the fill. Idempotent; re-run after any BSE/MoneyControl re-ingest (their upserts
write eps back to NULL).    python3 scripts/fill_india_eps.py [db_path]"""
import sqlite3
import statistics
import sys
from itertools import groupby

c = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else "data/market.db", timeout=60)
c.row_factory = sqlite3.Row
rows = c.execute("""SELECT q.symbol, q.period_end_date, q.net_profit_cr, q.eps FROM quarterly_data q JOIN stocks s USING(symbol)
                    WHERE s.market='IN' AND q.quarter_label NOT LIKE '%nnual%' ORDER BY q.symbol, q.period_end_date""").fetchall()
updates = []
for sym, g in groupby(rows, key=lambda r: r["symbol"]):
    g = list(g)
    implied = [(r["net_profit_cr"] * 1e7 / r["eps"]) if (r["eps"] and r["net_profit_cr"] and abs(r["eps"]) > 1e-6) else None for r in g]
    for i, r in enumerate(g):
        if r["eps"] is not None or r["net_profit_cr"] is None:
            continue
        near = [implied[j] for j in range(max(0, i - 3), min(len(g), i + 4)) if j != i and implied[j] and implied[j] > 0]
        if len(near) < 2:
            continue
        med = statistics.median(near)
        if sum(1 for v in near if abs(v / med - 1) < 0.03) >= 2:
            updates.append((round(r["net_profit_cr"] * 1e7 / med, 2), sym, r["period_end_date"]))
c.executemany("UPDATE quarterly_data SET eps=? WHERE symbol=? AND period_end_date=? AND eps IS NULL", updates)
c.commit()
print("India EPS filled:", len(updates))
