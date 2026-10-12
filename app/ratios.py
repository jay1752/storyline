"""Yearly ratios for stocks whose statements come from IndianAPI (Screener-format, consolidated, financial-year figures), and a Screener-style
ROCE for stocks that have only exchange-filing quarters.

One source per stock: when IndianAPI has the latest financial year for a stock, ROCE, ROE and debt/equity are IndianAPI's own figures
(debt/equity = borrowings / (equity capital + reserves), exactly Screener's). A stock with no IndianAPI year keeps what its filings give,
with ROCE computed the Screener way: profit before tax plus interest over (net worth + borrowings)."""
from datetime import date

MAX_AGE_DAYS = 550   # the financial year must end within ~18 months of the latest quarter, or it is not "current"


def ia_year_ratios(annual, ref_end: str | None) -> dict | None:
    """Latest IndianAPI financial year as {roce, roe, de, fy_end}, or None when this stock has no recent IndianAPI year."""
    if not annual or not ref_end:
        return None
    try:
        ref = date.fromisoformat(ref_end)
    except (TypeError, ValueError):
        return None
    for a in reversed(annual):
        if a["source"] != "indianapi":
            continue
        try:
            if (ref - date.fromisoformat(a["period_end_date"])).days > MAX_AGE_DAYS:
                return None
        except (TypeError, ValueError):
            return None
        eq, rs, bo = a["equity_capital_cr"], a["reserves_cr"], a["borrowings_cr"]
        de = bo / (eq + rs) if bo is not None and eq is not None and rs is not None and eq + rs > 0 else None
        return {"roce": a["roce_pct"], "roe": a["roe_pct"], "de": de, "fy_end": a["period_end_date"]}
    return None


def filings_roce(rows) -> float | None:
    """Screener-style ROCE for a stock with filings only: profit before tax + interest over the last 4 quarters, divided by (net worth + borrowings).
    Net worth is backed out of the stored trailing ROE (profit ÷ equity), so it is the same equity the filings' own ratios used. None unless all
    four quarters and the balance-sheet pieces are there."""
    last4 = rows[-4:]
    if len(last4) < 4:
        return None
    pbt = [r["pbt_cr"] for r in last4]
    fin = [r["finance_cost_cr"] for r in last4]
    npf = [r["net_profit_cr"] for r in last4]
    if any(x is None for x in pbt) or any(x is None for x in npf):
        return None
    roe, debt = rows[-1].get("roe_pct"), rows[-1].get("total_debt_cr")
    if roe is None or abs(roe) < 0.05 or debt is None:
        return None
    networth = sum(npf) / (roe / 100)
    if networth <= 0 or networth + debt <= 0:
        return None
    ebit = sum(pbt) + sum(x or 0 for x in fin)
    val = ebit / (networth + debt) * 100
    return round(val, 1) if abs(val) < 500 else None
