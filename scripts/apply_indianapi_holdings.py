"""Apply IndianAPI's quarterly shareholding history (promoter / FII / DII %) to quarterly_data - IndianAPI wherever it has the quarter,
existing (BSE) values otherwise. Skips stocks the basis gate reverted. Prior values of every changed row go to holdings_original
first (rollback). Promoter PLEDGE is not touched (IndianAPI has none). Where both sides had a value: promoters agreed 99%, FII/DII ~88%
(different investor-class definitions) - the mixed series is therefore close but not identical across the switch-over quarter.
    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/apply_indianapi_holdings.py [--apply]"""
import argparse
import collections
import sqlite3
import sys

from app.db import DATA_DIR

COL = {"Promoters": "promoter_pct", "FIIs": "fii_pct", "DIIs": "dii_pct"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--symbols", default="", help="comma-separated, e.g. NSE:TCS,NSE:INFY (default: every stock)")
    ap.add_argument("--period", default="", help="only this quarter end (YYYY-MM-DD)")
    a = ap.parse_args()
    only = {("NSE:" + x if ":" not in x else x) for x in a.symbols.split(",") if x.strip()} if getattr(a, "symbols", "") else None
    ia = sqlite3.connect(f"file:{DATA_DIR}/indianapi.db?mode=ro", uri=True)
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=120)
    if a.apply:
        m.execute("CREATE TABLE IF NOT EXISTS holdings_original (symbol TEXT, period_end_date TEXT, promoter_pct REAL, fii_pct REAL, dii_pct REAL, archived_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (symbol, period_end_date))")
    ok = {r[0] for r in ia.execute("SELECT symbol FROM ia_stock WHERE status IN ('loaded','unchecked')")}
    try:
        ok -= {r[0] for r in m.execute("SELECT symbol FROM ia_basis_status WHERE status='reverted'")}
    except sqlite3.OperationalError:
        pass
    per = collections.defaultdict(dict)
    for sym, pe, cat, v in ia.execute("SELECT symbol, period_end, category, value FROM ia_holding WHERE freq='Q' AND category IN ('Promoters','FIIs','DIIs')"):
        if sym in ok and (only is None or sym in only) and (not a.period or pe == a.period):
            per[(sym, pe)][COL[cat]] = v
    tally = collections.Counter()
    for (sym, pe), vals in per.items():
        row = m.execute("SELECT promoter_pct, fii_pct, dii_pct FROM quarterly_data WHERE symbol=? AND period_end_date=? AND quarter_label NOT LIKE '%annual%'", (sym, pe)).fetchone()
        if row is None:
            tally["no quarter row (skipped)"] += 1
            continue
        cur = dict(zip(("promoter_pct", "fii_pct", "dii_pct"), row))
        change = {k: v for k, v in vals.items() if cur[k] is None or abs(cur[k] - v) > 1e-9}
        if not change:
            tally["already identical"] += 1
            continue
        tally["filled (was empty)"] += sum(cur[k] is None for k in change)
        tally["replaced"] += sum(cur[k] is not None for k in change)
        if a.apply:
            m.execute("INSERT OR IGNORE INTO holdings_original (symbol, period_end_date, promoter_pct, fii_pct, dii_pct) VALUES (?,?,?,?,?)", (sym, pe, *cur.values()))
            m.execute(f"UPDATE quarterly_data SET {', '.join(k + '=?' for k in change)} WHERE symbol=? AND period_end_date=?", [*change.values(), sym, pe])
        if a.apply and sum(tally.values()) % 5000 == 0:
            m.commit()
    if a.apply:
        m.commit()
    print("APPLIED" if a.apply else "DRY RUN", dict(tally))


if __name__ == "__main__":
    sys.exit(main())
