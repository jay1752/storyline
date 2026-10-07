"""Real FRED (Federal Reserve Economic Data) ingestion — US macro context (Layer 5).
Free, self-serve API key (fredaccount.stlouisfed.org, email only, instant — no
approval step, same tier of friction as Tiingo/Finnhub). Endpoint shape verified
directly against FRED's own docs before writing this.

Four series, one row written per calendar quarter (aligning independently-cadenced
series — monthly for three of these, quarterly for GDP — onto the same quarterly
grid quarterly_data already uses):
  FEDFUNDS   Federal funds effective rate (monthly, already a %, no transform)
  CPIAUCSL   CPI, requested with units=pc1 (FRED's own year-over-year %
             transform) so this is real YoY inflation, not a raw index level
  GDPC1      Real GDP, units=pc1 — YoY growth %, same transform/methodology as
             India's World Bank series (worldbank_source.py) for comparability
  UNRATE     Unemployment rate (monthly, already a %)

For each quarter, takes each series' latest observation ON OR BEFORE that
quarter's end date — the standard "as-of" join for mismatched-frequency series.
"""

import os
from datetime import date, timedelta

import requests

from app.ingestion.base import DataSource, NotConfiguredError

API_URL = "https://api.stlouisfed.org/fred/series/observations"
API_KEY = os.environ.get("FRED_API_KEY")

SERIES = {
    "FEDFUNDS": {"field": "policy_rate_pct", "units": "lin"},
    "CPIAUCSL": {"field": "cpi_yoy_pct", "units": "pc1"},
    "GDPC1": {"field": "gdp_growth_pct", "units": "pc1"},
    "UNRATE": {"field": "unemployment_pct", "units": "lin"},
}

QUARTERS_OF_HISTORY = 300  # ~75 years — FRED's own history goes back to 1947-1954
# depending on series (verified directly against the API: FEDFUNDS from 1954-07-01,
# CPIAUCSL/GDPC1 from 1947-01-01, UNRATE from 1948-01-01); previously hardcoded to 20
# (5 years) even though _observations() already pulls each series' FULL history in one
# call and just discarded everything older than that. This was pure waste, not a real
# API limit — same free endpoint, same one call, just keeping more of what it returns.


def _quarter_ends(n: int) -> list[str]:
    today = date.today()
    q_end_month = ((today.month - 1) // 3) * 3 + 3
    end = date(today.year, q_end_month, 1) + timedelta(days=31)
    end = end.replace(day=1) - timedelta(days=1)  # last real day of the current quarter's end month
    ends = []
    for _ in range(n):
        ends.append(end.isoformat())
        prev_month_end = end.replace(day=1) - timedelta(days=1)
        for _ in range(2):
            prev_month_end = prev_month_end.replace(day=1) - timedelta(days=1)
        end = prev_month_end
    return sorted(ends)


class FredSource(DataSource):
    name = "fred"

    def _require_configured(self):
        if not API_KEY:
            raise NotConfiguredError("FRED_API_KEY not set — sign up free at fredaccount.stlouisfed.org, no KYC needed.")

    def sync_universe(self, conn) -> int:
        raise NotImplementedError("fred is a macro-only source — no symbol universe.")

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("fred is a macro-only source — see ingest_macro.")

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("fred is a macro-only source — see ingest_macro.")

    def _observations(self, series_id: str, units: str) -> dict[str, float]:
        """{date: value} — full history in one call, matching every other source
        here's "fetch everything, filter locally" pattern."""
        resp = requests.get(
            API_URL,
            params={"series_id": series_id, "api_key": API_KEY, "file_type": "json", "units": units},
            timeout=30,
        )
        resp.raise_for_status()
        out = {}
        for obs in resp.json().get("observations", []):
            if obs["value"] == ".":
                continue
            try:
                out[obs["date"]] = float(obs["value"])
            except ValueError:
                continue
        return out

    def _as_of(self, series: dict[str, float], as_of: str) -> float | None:
        candidates = [d for d in series if d <= as_of]
        return series[max(candidates)] if candidates else None

    def ingest_macro(self, conn, limit=None, symbol=None, progress=None) -> int:
        self._require_configured()
        series_data = {sid: self._observations(sid, cfg["units"]) for sid, cfg in SERIES.items()}
        rows_written = 0

        for q_end in _quarter_ends(QUARTERS_OF_HISTORY):
            values = {cfg["field"]: self._as_of(series_data[sid], q_end) for sid, cfg in SERIES.items()}
            if all(v is None for v in values.values()):
                continue
            conn.execute(
                """INSERT OR REPLACE INTO macro_context
                (market, period_end_date, policy_rate_pct, cpi_yoy_pct, gdp_growth_pct, unemployment_pct, source)
                VALUES ('US', ?, ?, ?, ?, ?, 'fred')""",
                (q_end, values["policy_rate_pct"], values["cpi_yoy_pct"], values["gdp_growth_pct"], values["unemployment_pct"]),
            )
            rows_written += 1
            if progress:
                progress(rows_written)

        conn.commit()
        return rows_written
