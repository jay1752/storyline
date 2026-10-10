"""Backfill NSE delivery % history: python scripts/backfill_delivery.py [--days 400]
Run inside the app container (PYTHONPATH=/app). Skips days already stored, so it resumes."""
import argparse
from datetime import date, timedelta

from app.db import get_market_conn, init_db
from app.ingestion import nse_delivery


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400)
    a = ap.parse_args()
    init_db()
    with get_market_conn() as m:
        known = {r[0] for r in m.execute("SELECT symbol FROM stocks WHERE market='IN'")}
        have = {r[0] for r in m.execute("SELECT DISTINCT date FROM delivery_data")}
        today = date.today()
        todo = [d for d in (today - timedelta(days=i) for i in range(a.days, -1, -1)) if d.weekday() < 5 and d.isoformat() not in have]
        print(f"{len(todo)} days to fetch", flush=True)
        done = 0
        for i in range(0, len(todo), 20):
            done += nse_delivery.ingest_days(m, todo[i:i + 20], known)
            print(f"{min(i + 20, len(todo))}/{len(todo)} days, {done} rows", flush=True)
        print("total rows", m.execute("SELECT COUNT(*) FROM delivery_data").fetchone()[0])


if __name__ == "__main__":
    main()
