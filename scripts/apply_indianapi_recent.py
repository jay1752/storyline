"""Second chance for IndianAPI stocks the basis gate turned away ('reverted' by apply_indianapi_gate.py, or 'held_out' by the identity check in
load_indianapi.py). Both tests look at every overlapping quarter, so one restatement or demerger years ago (Vedanta) sinks a stock whose
current numbers are fine. Here only the latest quarters count: a stock qualifies as 'recent_ok' when, over its last 4 quarters present in
both the BSE filings and IndianAPI,
  - the latest two net profits agree within 2% (+Rs 0.6 cr), and
  - revenue agrees within 1% in at least 3 of the 4. Banks and lenders (whose IndianAPI 'Sales' is a different line, and whose IndianAPI profit is group
    level) are judged on their last-year profit instead: within 10% of the filings.
recent_ok stocks get IndianAPI ANNUAL rows for their latest 4 fiscal years only (cash flow, balance sheet, dividends, ROCE inputs); their
quarterly rows stay as filed (BSE/NSE). Status lives in market.db.ia_basis_status. Re-runnable; --dry shows the list.
    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/apply_indianapi_recent.py [--dry]
then:  load_indianapi.py --symbols <printed list>  and  apply_indianapi_annual.py --symbols <printed list>"""
import argparse
import calendar
import collections
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

from app.db import DATA_DIR

RAW = DATA_DIR / "indianapi" / "raw" / "historical_stats"
ABBR = {mn: i for i, mn in enumerate(calendar.month_abbr) if mn}


def month_end(label: str):
    try:
        mon, yr = label.split()
        return date(int(yr), ABBR[mon], calendar.monthrange(int(yr), ABBR[mon])[1]).isoformat()
    except (ValueError, KeyError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=120)
    m.row_factory = sqlite3.Row
    ia = sqlite3.connect(f"file:{DATA_DIR}/indianapi.db?mode=ro", uri=True)
    cur = {r["symbol"]: r["status"] for r in m.execute("SELECT symbol, status FROM ia_basis_status")}
    cand = {s for s, v in cur.items() if v in ("reverted", "recent_ok")} | {r[0] for r in ia.execute("SELECT symbol FROM ia_stock WHERE status='held_out'")}
    bank = {r[0] for r in m.execute("SELECT symbol FROM stock_metrics WHERE bank=1")}
    orig: dict[str, dict] = collections.defaultdict(dict)
    for r in m.execute("SELECT symbol, period_end_date, net_profit_cr, revenue_cr FROM quarterly_data_original WHERE quarter_label LIKE 'Q ending%'"):
        orig[r["symbol"]][r["period_end_date"]] = (r["net_profit_cr"], r["revenue_cr"])
    for r in m.execute("SELECT symbol, period_end_date, net_profit_cr, revenue_cr FROM quarterly_data WHERE source IN ('bse','nse') AND symbol LIKE 'NSE:%'"):
        orig[r["symbol"]].setdefault(r["period_end_date"], (r["net_profit_cr"], r["revenue_cr"]))
    passed, tally = [], collections.Counter()
    for sym in sorted(cand):
        p = RAW / f"{sym.split(':', 1)[1]}.json"
        if not p.exists():
            tally["no raw file"] += 1
            continue
        q = json.loads(p.read_text()).get("quarter_results") or {}
        got: dict[str, dict] = collections.defaultdict(dict)
        for key, field in (("Sales", "rv"), ("Revenue", "rv"), ("Net Profit", "np")):
            for lab, v in (q.get(key) or {}).items():
                d = month_end(lab)
                if d and v is not None:
                    got[d][field] = v
        dates = sorted(d for d in got if d in orig[sym])[-4:]
        if len(dates) < 3:
            tally["fewer than 3 recent quarters to compare"] += 1
            continue
        tol = 0.04 if sym in bank else 0.02  # a bank's IndianAPI profit is group level, its BSE row often the bank alone: a few percent apart is the same business
        np_ok = [("np" in got[d] and orig[sym][d][0] is not None and abs(got[d]["np"] - orig[sym][d][0]) <= tol * abs(orig[sym][d][0]) + 0.6) for d in dates]
        rv_ok = sum(1 for d in dates if got[d].get("rv") and orig[sym][d][1] and abs(got[d]["rv"] - orig[sym][d][1]) <= 0.01 * abs(orig[sym][d][1]) + 1)
        if sym in bank:  # group vs bank-alone profit: judged on the whole recent year (within 10%), not quarter by quarter
            sia = sum(got[d]["np"] for d in dates if "np" in got[d] and orig[sym][d][0] is not None)
            sbs = sum(orig[sym][d][0] for d in dates if "np" in got[d] and orig[sym][d][0] is not None)
            same = sbs > 0 and abs(sia / sbs - 1) <= 0.10
        else:
            same = all(np_ok[-2:]) and rv_ok >= min(3, len(dates))
        if same:
            passed.append(sym)
            tally["recent_ok"] += 1
        else:
            tally["still held back"] += 1
    print(dict(tally))
    print(",".join(passed))
    if a.dry:
        return
    for sym in passed:
        m.execute("INSERT OR REPLACE INTO ia_basis_status (symbol, status, np_agree, n_overlap) VALUES (?,?,?,?)", (sym, "recent_ok", 2, 2))
    m.commit()
    print("ia_basis_status updated for", len(passed))


if __name__ == "__main__":
    sys.exit(main())
