"""NSE bulk and block deals into market.db.deals.

NSE publishes today's bulk and block deals as two CSV files (the history endpoint needs a browser session and answers 503 to scripts), so this keeps
what each run sees: run it once a day after the market closes and the history builds up. Rows are matched to our stocks by NSE symbol; deals in
stocks we do not carry are skipped.
    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/fill_deals_nse.py [--dry]"""
import argparse
import csv
import io
import sqlite3
import sys
from datetime import datetime

import requests

from app.db import DATA_DIR, init_db

H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
FILES = {"bulk": "https://nsearchives.nseindia.com/content/equities/bulk.csv", "block": "https://nsearchives.nseindia.com/content/equities/block.csv"}


def num(x):
    try:
        return float(str(x).replace(",", ""))
    except ValueError:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    init_db()
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=300)
    ours = {r[0] for r in m.execute("SELECT symbol FROM stocks WHERE exchange = 'NSE' OR symbol LIKE 'NSE:%'")}
    for kind, url in FILES.items():
        r = requests.get(url, headers=H, timeout=30)
        r.raise_for_status()
        rows, skipped, days = [], 0, set()
        for rec in csv.DictReader(io.StringIO(r.text.lstrip("﻿"))):
            rec = {k.strip(): (v or "").strip() for k, v in rec.items() if k}
            sym = "NSE:" + rec.get("Symbol", "")
            if sym not in ours:
                skipped += 1
                continue
            day = datetime.strptime(rec["Date"], "%d-%b-%Y").strftime("%Y-%m-%d")
            days.add(day)
            rows.append((day, sym, kind, rec["Client Name"], rec["Buy/Sell"].upper(), num(rec["Quantity Traded"]), num(rec["Trade Price / Wght. Avg. Price"]),
                         rec.get("Remarks") if rec.get("Remarks") not in ("", "-") else None))
        print(f"{kind:5} {url.rsplit('/', 1)[1]}: {len(rows)} deals in stocks we carry on {sorted(days)}, {skipped} skipped (not ours)")
        if rows and not a.dry:
            before = m.total_changes
            m.executemany("INSERT OR IGNORE INTO deals (deal_date, symbol, kind, client, side, qty, price, remarks) VALUES (?,?,?,?,?,?,?,?)", rows)
            m.commit()
            print(f"      stored {m.total_changes - before} new rows")
    print("deals on file:", m.execute("SELECT COUNT(*), MIN(deal_date), MAX(deal_date) FROM deals").fetchone())


if __name__ == "__main__":
    sys.exit(main())
