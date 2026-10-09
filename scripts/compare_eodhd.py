"""Score EODHD fundamentals against what Stocktale already holds, before paying for a longer subscription.

Run inside the app container (read-only on market.db; raw responses cached to /tmp/eodhd/ so a re-run costs no API calls):
    docker compose exec -T -e EODHD_API_KEY=... app python scripts/compare_eodhd.py [--n 20] [--us 5]

Reports, per field (revenue, net profit, CFO, finance cost, current assets/liabilities, inventory):
  coverage  - share of sampled stock-quarters where EODHD has a value
  oldest    - how far back the quarterly history reaches
  agree     - where both sides have a value, share within 3% of ours
  fills     - quarters where WE are empty and EODHD has a value (the reason to buy)
"""
import argparse
import json
import os
import random
import sqlite3
import sys
from datetime import date
from pathlib import Path

import requests

CACHE = Path("/tmp/eodhd")
CRORE = 1e7
# (our column, EODHD statement, EODHD key)
FIELDS = [
    ("revenue_cr", "Income_Statement", "totalRevenue"),
    ("net_profit_cr", "Income_Statement", "netIncome"),
    ("finance_cost_cr", "Income_Statement", "interestExpense"),
    ("cfo_cr", "Cash_Flow", "totalCashFromOperatingActivities"),
    ("current_assets_cr", "Balance_Sheet", "totalCurrentAssets"),
    ("current_liabilities_cr", "Balance_Sheet", "totalCurrentLiabilities"),
    ("inventory_cr", "Balance_Sheet", "inventory"),
]
KNOWN_IN = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "ITC", "LT", "HINDUNILVR", "TATAMOTORS", "SUNPHARMA",
            "TITAN", "ASIANPAINT", "BAJFINANCE", "MARUTI", "ULTRACEMCO"]


def fetch(eod_symbol: str, key: str):
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"{eod_symbol}.json"
    if f.exists():
        return json.loads(f.read_text())
    r = requests.get(f"https://eodhd.com/api/fundamentals/{eod_symbol}", params={"api_token": key, "fmt": "json"}, timeout=40)
    if r.status_code != 200:
        return {"_error": f"HTTP {r.status_code}: {r.text[:120]}"}
    body = r.json()
    f.write_text(json.dumps(body))
    return body


def num(v):
    try:
        return None if v in (None, "", "None", "NA") else float(v)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--us", type=int, default=5)
    a = ap.parse_args()
    key = os.environ.get("EODHD_API_KEY")
    if not key:
        sys.exit("Set EODHD_API_KEY (docker compose exec -e EODHD_API_KEY=...)")

    db = sqlite3.connect("file:/app/data/market.db?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rng = random.Random(7)
    india = [f"NSE:{t}" for t in KNOWN_IN[:a.n // 2]]
    pool = [r["symbol"] for r in db.execute(
        "SELECT symbol FROM stocks WHERE market='IN' AND cap_tier IN ('mid','small') AND symbol IN (SELECT symbol FROM quarterly_data GROUP BY symbol HAVING COUNT(*)>=8)")]
    india += rng.sample(pool, min(a.n - len(india), len(pool)))
    us_pool = [r["symbol"] for r in db.execute(
        "SELECT symbol FROM stocks WHERE market='US' AND symbol IN (SELECT symbol FROM quarterly_data GROUP BY symbol HAVING COUNT(*)>=8)")]
    us = rng.sample(us_pool, min(a.us, len(us_pool)))

    for label, syms in (("INDIA", india), ("US", us)):
        stats = {f[0]: dict(both=0, agree=0, theirs=0, ours_missing_theirs_present=0, total=0, oldest=None) for f in FIELDS}
        errors = []
        for sym in syms:
            ticker = sym.split(":", 1)[1]
            eod = f"{ticker}.NSE" if label == "INDIA" else f"{ticker}.US"
            body = fetch(eod, key)
            if "_error" in body or "Financials" not in body:
                errors.append((sym, body.get("_error", "no Financials block")))
                continue
            mine = {r["period_end_date"]: r for r in db.execute("SELECT * FROM quarterly_data WHERE symbol=?", (sym,))}
            for col, stmt, ek in FIELDS:
                q = (((body["Financials"].get(stmt) or {}).get("quarterly")) or {})
                scale = CRORE if label == "INDIA" else 1e6  # our US values are $ millions
                for d, row in q.items():
                    theirs = num(row.get(ek))
                    ours_row = next((v for k, v in mine.items() if abs((date.fromisoformat(k) - date.fromisoformat(d)).days) <= 6), None)
                    s = stats[col]
                    s["total"] += 1
                    if theirs is not None:
                        s["theirs"] += 1
                        s["oldest"] = d if s["oldest"] is None or d < s["oldest"] else s["oldest"]
                    mv = ours_row[col] if ours_row is not None else None
                    if theirs is not None and mv is not None:
                        s["both"] += 1
                        t = theirs / scale
                        if abs(t - mv) <= 0.03 * max(abs(mv), 1e-9) + 0.01:
                            s["agree"] += 1
                    elif theirs is not None and ours_row is not None and mv is None:
                        s["ours_missing_theirs_present"] += 1
        print(f"\n=== {label}: {len(syms) - len(errors)}/{len(syms)} symbols returned fundamentals ===")
        for sym, e in errors:
            print(f"  no data  {sym}: {e}")
        print(f"{'field':24}{'coverage':>10}{'oldest':>13}{'agree<=3%':>11}{'fills':>7}")
        for col, s in stats.items():
            cov = f"{s['theirs'] / s['total']:.0%}" if s["total"] else "-"
            agr = f"{s['agree']}/{s['both']}" if s["both"] else "-"
            print(f"{col:24}{cov:>10}{(s['oldest'] or '-'):>13}{agr:>11}{s['ours_missing_theirs_present']:>7}")


if __name__ == "__main__":
    main()
