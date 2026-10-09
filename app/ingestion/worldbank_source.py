"""Real World Bank ingestion — India macro context (Layer 5). Free, public, no API
key — verified directly this session (India CPI inflation, GDP growth, both real
and current: 2025 data already available).

Annual only, not quarterly — a real, honest limitation of the source, not a
corner cut. India has no policy-rate or unemployment series sourced here (World
Bank doesn't carry RBI's repo rate); those stay NULL rather than guessed at.
US macro (quarterly, via FRED) is a separate source — see fred_source.py.
"""

import os
import re
from datetime import date, timedelta

import requests

from app.ingestion.base import DataSource

API_BASE = "https://api.worldbank.org/v2/country/IN/indicator"
CPI_INDICATOR = "FP.CPI.TOTL.ZG"      # inflation, consumer prices (annual %)
GDP_INDICATOR = "NY.GDP.MKTP.KD.ZG"   # GDP growth (annual %)


class WorldBankSource(DataSource):
    name = "worldbank"

    def sync_universe(self, conn) -> int:
        raise NotImplementedError("worldbank is a macro-only source — no symbol universe.")

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("worldbank is a macro-only source — see ingest_macro.")

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("worldbank is a macro-only source — see ingest_macro.")

    def _series(self, indicator: str) -> dict[str, float]:
        """{year: value} — World Bank's own pagination metadata (`pages`/`total`)
        confirms one call with a generous per_page covers full history; verified
        directly (66 years of India data in one response)."""
        resp = requests.get(f"{API_BASE}/{indicator}", params={"format": "json", "per_page": 100}, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if len(body) < 2 or not body[1]:
            return {}
        return {row["date"]: row["value"] for row in body[1] if row.get("value") is not None}

    def ingest_macro(self, conn, limit=None, symbol=None, progress=None) -> int:
        cpi = self._series(CPI_INDICATOR)
        gdp = self._series(GDP_INDICATOR)
        years = sorted(set(cpi.keys()) | set(gdp.keys()))
        rows_written = 0

        for year in years:
            conn.execute(
                """INSERT OR REPLACE INTO macro_context
                (market, period_end_date, cpi_yoy_pct, gdp_growth_pct, source)
                VALUES ('IN', ?, ?, ?, 'worldbank')""",
                (f"{year}-12-31", cpi.get(year), gdp.get(year)),
            )
            rows_written += 1
            if progress:
                progress(rows_written)

        rows_written += self._current_quarter(conn)
        conn.commit()
        return rows_written

    def _current_quarter(self, conn) -> int:
        """World Bank is annual and lags a year. This adds ONE row for the current quarter from sources that are
        current: the RBI's published Policy Repo Rate (homepage "Current Rates" box) and India's latest quarterly
        real GDP growth from FRED (OECD series INDGDPRQPSMEI). CPI/unemployment stay NULL on this row rather than
        carrying a stale annual figure forward — the Quality lens only prints what is present."""
        policy = gdp = None
        try:
            html = requests.get("https://www.rbi.org.in/", headers={"User-Agent": "Mozilla/5.0"}, timeout=30).text
            m = re.search(r"Policy\s*Repo\s*Rate\s*:\s*([\d.]+)\s*%", re.sub(r"<[^>]+>|&nbsp;", " ", html))
            policy = float(m.group(1)) if m else None
        except (requests.RequestException, ValueError):
            pass
        key = os.environ.get("FRED_API_KEY")
        if key:
            try:
                obs = requests.get("https://api.stlouisfed.org/fred/series/observations", timeout=30,
                                   params={"series_id": "INDGDPRQPSMEI", "api_key": key, "file_type": "json", "sort_order": "desc", "limit": 1}).json()["observations"]
                gdp = float(obs[0]["value"]) if obs and obs[0]["value"] != "." else None
            except (requests.RequestException, ValueError, KeyError):
                pass
        if policy is None and gdp is None:
            return 0
        today = date.today()
        end = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)  # last COMPLETED quarter end
        conn.execute(
            "INSERT OR REPLACE INTO macro_context (market, period_end_date, policy_rate_pct, gdp_growth_pct, source) VALUES ('IN', ?, ?, ?, 'rbi+fred')",
            (end.isoformat(), policy, gdp),
        )
        return 1
