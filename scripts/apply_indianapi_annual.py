"""Fill market.db.annual_data from the staged IndianAPI annual statements (12 years: P&L, balance sheet, cash flow, ratios).

Same stock selection as the quarterly apply (status loaded/unchecked). Bank/NBFC layout: only net profit, PBT, tax, EPS, other income,
balance-sheet totals and the whole cash-flow statement (IndianAPI's bank 'Revenue'/'Financing Profit' are not our revenue/operating profit).
Idempotent (INSERT OR REPLACE per stock-year). Run: docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/apply_indianapi_annual.py"""
import collections
import sqlite3
import sys

from app.db import DATA_DIR, init_db

STD = {
    "Sales": "revenue_cr", "Expenses": "expenses_cr", "Operating Profit": "operating_profit_cr", "OPM %": "opm_pct", "Other Income": "other_income_cr",
    "Interest": "finance_cost_cr", "Depreciation": "depreciation_cr", "Profit before tax": "pbt_cr", "Tax %": "tax_pct", "Net Profit": "net_profit_cr",
    "EPS in Rs": "eps", "Dividend Payout %": "dividend_payout_pct",
    "Equity Capital": "equity_capital_cr", "Reserves": "reserves_cr", "Borrowings": "borrowings_cr", "Other Liabilities": "other_liabilities_cr",
    "Total Liabilities": "total_liabilities_cr", "Fixed Assets": "fixed_assets_cr", "CWIP": "cwip_cr", "Investments": "investments_cr",
    "Other Assets": "other_assets_cr", "Total Assets": "total_assets_cr",
    "Cash from Operating Activity": "cfo_cr", "Cash from Investing Activity": "cfi_cr", "Cash from Financing Activity": "cff_cr",
    "Net Cash Flow": "net_cash_flow_cr", "Free Cash Flow": "fcf_cr", "CFO/OP": "cfo_to_op",
    "Debtor Days": "debtor_days", "Inventory Days": "inventory_days", "Days Payable": "days_payable", "Cash Conversion Cycle": "cash_conversion_cycle",
    "Working Capital Days": "working_capital_days", "ROCE %": "roce_pct",
}
BANK_OK = {"Profit before tax", "Tax %", "Net Profit", "EPS in Rs", "Other Income", "Equity Capital", "Reserves", "Borrowing", "Total Liabilities", "Total Assets",
           "Fixed Assets", "Investments", "Other Assets", "Cash from Operating Activity", "Cash from Investing Activity", "Cash from Financing Activity",
           "Net Cash Flow", "Free Cash Flow", "ROCE %"}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="", help="comma-separated, e.g. NSE:TCS,NSE:INFY (default: every stock)")
    a = ap.parse_args()
    only = {("NSE:" + x if ":" not in x else x) for x in a.symbols.split(",") if x.strip()} or None
    init_db()  # creates annual_data on databases that predate it
    ia = sqlite3.connect(f"file:{DATA_DIR}/indianapi.db?mode=ro", uri=True)
    ia.row_factory = sqlite3.Row
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=120)
    known = {r[0] for r in m.execute("SELECT symbol FROM stocks WHERE market='IN'")}
    try:
        reverted = {r[0] for r in m.execute("SELECT symbol FROM ia_basis_status WHERE status='reverted'")}
    except sqlite3.OperationalError:
        reverted = set()
    tally = collections.Counter()
    if only:  # a targeted refresh: replace just these stocks' rows
        m.executemany("DELETE FROM annual_data WHERE source = 'indianapi' AND symbol = ?", [(s,) for s in only])
    else:
        m.execute("DELETE FROM annual_data WHERE source = 'indianapi'")  # rebuild cleanly so rows dropped by a rule change don't linger
    for s in ia.execute("SELECT symbol, layout FROM ia_stock WHERE status IN ('loaded','unchecked') ORDER BY symbol").fetchall():
        sym = s["symbol"]
        if sym not in known or sym in reverted or (only and sym not in only):
            continue
        bank = s["layout"] == "bank"
        years: dict[str, dict] = collections.defaultdict(dict)
        for r in ia.execute("SELECT period_end, metric, value FROM ia_fact WHERE symbol=? AND freq='A'", (sym,)):
            metric = r["metric"]
            if bank and metric not in BANK_OK:
                continue
            col = STD.get(metric) or ("borrowings_cr" if metric == "Borrowing" else None)
            if col and r["value"] is not None:
                years[r["period_end"]][col] = r["value"]
        for pe, vals in years.items():
            if not any(k in vals for k in ("revenue_cr", "net_profit_cr", "cfo_cr")):
                continue  # a balance-sheet-only column is a half-year snapshot (e.g. Sep), not a fiscal year
            if vals.get("revenue_cr") and vals.get("operating_profit_cr") is not None:
                vals["opm_pct"] = round(vals["operating_profit_cr"] * 100.0 / vals["revenue_cr"], 2)  # IndianAPI's own OPM is whole-number rounded
            cols = ["symbol", "period_end_date", "source", *vals]
            m.execute(f"INSERT OR REPLACE INTO annual_data ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [sym, pe, "indianapi", *vals.values()])
            tally["year rows"] += 1
            tally["with CFO"] += "cfo_cr" in vals
        tally["stocks"] += 1
        if tally["stocks"] % 500 == 0:
            m.commit()
    m.commit()
    print(dict(tally))


if __name__ == "__main__":
    sys.exit(main())
