"""Make IndianAPI the source of record for India QUARTERLY results wherever it has them; BSE/NSE rows stay where it has none.

Decision (user, 2026-10-10): IndianAPI's numbers are normalised (Screener-style: comparable sales / operating profit, bonus-adjusted
EPS), BSE/NSE filing values are literal. Where IndianAPI has a quarter it replaces the P&L columns of quarterly_data and the row is
tagged source='indianapi'; originals are archived in quarterly_data_original first (rollback = copy them back).

Per stock: only status 'loaded' / 'unchecked' in indianapi.db (the 'held_out' ones disagreed with BSE revenue - identity doubtful).
Bank/NBFC layout: only net profit, PBT, tax % and EPS are taken (IndianAPI's bank 'Revenue'/'Financing Profit' are not our
InterestEarned/pre-provision profit, so those stay as filed).
Never touched: March ANNUAL rows (MoneyControl, quarter_label '... annual') - a quarter that would land on one is skipped, the same
key-collision that once overwrote March quarters with annual figures.

    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/apply_indianapi_quarterly.py [--apply] [--limit N]
Without --apply it only counts."""
import argparse
import collections
import sqlite3
import sys
from datetime import date, timedelta

from app.db import DATA_DIR
from app.ingestion.base import safe_growth_pct

STD = {"Sales": "revenue_cr", "Operating Profit": "operating_profit_cr", "OPM %": "opm_pct", "Other Income": "other_income_cr",
       "Interest": "finance_cost_cr", "Profit before tax": "pbt_cr", "Tax %": "tax_pct", "Net Profit": "net_profit_cr", "EPS in Rs": "eps"}
BANK = {"Profit before tax": "pbt_cr", "Tax %": "tax_pct", "Net Profit": "net_profit_cr", "EPS in Rs": "eps"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--symbols", default="", help="comma-separated, e.g. NSE:TCS,NSE:INFY (default: every stock)")
    a = ap.parse_args()
    only = {("NSE:" + x if ":" not in x else x) for x in a.symbols.split(",") if x.strip()} if getattr(a, "symbols", "") else None
    ia = sqlite3.connect(f"file:{DATA_DIR}/indianapi.db?mode=ro", uri=True)
    ia.row_factory = sqlite3.Row
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=120)
    m.row_factory = sqlite3.Row
    if a.apply:
        m.execute("CREATE TABLE IF NOT EXISTS quarterly_data_original AS SELECT *, CURRENT_TIMESTAMP AS archived_at FROM quarterly_data WHERE 0")
        m.commit()
    stocks = ia.execute("SELECT symbol, layout, status FROM ia_stock WHERE status IN ('loaded','unchecked') ORDER BY symbol").fetchall()
    known = {r[0] for r in m.execute("SELECT symbol FROM stocks WHERE market='IN'")}
    stocks = [s for s in stocks if s["symbol"] in known]
    try:  # a stock the basis gate reverted (IndianAPI on another basis than the filings) stays on BSE/NSE data
        reverted = {r[0] for r in m.execute("SELECT symbol FROM ia_basis_status WHERE status IN ('reverted','recent_ok')")}  # recent_ok: annual only, see apply_indianapi_recent.py
    except sqlite3.OperationalError:
        reverted = set()
    stocks = [s for s in stocks if s["symbol"] not in reverted]
    if only:
        stocks = [s for s in stocks if s["symbol"] in only]
    if a.limit:
        stocks = stocks[:a.limit]
    tally = collections.Counter()
    for i, s in enumerate(stocks, 1):
        sym, cols = s["symbol"], (BANK if s["layout"] == "bank" else STD)
        per: dict[str, dict] = collections.defaultdict(dict)
        for r in ia.execute("SELECT period_end, metric, value FROM ia_fact WHERE symbol=? AND freq='Q' AND statement='PL'", (sym,)):
            if r["metric"] in cols and r["value"] is not None:
                per[r["period_end"]][cols[r["metric"]]] = r["value"]
        existing = {r["period_end_date"]: r for r in m.execute("SELECT * FROM quarterly_data WHERE symbol=?", (sym,))}
        sales = {p: v["revenue_cr"] for p, v in per.items() if "revenue_cr" in v}
        for p, vals in sorted(per.items()):
            if not vals:
                continue
            ex = existing.get(p)
            if ex is not None and "annual" in (ex["quarter_label"] or "").lower():
                tally["skipped: key held by an annual row"] += 1
                continue
            if "revenue_cr" in vals:  # growth on the IndianAPI basis, from IndianAPI's own prior-year quarter
                prior = next((v for k, v in sales.items() if abs((date.fromisoformat(k) - (date.fromisoformat(p) - timedelta(days=365))).days) <= 10), None)
                g = safe_growth_pct(vals["revenue_cr"], prior) if prior else None
                if g is not None:
                    vals["revenue_growth_pct"] = g
            if ex is None:
                tally["insert (older/missing quarter)"] += 1
                if a.apply:
                    cs = ["symbol", "period_end_date", "quarter_label", "source", *vals]
                    m.execute(f"INSERT INTO quarterly_data ({', '.join(cs)}) VALUES ({', '.join('?' * len(cs))})", [sym, p, f"Q ending {p}", "indianapi", *vals.values()])
                continue
            changed = any(ex[c] is None or abs((ex[c] or 0) - v) > 1e-9 for c, v in vals.items())
            tally["update (value changed)" if changed else "update (identical)"] += 1
            if a.apply:
                if not m.execute("SELECT 1 FROM quarterly_data_original WHERE symbol=? AND period_end_date=?", (sym, p)).fetchone() and ex["source"] != "indianapi":
                    m.execute("INSERT INTO quarterly_data_original SELECT *, CURRENT_TIMESTAMP FROM quarterly_data WHERE symbol=? AND period_end_date=?", (sym, p))
                sets = ", ".join(f"{c}=?" for c in vals)
                m.execute(f"UPDATE quarterly_data SET {sets}, source='indianapi' WHERE symbol=? AND period_end_date=?", [*vals.values(), sym, p])
        if a.apply and i % 100 == 0:
            m.commit()
            print(f"{i}/{len(stocks)}", flush=True)
    if a.apply:
        m.commit()
    print(("APPLIED" if a.apply else "DRY RUN"), len(stocks), "stocks")
    for k, v in tally.most_common():
        print(f"  {k:40}{v:8}")


if __name__ == "__main__":
    sys.exit(main())
