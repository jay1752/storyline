"""Real World Bank ingestion — India macro context (Layer 5). Free, public, no API
key — verified directly this session (India CPI inflation, GDP growth, both real
and current: 2025 data already available).

Annual only, not quarterly — a real, honest limitation of the source, not a
corner cut. India has no policy-rate or unemployment series sourced here (World
Bank doesn't carry RBI's repo rate); those stay NULL rather than guessed at.
US macro (quarterly, via FRED) is a separate source — see fred_source.py.
"""

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

        conn.commit()
        return rows_written
