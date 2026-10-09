"""Annual (fiscal-year) figures. IndianAPI is the source of record where it has a year (normalised statements, 12 years incl. cash
flow); MoneyControl's older annual rows in quarterly_data fill the years IndianAPI doesn't have. Annual rows live in their own table
(annual_data) because an annual March row and the March QUARTER row cannot share quarterly_data's (symbol, period_end_date) key."""
from datetime import date, timedelta


class AnnualRow(dict):
    """Dict that answers None for any quarterly_data column an annual row doesn't have (so table builders can index it like a Row)."""

    def __missing__(self, key):
        return None


def annual_rows(conn, symbol: str) -> list[AnnualRow]:
    try:
        raw = conn.execute("SELECT * FROM annual_data WHERE symbol = ? ORDER BY period_end_date", (symbol,)).fetchall()
    except Exception:  # table not created yet on an old database
        return []
    rows = []
    for r in raw:
        d = AnnualRow(dict(r))
        d["quarter_label"] = f"FY{r['period_end_date'][:4]} (IndianAPI, annual)"
        rows.append(d)
    by = {r["period_end_date"]: r for r in rows}
    for r in rows:
        want = date.fromisoformat(r["period_end_date"]) - timedelta(days=365)
        prior = next((p for k, p in by.items() if abs((date.fromisoformat(k) - want).days) <= 10), None)
        if r["revenue_cr"] and prior and prior["revenue_cr"]:
            r["revenue_growth_pct"] = round((r["revenue_cr"] / prior["revenue_cr"] - 1) * 100, 1)
    return rows


def merge_annual(ia_rows: list, mc_rows: list) -> list:
    """IndianAPI wins per fiscal year and per field; MoneyControl's older annual rows fill the years - and, for banks, the fields
    (revenue, operating profit) - that IndianAPI doesn't give."""
    by = {r["period_end_date"]: r for r in mc_rows}
    for r in ia_rows:
        base = by.get(r["period_end_date"])
        if base is None:
            by[r["period_end_date"]] = r
            continue
        merged = AnnualRow(dict(base))
        merged.update({k: v for k, v in r.items() if v is not None})
        if r.get("eps") is not None:
            merged["eps_adjusted"] = None  # IndianAPI's EPS is already bonus/split-adjusted; an older adjusted figure must not shadow it
        by[r["period_end_date"]] = merged
    return [by[k] for k in sorted(by)]
