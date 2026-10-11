"""Fill the holes in US stocks that DO have SEC quarters: big names whose filings tag operating income, debt, current assets or cash flow in a way
our SEC parser does not read (Exxon, PACCAR, D.R. Horton, Lennar...). For each stock with fewer than 7 checkable rules, the matching quarters
from Yahoo (USD millions) fill ONLY the empty columns of the existing rows (never overwriting an SEC figure), plus ROCE / ROE / debt-to-equity on
the latest quarter where the SEC row had none. Rows written this way keep source='sec_edgar'; the gap fill is recorded in the run log only.
    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/fill_us_gaps_yahoo.py [--limit N] [--symbols A,B]"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import date

from app.db import DATA_DIR
from app.yahoo_fundamentals import build_rows

FILL = {"op": "operating_profit_cr", "pbt": "pbt_cr", "tax_pct": "tax_pct", "fin": "finance_cost_cr", "ca": "current_assets_cr",
        "cl": "current_liabilities_cr", "inv": "inventory_cr", "cfo": "cfo_cr", "cash": "cash_cr", "debt": "total_debt_cr"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--symbols", default="")
    a = ap.parse_args()
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=300)
    m.row_factory = sqlite3.Row
    want = {x.strip() for x in a.symbols.split(",") if x.strip()}
    cand = []
    for r in m.execute("SELECT symbol, raws, mcap FROM stock_metrics WHERE market='US' AND bank=0 AND mcap >= 150"):
        if want and r["symbol"] not in want:
            continue
        if want or len(json.loads(r["raws"] or "{}")) < 7:
            cand.append(r["symbol"])
    if a.limit:
        cand = cand[:a.limit]
    print(len(cand), "stocks to check", flush=True)
    filled = touched = failed = 0
    for i, sym in enumerate(cand, 1):
        px = m.execute("SELECT close FROM price_history WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,)).fetchone()
        try:
            res, err = build_rows(sym, px["close"] if px else None)
        except Exception:  # noqa: BLE001
            res, err = None, "error"
        if res is None:
            failed += 1
            continue
        mine = {r["period_end_date"]: dict(r) for r in m.execute("SELECT * FROM quarterly_data WHERE symbol=? AND quarter_label NOT LIKE '%annual%' ORDER BY period_end_date", (sym,))}
        ends = sorted(mine)
        got = False
        for r in res["rows"]:
            d = date.fromisoformat(r["end"])
            match = next((e for e in ends if abs((date.fromisoformat(e) - d).days) <= 6), None)
            if not match:
                continue
            row = mine[match]
            sets = {col: r[k] for k, col in FILL.items() if row.get(col) is None and r.get(k) is not None}
            if row.get("opm_pct") is None and r.get("op") is not None and (row.get("revenue_cr") or r.get("rev")):
                rev = row.get("revenue_cr") or r["rev"]
                sets["opm_pct"] = round(r["op"] / rev * 100, 2)
            if match == ends[-1]:  # latest quarter: the trailing ratios
                for key, col in (("roce", "roce_pct"), ("roe", "roe_pct"), ("de", "debt_equity")):
                    if row.get(col) is None and r.get(key) is not None:
                        sets[col] = r[key]
                if row.get("total_debt_cr") is None and "total_debt_cr" not in sets and r.get("debt") is not None:
                    sets["total_debt_cr"] = r["debt"]
            if sets:
                m.execute(f"UPDATE quarterly_data SET {', '.join(c + '=?' for c in sets)} WHERE symbol=? AND period_end_date=?", [*sets.values(), sym, match])
                filled += len(sets)
                got = True
        touched += got
        if i % 25 == 0:
            m.commit()
            print(f"{i}/{len(cand)} stocks touched={touched} cells={filled} failed={failed}", flush=True)
        time.sleep(0.3)
    m.commit()
    print("DONE", touched, "stocks,", filled, "cells,", failed, "failed", flush=True)


if __name__ == "__main__":
    sys.exit(main())
