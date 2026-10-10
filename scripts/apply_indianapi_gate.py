"""Basis gate for IndianAPI data (run AFTER apply_indianapi_quarterly.py / apply_indianapi_annual.py).

Evidence (2026-10-10, 17 stocks compared with Screener): where IndianAPI's net profit agrees with the BSE filing, its normalised sales /
operating profit match Screener (IndianAPI is the better source); where net profit disagrees, IndianAPI is on another basis (usually
standalone vs consolidated) and the BSE row matches Screener. Net profit is the test because it has no definitional wiggle room.

Per stock, over quarters present in both (BSE original taken from quarterly_data_original):
  verified    >= 3 overlapping quarters and >= 80% agree within 2%  -> keep IndianAPI
  reverted    >= 3 overlapping and < 80%                             -> restore the BSE/NSE rows, delete IndianAPI-only quarters and annual rows
  unverified  < 3 overlapping quarters                               -> keep, flagged (nothing to test against)
Result is stored in market.db.ia_basis_status; the apply scripts skip 'reverted' stocks on re-runs.
    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/apply_indianapi_gate.py [--apply]"""
import argparse
import collections
import sqlite3
import sys

from app.db import DATA_DIR

P_L_COLS = ["revenue_cr", "operating_profit_cr", "opm_pct", "other_income_cr", "finance_cost_cr", "pbt_cr", "tax_pct", "net_profit_cr", "eps",
            "revenue_growth_pct", "quarter_label", "source"]
AGREE, MIN_OVERLAP = 0.80, 3


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--symbols", default="", help="comma-separated, e.g. NSE:TCS,NSE:INFY (default: every stock)")
    a = ap.parse_args()
    only = {("NSE:" + x if ":" not in x else x) for x in a.symbols.split(",") if x.strip()} if getattr(a, "symbols", "") else None
    ia = sqlite3.connect(f"file:{DATA_DIR}/indianapi.db?mode=ro", uri=True)
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=120)
    m.row_factory = sqlite3.Row
    if a.apply:
        m.execute("CREATE TABLE IF NOT EXISTS ia_basis_status (symbol TEXT PRIMARY KEY, status TEXT NOT NULL, np_agree INTEGER, n_overlap INTEGER, checked_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    orig = collections.defaultdict(dict)
    for r in m.execute("SELECT symbol, period_end_date, net_profit_cr FROM quarterly_data_original WHERE quarter_label LIKE 'Q ending%'"):
        orig[r["symbol"]][r["period_end_date"]] = r["net_profit_cr"]
    ianp = collections.defaultdict(dict)
    for sym, pe, v in ia.execute("SELECT symbol, period_end, value FROM ia_fact WHERE freq='Q' AND metric='Net Profit'"):
        ianp[sym][pe] = v
    tally, restored, deleted = collections.Counter(), 0, 0
    for (sym,) in ia.execute("SELECT symbol FROM ia_stock WHERE status IN ('loaded','unchecked') ORDER BY symbol").fetchall():
        if only is not None and sym not in only:
            continue
        n = ok = 0
        for pe, v in ianp[sym].items():
            b = orig[sym].get(pe)
            if b is None or v is None:
                continue
            n += 1
            ok += abs(v - b) <= 0.02 * max(abs(b), 1e-9) + 0.6
        status = "unverified" if n < MIN_OVERLAP else ("verified" if ok / n >= AGREE else "reverted")
        tally[status] += 1
        if not a.apply:
            continue
        m.execute("INSERT OR REPLACE INTO ia_basis_status (symbol, status, np_agree, n_overlap) VALUES (?,?,?,?)", (sym, status, ok, n))
        if status != "reverted":
            continue
        for r in m.execute("SELECT * FROM quarterly_data_original WHERE symbol=?", (sym,)).fetchall():
            cur = m.execute("SELECT source FROM quarterly_data WHERE symbol=? AND period_end_date=?", (sym, r["period_end_date"])).fetchone()
            if cur and cur["source"] == "indianapi":
                m.execute(f"UPDATE quarterly_data SET {', '.join(c + '=?' for c in P_L_COLS)} WHERE symbol=? AND period_end_date=?",
                          [r[c] for c in P_L_COLS] + [sym, r["period_end_date"]])
                restored += 1
        deleted += m.execute("DELETE FROM quarterly_data WHERE symbol=? AND source='indianapi'", (sym,)).rowcount  # quarters IndianAPI added
        m.execute("DELETE FROM annual_data WHERE symbol=? AND source='indianapi'", (sym,))
        if tally["reverted"] % 25 == 0:
            m.commit()
    if a.apply:
        m.commit()
    print("APPLIED" if a.apply else "DRY RUN", dict(tally), "| restored rows:", restored, "| IndianAPI-only quarters removed:", deleted)


if __name__ == "__main__":
    sys.exit(main())
