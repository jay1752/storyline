from datetime import date, timedelta

from app.ingestion.base import DataSource
from app.ingestion.mock_data import PRICE_SERIES, QUARTERLY, QUARTERS, RATIOS, STOCKS


def _recent_quarter_ends(n=8, today=None):
    """Last n calendar-quarter-end dates, most recent completed quarter last."""
    today = today or date.today()
    q_end_month = ((today.month - 1) // 3) * 3  # end of the quarter BEFORE the current one
    y, m = today.year, q_end_month
    if m == 0:
        y, m = y - 1, 12
    ends = []
    for _ in range(n):
        last_day = (date(y, m, 1).replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        ends.append(last_day)
        m -= 3
        if m <= 0:
            m += 12
            y -= 1
    return list(reversed(ends))


class MockSource(DataSource):
    """Illustrative data, same numbers as the Analyst Desk / Position Desk artifacts.
    Stands in for 5paisa + SEC EDGAR until those are configured and built."""

    name = "mock"

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        period_ends = _recent_quarter_ends(8)
        conn.executemany(
            "INSERT OR REPLACE INTO stocks (symbol, exchange, name, sector, market) "
            "VALUES (:symbol,:exchange,:name,:sector,:market)",
            STOCKS,
        )
        targets = [symbol] if symbol else (list(QUARTERLY.keys())[:limit] if limit else list(QUARTERLY.keys()))
        rows = 0
        for sym in targets:
            series = QUARTERLY[sym]
            r = RATIOS[sym]
            for i, q in enumerate(QUARTERS):
                bs_idx = i - 4
                total_debt = r["total_debt_cr"][bs_idx] if bs_idx >= 0 else None
                net_debt = r["net_debt_cr"][bs_idx] if bs_idx >= 0 else None
                cash = r["cash_cr"][bs_idx] if bs_idx >= 0 else None
                is_latest = i == 7
                conn.execute(
                    """INSERT OR REPLACE INTO quarterly_data
                    (symbol, period_end_date, quarter_index, quarter_label, source,
                     revenue_cr, operating_profit_cr, opm_pct,
                     other_income_cr, net_profit_cr, eps, revenue_growth_pct, guidance_vs_delivered,
                     growth_vs_sector_pp, margin_vs_sector_pp, fii_pct, dii_pct, promoter_pct,
                     promoter_pledge, sector_momentum, total_debt_cr, net_debt_cr, cash_cr,
                     roe_pct, roce_pct, debt_equity, pe_current, pe_low_3yr, pe_high_3yr)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        sym, period_ends[i].isoformat(), i + 1, q, "mock",
                        series["revenue_cr"][i], series["operating_profit_cr"][i], series["opm_pct"][i],
                        series["other_income_cr"][i], series["net_profit_cr"][i], series["eps"][i],
                        series["revenue_growth_pct"][i], series["guidance_vs_delivered"][i],
                        series["growth_vs_sector_pp"][i], series["margin_vs_sector_pp"][i],
                        series["fii_pct"][i], series["dii_pct"][i], series["promoter_pct"][i],
                        series["promoter_pledge"][i], series["sector_momentum"][i],
                        total_debt, net_debt, cash,
                        r["roe_pct"] if is_latest else None, r["roce_pct"] if is_latest else None,
                        r["debt_equity"] if is_latest else None, r["pe_current"] if is_latest else None,
                        r["pe_low_3yr"] if is_latest else None, r["pe_high_3yr"] if is_latest else None,
                    ),
                )
                rows += 1
            if progress:
                progress(rows)
        conn.commit()
        return rows

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        today = date.today()
        targets = [symbol] if symbol else (list(PRICE_SERIES.keys())[:limit] if limit else list(PRICE_SERIES.keys()))
        rows = 0
        for sym in targets:
            prices = PRICE_SERIES[sym]
            n = len(prices)
            dates = [(today - timedelta(days=30 * (n - 1 - i))).isoformat() for i in range(n)]
            for d, p in zip(dates, prices):
                conn.execute(
                    "INSERT OR REPLACE INTO price_history (symbol, date, close, source) VALUES (?,?,?,'mock')",
                    (sym, d, p),
                )
                rows += 1
            if progress:
                progress(rows)
        conn.commit()
        return rows
