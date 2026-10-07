from abc import ABC, abstractmethod
from typing import Callable, Optional


class NotConfiguredError(Exception):
    """Raised when a source needs credentials/terms that aren't set up yet."""


def apply_symbol_filter(symbols: list[str], limit: Optional[int], symbol: Optional[str]) -> list[str]:
    """Shared selection logic every source uses: an explicit `symbol` always wins over
    `limit` (targeted re-ingestion beats a bulk-run cap)."""
    if symbol:
        return [s for s in symbols if s == symbol]
    if limit:
        return symbols[:limit]
    return symbols


_GROWTH_PCT_SANITY_CAP = 2000.0  # see safe_growth_pct


def safe_growth_pct(current: Optional[float], prior: Optional[float], places: int = 1) -> Optional[float]:
    """YoY growth as a percentage — None when prior isn't usable, same as the plain
    formula every source used to inline separately. Real gap found live, independently
    in all three (bse_source.py, sec_edgar_source.py, moneycontrol_source.py): a prior-
    year revenue base close to zero (common for an early-stage company's first real
    revenue quarter, or a reporting-rounding artifact) makes the formula blow up to a
    technically-correct but meaningless number — confirmed values up to 211,547,600%
    and -37,024,500% in production data. None of these represent real, comparable
    growth; they're a near-zero denominator, not a number anyone should see. Returns
    None instead whenever the result would exceed _GROWTH_PCT_SANITY_CAP (2000%, i.e.
    the prior base is under ~5% of current revenue) — matches how data vendors
    commonly mark this "NM" (not meaningful) rather than show a number that misleads
    more than an honest blank."""
    if current is None or not prior:
        return None
    pct = round((current / prior - 1) * 100, places)
    return pct if abs(pct) <= _GROWTH_PCT_SANITY_CAP else None


class DataSource(ABC):
    name: str

    def sync_universe(self, conn) -> int:
        """Write the full list of known symbols into `stocks`. Default: not supported —
        only sources that can list an exchange's full symbol set override this
        (SEC EDGAR, 5paisa/INDstocks). Cheap: one call, not per-symbol."""
        raise NotImplementedError(f"{self.name} doesn't provide a symbol universe listing.")

    @abstractmethod
    def ingest_quarterly(self, conn, limit: Optional[int] = None, symbol: Optional[str] = None,
                          progress: Optional[Callable[[int], None]] = None) -> int:
        """Write quarterly_data rows for this source's stocks. `symbol` restricts the run
        to exactly one symbol (re-ingestion after a correction/change) and takes priority
        over `limit`, which caps how many symbols a bulk run touches (None = all).
        `progress(rows_so_far)` is called after each symbol so a long run's status is
        visible while it's still going, not just at the end. Returns total rows written."""

    @abstractmethod
    def ingest_prices(self, conn, limit: Optional[int] = None, symbol: Optional[str] = None,
                       progress: Optional[Callable[[int], None]] = None) -> int:
        """Write price_history rows for this source's stocks. Same `limit`/`symbol`/`progress` contract."""

    def ingest_institutional(self, conn, limit: Optional[int] = None, symbol: Optional[str] = None,
                              progress: Optional[Callable[[int], None]] = None) -> int:
        """Write institutional_holdings rows (Layer 3). Default: not supported — this is
        a fundamentally different data shape (aggregated across thousands of external
        filers, not per-company), so only a source built specifically for it overrides
        this. Same `limit`/`symbol`/`progress` contract as the others."""
        raise NotImplementedError(f"{self.name} doesn't provide institutional holdings data.")

    def ingest_macro(self, conn, limit: Optional[int] = None, symbol: Optional[str] = None,
                      progress: Optional[Callable[[int], None]] = None) -> int:
        """Write macro_context rows (Layer 5) — market-level, not per-symbol, so `limit`/
        `symbol` are unused by every implementation so far but kept for interface
        consistency with the runner's uniform call signature."""
        raise NotImplementedError(f"{self.name} doesn't provide macro data.")
