"""Re-pull the full Yahoo history (raw close, split-adjusted only) for the given symbols or for every symbol written after a row id.
    python scripts/repull_yahoo.py --symbols A,B   |   --after-rowid N [--min-rows 2000]"""
import argparse
import sqlite3
import sys
import time

from app.db import DATA_DIR
from app.price_fetch import refresh_history

ap = argparse.ArgumentParser()
ap.add_argument("--symbols", default="")
ap.add_argument("--after-rowid", type=int, default=0)
ap.add_argument("--min-rows", type=int, default=2000)
a = ap.parse_args()
c = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=300)
if a.after_rowid:
    syms = [r[0] for r in c.execute("SELECT symbol FROM price_history WHERE rowid > ? GROUP BY symbol HAVING COUNT(*) > ? ORDER BY symbol", (a.after_rowid, a.min_rows))]
else:
    syms = [x.strip() for x in a.symbols.split(",") if x.strip()]
print(len(syms), "symbols", flush=True)
ok = 0
for i, s in enumerate(syms, 1):
    try:
        ok += 1 if refresh_history(c, s) else 0
    except Exception as e:  # noqa: BLE001
        print("error", s, type(e).__name__, flush=True)
    time.sleep(0.4)
    if i % 50 == 0:
        print(i, ok, flush=True)
print("DONE", ok, "of", len(syms), flush=True)
