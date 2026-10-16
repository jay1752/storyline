"""Benchmark and sector index closes from Yahoo into market.db.index_prices: the line a stock's own price is compared against.

India: Nifty 50 plus the sector indices that match our sector names; US: S&P 500 and Nasdaq. Full history on the first run, then only new days.
    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/fill_index_prices.py [--dry]"""
import argparse
import sqlite3
import sys
import time

from app.db import DATA_DIR, init_db

INDICES = {  # Yahoo symbol: what it is. Yahoo's other NSE sector indices (FMCG, Auto, Metal, Energy, Realty, Infra, Financial Services, Media,
    # PSU Bank) carry only the last two days, so they are left out: a sector line for those is computed from our own stocks instead.
    "^NSEI": "Nifty 50", "^NSEBANK": "Nifty Bank", "^CNXIT": "Nifty IT", "^CNXPHARMA": "Nifty Pharma",
    "^GSPC": "S&P 500", "^IXIC": "Nasdaq Composite",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    import yfinance as yf

    init_db()
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=300)
    ok = 0
    for sym, name in INDICES.items():
        last = m.execute("SELECT MAX(date) FROM index_prices WHERE symbol = ?", (sym,)).fetchone()[0]
        try:
            df = yf.Ticker(sym).history(period="max" if not last else "1mo", auto_adjust=False, timeout=20)
        except Exception as e:  # noqa: BLE001 - one index failing must not stop the rest
            print(f"{sym:12} {name:26} FAILED {type(e).__name__}", flush=True)
            time.sleep(3)
            continue
        rows = [(sym, d.strftime("%Y-%m-%d"), round(float(c), 2)) for d, c in df["Close"].items() if c == c and (not last or d.strftime("%Y-%m-%d") > last)]
        print(f"{sym:12} {name:26} {len(rows):6} new rows" + (f" ({rows[0][1]} to {rows[-1][1]})" if rows else ""), flush=True)
        if rows and not a.dry:
            m.executemany("INSERT OR REPLACE INTO index_prices (symbol, date, close) VALUES (?,?,?)", rows)
            m.commit()
            ok += 1
        time.sleep(1.5)
    print("DONE", ok, "indices updated")


if __name__ == "__main__":
    sys.exit(main())
