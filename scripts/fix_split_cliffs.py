"""Find stocks whose stored price history has a split-sized cliff and re-pull their full history from Yahoo (which restates every old
price to the current share count). Tiingo-sourced US history and older NSE/BSE bhavcopy rows are raw, so a 10-for-1 split shows as a -90%
day and wrecks 1Y/5Y returns, the 52-week range and the P/E range (AVGO 2024-07-15: 1,700 -> 171).

A "cliff" on a raw-source row (Tiingo, 5paisa, exchange bhavcopy; Yahoo history is already split-adjusted) is a one-day close ratio outside 0.6 .. 1.7 whose size is within 8% of a classic split factor (1/2, 1/3, 1/4, 1/5, 1/10, 1/20,
and the reverse 2, 3, 4, 5, 10). After the re-pull the stock is re-measured and reported as FIXED (cliff gone), REAL (cliff stays: the
move is genuine, Yahoo agrees) or FAILED (Yahoo had nothing). Idempotent. Only touches stocks in stock_metrics (screened stocks).
    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/fix_split_cliffs.py [--dry] [--limit N] [--symbols A,B]"""
import argparse
import collections
import sqlite3
import sys
import time

from app.db import DATA_DIR
from app.price_fetch import refresh_history

# Only a cliff on a row that came from a RAW source can be an unadjusted split: Yahoo rows are already split-adjusted, so a big move there is real.
RAW_SOURCES = ("tiingo", "5paisa", "nse_bhavcopy", "bse_bhavcopy")
FACTORS = (0.5, 1 / 3, 0.25, 0.2, 0.1, 0.05, 2, 3, 4, 5, 10)


def split_like(x: float) -> bool:
    return (x < 0.6 or x > 1.7) and any(abs(x / f - 1) < 0.08 for f in FACTORS)


def cliffs(conn, symbols=None):
    q = """SELECT symbol, date, ratio FROM (
             SELECT symbol, date, source, close * 1.0 / LAG(close) OVER (PARTITION BY symbol ORDER BY date) ratio
             FROM price_history WHERE close > 0 {where}) WHERE (ratio < 0.6 OR ratio > 1.7) AND source IN ({raw})"""
    where = ""
    args: tuple = ()
    if symbols:
        where = "AND symbol IN (%s)" % ",".join("?" * len(symbols))
        args = tuple(symbols)
    else:
        where = "AND symbol IN (SELECT symbol FROM stock_metrics)"
    out = collections.defaultdict(list)
    raw = ",".join("'%s'" % x for x in RAW_SOURCES)
    for sym, d, r in conn.execute(q.format(where=where, raw=raw), args):
        if split_like(r):
            out[sym].append((d, round(r, 3)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--symbols", default="")
    a = ap.parse_args()
    conn = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=300)
    only = [x.strip() for x in a.symbols.split(",") if x.strip()] or None
    t0 = time.time()
    found = cliffs(conn, only)
    print(f"{len(found)} stocks with a split-sized cliff ({int(time.time() - t0)}s)", flush=True)
    if a.dry:
        for s, l in list(found.items())[:30]:
            print(s, l[:3])
        return
    todo = sorted(found)[:a.limit] if a.limit else sorted(found)
    res = collections.Counter()
    for i, sym in enumerate(todo, 1):
        try:
            n = refresh_history(conn, sym)
        except Exception as e:  # noqa: BLE001 - one bad ticker must not stop the run
            print("error", sym, type(e).__name__, flush=True)
            res["FAILED"] += 1
            continue
        conn.commit()
        if not n:
            res["FAILED"] += 1
            continue
        after = cliffs(conn, [sym])
        if sym in after:  # Yahoo does not reach back far enough: the raw rows older than the last cliff are on another basis, so they go
            last = max(d for d, _ in after[sym])
            conn.execute("DELETE FROM price_history WHERE symbol = ? AND date < ? AND source IN (%s)" % ",".join("'%s'" % x for x in RAW_SOURCES), (sym, last))
            conn.commit()
            res["TRIMMED"] += 1
        else:
            res["FIXED"] += 1
        if i % 50 == 0:
            print(f"{i}/{len(todo)} {dict(res)} {int(time.time() - t0)}s", flush=True)
        time.sleep(0.4)
    print("DONE", dict(res), f"{int(time.time() - t0)}s", flush=True)


if __name__ == "__main__":
    sys.exit(main())
