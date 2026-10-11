"""Quarterly fundamentals for US-listed foreign companies (ADRs / foreign private issuers) from Yahoo's statements.

These companies file annual reports (20-F / 40-F) and half-yearly 6-Ks, not 10-Qs, so the SEC feed has no quarters for them (ASML, Shell,
Toyota, HSBC, Novartis...). Yahoo carries 5-6 quarters of income statement, balance sheet and cash flow, in the company's REPORTING currency.
Everything is converted to USD at TODAY's rate (one rate for every quarter, so growth rates are the company's own, not a currency effect).
Per-share figures are per ADR: the ADR-equivalent share count is market cap / ADR price, so EPS = net profit / that count and P/E agrees with the
price shown. Rows are written with source='yahoo_adr' (USD millions, like the SEC rows) and never touch a stock that has SEC quarters.

    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/fill_adr_yahoo.py [--symbols NASDAQ:ASML,...] [--limit N] [--dry]"""
import argparse
import sqlite3
import sys
import time

from app.db import DATA_DIR
from app.us_securities import is_derivative
from app.yahoo_fundamentals import build_rows

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--stale", action="store_true", help="instead: stocks whose SEC quarters stopped over 400 days ago (re-organised filers, ADRs that left 10-Q)")
    a = ap.parse_args()
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=300)
    m.row_factory = sqlite3.Row
    want = {x.strip() for x in a.symbols.split(",") if x.strip()}
    tickers = {r[0].split(":", 1)[1]: r[1] for r in m.execute("SELECT symbol, name FROM stocks WHERE market='US'")}
    cand = []
    if a.stale:
        sql = """SELECT s.symbol, s.cap_tier, s.sector FROM stocks s WHERE s.market='US'
                 AND (SELECT MAX(period_end_date) FROM quarterly_data q WHERE q.symbol=s.symbol AND q.quarter_label NOT LIKE '%annual%') < date('now','-400 day')
                 AND EXISTS (SELECT 1 FROM price_history p WHERE p.symbol=s.symbol AND p.date >= date('now','-20 day'))"""
    else:
        sql = """SELECT s.symbol, s.cap_tier, s.sector FROM stocks s WHERE s.market='US'
                 AND NOT EXISTS (SELECT 1 FROM quarterly_data q WHERE q.symbol=s.symbol AND q.quarter_label NOT LIKE '%annual%')
                 AND EXISTS (SELECT 1 FROM price_history p WHERE p.symbol=s.symbol AND p.date >= date('now','-20 day'))"""
    for r in m.execute(sql):
        sym = r["symbol"]
        if want and sym not in want:
            continue
        if not want and (r["cap_tier"] not in ("large", "mid", "small") or is_derivative(sym.split(":", 1)[1], tickers)):
            continue
        cand.append(sym)
    if a.limit:
        cand = cand[:a.limit]
    print(len(cand), "stocks to try", flush=True)
    ok = fail = 0
    why: dict[str, int] = {}
    for i, sym in enumerate(cand, 1):
        px = m.execute("SELECT close FROM price_history WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,)).fetchone()
        try:
            res, err = build_rows(sym, px["close"] if px else None)
        except Exception as e:  # noqa: BLE001 - one odd ticker must not stop the run
            res, err = None, type(e).__name__
        if res is None:
            fail += 1
            why[err] = why.get(err, 0) + 1
            continue
        if a.dry:
            print(sym, res["currency"], round(res["fx"], 4), len(res["rows"]), res["rows"][-1])
            ok += 1
            continue
        sh = res["adr_shares"]
        for k, r in enumerate(res["rows"]):
            last = k == len(res["rows"]) - 1
            eps = round(r["ni"] * 1e6 / sh, 2) if sh and r["ni"] is not None else None
            opm = round(r["op"] / r["rev"] * 100, 2) if r["op"] is not None and r["rev"] else None
            bvps = round(r["equity"] * 1e6 / sh, 2) if sh and r["equity"] is not None and last else None
            netdebt = r["debt"] - r["cash"] if r["debt"] is not None and r["cash"] is not None else None
            m.execute("""INSERT OR IGNORE INTO quarterly_data (symbol, period_end_date, quarter_label, source, revenue_cr, operating_profit_cr, opm_pct, net_profit_cr,
                         eps, revenue_growth_pct, total_debt_cr, net_debt_cr, cash_cr, roe_pct, roce_pct, debt_equity, pbt_cr, tax_pct, book_value_per_share,
                         shares_outstanding, finance_cost_cr, current_assets_cr, current_liabilities_cr, inventory_cr, cfo_cr)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (sym, r["end"], f"Q ending {r['end']}", "yahoo_adr", r["rev"], r["op"], opm, r["ni"], eps, r["growth"], r["debt"], netdebt, r["cash"],
                       r.get("roe") if last else None, r.get("roce") if last else None, r.get("de") if last else None, r["pbt"], r["tax_pct"], bvps,
                       sh, r["fin"], r["ca"], r["cl"], r["inv"], r["cfo"]))
        m.commit()
        ok += 1
        if i % 25 == 0:
            print(f"{i}/{len(cand)} ok={ok} failed={fail} {why}", flush=True)
        time.sleep(0.3)
    print("DONE", ok, "filled,", fail, "failed", why, flush=True)


if __name__ == "__main__":
    sys.exit(main())
