"""Real SEC EDGAR ingestion — free, public, no API key. Only genuinely unblocked
source in this app (see admin dashboard for why the others aren't).

Covers Layer 1 (revenue, operating profit, margin, net profit, EPS, revenue growth,
balance sheet) plus sector classification (SIC code, feeds Layers 2 & 4 once those are
built). Layer 3 (FII/DII-equivalent) isn't in EDGAR at all — left NULL, same honest-gap
treatment as the Macro stub. ROE/ROCE/D-E are computed here (trailing-twelve-month,
stored on the latest period only, needs 4 consecutive quarters of history). P/E stays
NULL — it needs a current price, which this source doesn't provide (Tiingo does).

Fiscal Q4 (only ever reported in the 10-K, never a 10-Q) is derived as
annual - sum(first 3 quarters) — the standard industry approximation analysts use.
This isn't just a completeness fix: without it, the TTM window used for ROE/ROCE
(the "last 4 periods" in the sorted list) silently spliced together non-consecutive
quarters across the missing gap, quietly corrupting those ratios for every company
near a fiscal year-end. EPS derived this way is a known approximation — diluted
share count can shift quarter to quarter, so Q4 EPS = FY EPS - 9M EPS isn't exact,
just the standard convention.

Full-universe note: SEC's ticker file lists ~10,000 entities (many are ETFs, trusts,
shells with no real quarterly fundamentals). sync_universe() loads all of them into
`stocks` — cheap, one call. Actually pulling companyfacts for all of them is the
expensive part: rate-limited to stay under SEC's fair-use guidance (~10 req/sec),
so ~10,000 symbols is realistically an hours-long background run, not a button click
you wait on. Use `limit` to test on a slice before committing to a full run.
"""

import os
import time
from datetime import date, timedelta

import requests

from app.ingestion.base import DataSource, NotConfiguredError, apply_symbol_filter, safe_growth_pct

UA = os.environ.get("SEC_EDGAR_USER_AGENT")  # required by SEC's fair-use policy, e.g. "Tenetfolio you@example.com"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
RATE_LIMIT_SECONDS = 0.15  # ~6-7 req/sec, safely under SEC's ~10 req/sec fair-use guidance

# Ticker -> CIK overrides for cases where SEC's own company_tickers.json points a
# familiar ticker at the WRONG entity. Real bug found live: "XOM" resolves via SEC's
# own file to CIK 2115436, "ExxonMobil Holdings Corp" — a newer holding-company
# entity with only 94 us-gaap tags and 4 data points total (one single 10-Q, filed
# 2026-08-03), not the real long-standing filer. The actual "Exxon Mobil Corporation"
# (CIK 34088 — confirmed: its own accession numbers are prefixed 0000034088, matching
# recent XOM filings) has 438 tags and 127 quarters of Revenue history back to 2009.
# Small and manual on purpose, same reasoning as finnhub_source.py's ADR-ticker list —
# each entry verified individually (real, deep companyfacts data) before being added,
# not guessed at scale.
CIK_OVERRIDES = {
    "XOM": 34088,
}

# RevenuesNetOfInterestExpense appended last (not a replacement) — real gap found
# live: Goldman Sachs had ZERO rows in quarterly_data, not just missing fields,
# because none of the other 3 tags exist in its filings at all (confirmed via its
# raw companyfacts). Checked across 5 major banks (GS, JPM, BAC, WFC, MS): banks'
# income statements report revenue net of interest expense as a distinct line
# their industrial-company tags don't cover, so this is a genuine bank-specific
# tag, not a guess — but it must stay last so a company that already matches
# Revenues/SalesRevenueNet keeps using that (first-tag-wins in _quarterly_series).
# RevenueFromContractWithCustomerIncludingAssessedTax appended last too — real gap
# found live: CrowdStrike had ZERO rows despite being an ordinary US 10-Q filer,
# because it tags revenue gross of assessed tax (the "Including" variant) rather
# than net of it (the "Excluding" variant already covered above) — a real, valid
# ASC 606 reporting choice some filers make, not a data problem on their end.
# Checked 5 SaaS peers (DDOG, NET, PANW, ZS, SNOW): none share this gap, so it
# looks isolated rather than systemic, but cheap and safe to cover regardless.
REVENUE_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                "RevenuesNetOfInterestExpense", "RevenueFromContractWithCustomerIncludingAssessedTax"]
# Community banks/thrifts (~390 US stocks had ZERO rows) tag none of REVENUE_TAGS: their income
# statement is net interest income + noninterest income. Used only when REVENUE_TAGS yield nothing,
# summed per period ONLY where both components exist (a lone NII would understate revenue), and it is
# net of interest expense — the same convention as RevenuesNetOfInterestExpense for the big banks.
BANK_REVENUE_COMPONENT_TAGS = ("InterestIncomeExpenseNet", "NoninterestIncome")
NET_INCOME_TAGS = ["NetIncomeLoss"]
# Only fills periods NetIncomeLoss lacks (REITs/partnerships report these instead); never overrides it.
NET_INCOME_FILL_TAGS = ["NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"]  # confirmed present for banks too — no substitute needed
# OperatingIncomeLoss confirmed ABSENT for every major bank checked (GS, JPM, BAC,
# WFC, MS) — banks' income statements have no classified operating-income subtotal
# distinct from pretax income (revenue flows straight to pretax income through a
# list of interest/provision/noninterest expenses). Left as the sole tag rather
# than adding a bank substitute: PBT_TAGS' pretax-income figure is a different,
# larger number (includes items operating income excludes), so faking it in would
# silently mislabel one metric as another. op_income/opm_pct legitimately stay
# NULL for banks — an honest structural gap, same treatment as the Layer-3 stub.
OPERATING_INCOME_TAGS = ["OperatingIncomeLoss"]
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]  # confirmed present for banks too

# Balance sheet tags are "instant" (point-in-time) XBRL facts, not "duration" facts like
# revenue — no start date, just a snapshot at period end. Handled by _instant_series, not
# _quarterly_series. Debt tagging is the least standardized of these across companies;
# tries a few common variants and merges them (see _instant_series's merge_tags).
# LongTermDebtAndCapitalLeaseObligations appended after confirming live (Exxon, AbbVie)
# that filers who migrated here around the 2019 lease-accounting standard (ASC 842)
# stopped updating the older tags entirely — without merge_tags this one would never
# even get tried, since an earlier tag's stale-but-real old data already "wins".
# DebtInstrumentCarryingAmount deliberately NOT added: checked live on Oracle and its
# values swing wildly quarter to quarter (6.25B, 14B, 92.9B, 18B, 43B, 130B) — a
# per-instrument or per-disclosure figure, not total debt outstanding, so treating it
# as one would silently corrupt debt_equity rather than just leave it NULL.
DEBT_TAGS = ["LongTermDebtNoncurrent", "LongTermDebt", "DebtLongtermAndShorttermCombinedAmount",
             "LongTermDebtAndCapitalLeaseObligations"]
# Real gap found live (Agree Realty, a REIT): some filers split total debt across TWO
# tags instead of reporting one consolidated figure — SecuredDebt ($41.19M) and
# UnsecuredDebt ($696.28M) both current through the latest quarter, while the
# DEBT_TAGS above (LongTermDebt here) had gone stale in 2022, the same tag-migration
# pattern as Exxon/AbbVie but to a SPLIT pair rather than one replacement tag. These
# need SUMMING, not merging as alternatives — unlike DEBT_TAGS, where only one tag
# ever has real data for a given period, a filer using this split pair can report
# both simultaneously and the true total is neither alone.
SPLIT_DEBT_TAGS = ["SecuredDebt", "UnsecuredDebt"]
# Real gap found live (AAON): a genuinely low-leverage company can carry zero
# long-term debt for most/all periods and simply never report any LONG-TERM debt
# tag at all — DEBT_TAGS came back entirely empty across all 38 quarters even
# though SEC's own companyfacts has the real number under DebtCurrent (confirmed:
# $0 most quarters, a real $16M in a couple of 2024-2025 quarters). Same
# fallback-only-when-nothing-else-has-data treatment as SPLIT_DEBT_TAGS, not a
# blanket sum — a company that already gets a number from DEBT_TAGS (e.g. the
# explicitly-combined DebtLongtermAndShorttermCombinedAmount tag) may already
# include its current portion, so adding DebtCurrent on top there would double-count.
CURRENT_DEBT_FALLBACK_TAG = "DebtCurrent"
# Debt fallback (second pass, 2026-10-07 gap audit: 44% of US stocks had no total debt on their latest
# quarter). Used ONLY for periods the tags above left empty, and only ONE tag — the newest-data one —
# is taken, never a sum, because these are components a filer may report next to each other.
# DebtInstrumentCarryingAmount stays excluded (per-instrument figure, see above).
FALLBACK_DEBT_TAGS = ["LongTermNotesPayable", "NotesPayable", "ConvertibleNotesPayable", "SeniorNotes",
                      "LongTermLineOfCredit", "LineOfCredit", "OtherBorrowings", "SubordinatedDebt",
                      "AdvancesFromFederalHomeLoanBanks", "DebtAndCapitalLeaseObligations", "FinanceLeaseLiability"]
# Revenue fallback for filers none of REVENUE_TAGS cover (REIT/mortgage/BDC/lessor/insurer/broker): used only
# when the main tags give nothing or have gone stale (newest period > 100 days behind these), and it only
# FILLS periods the main tags lack. Net-of-interest-expense convention is kept for banks via the
# component sum above; these are each company's own top-line.
FALLBACK_REVENUE_TAGS = ["RegulatedAndUnregulatedOperatingRevenue", "RegulatedOperatingRevenue", "ElectricUtilityRevenue", "RealEstateRevenueNet", "OperatingLeasesIncomeStatementLeaseRevenue", "OperatingLeaseLeaseIncome", "InterestAndDividendIncomeOperating",
                         "GrossInvestmentIncomeOperating", "InterestIncomeOperating", "PremiumsEarnedNet",
                         "RevenuesExcludingInterestAndDividends", "SalesRevenueServicesNet", "SalesRevenueGoodsNet",
                         "RevenueMineralSales", "OilAndGasRevenue"]
# Operating income when OperatingIncomeLoss isn't tagged: revenue - CostsAndExpenses, else GrossProfit - OperatingExpenses.
COSTS_TAG, GROSS_PROFIT_TAG, OPEX_TAG = "CostsAndExpenses", "GrossProfit", "OperatingExpenses"
# EPS when neither EPS tag is reported for a period: net income / weighted-average diluted (else basic) shares.
WEIGHTED_SHARES_TAGS = ["WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic"]
# Inputs for the scan's coverage / liquidity / cash-conversion rows (all optional — a filer that doesn't tag one simply gets NULL).
INTEREST_EXPENSE_TAGS = ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt", "InterestAndDebtExpense",
                         "InterestExpenseOperating", "InterestExpenseRelatedParty"]
CURRENT_ASSETS_TAGS = ["AssetsCurrent"]
INVENTORY_TAGS = ["InventoryNet", "InventoryFinishedGoodsNetOfReserves", "InventoryGross"]
CFO_TAG = "NetCashProvidedByUsedInOperatingActivities"
CASH_TAGS = ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]
EQUITY_TAGS = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
               "PartnersCapital", "MembersEquity"]
ASSETS_TAGS = ["Assets"]
# LiabilitiesCurrent confirmed ABSENT for every major bank checked (GS, JPM, BAC,
# WFC, MS): banks file an unclassified, liquidity-ordered balance sheet with no
# current/noncurrent split at all (deposits and trading liabilities don't sort
# into that framework the way an industrial company's do), so there's no bank
# substitute to add here — capital-employed/ROCE legitimately stays NULL for
# banks, an honest structural gap rather than a bug.
CURRENT_LIABILITIES_TAGS = ["LiabilitiesCurrent"]

# PBT/tax are duration facts like revenue (handled by _quarterly_series + Q4 derivation).
# Shares outstanding is a point-in-time fact like the balance sheet items (_instant_series).
# All three confirmed present in real companyfacts data (checked against Apple's CIK)
# before wiring up — previously never queried at all, not attempted-and-failed.
PBT_TAGS = [
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
]
TAX_TAGS = ["IncomeTaxExpenseBenefit"]
SHARES_OUTSTANDING_TAGS = ["CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"]
# Real gap found live (same bug class as moneycontrol_source.py's Other Income miss):
# never queried at all before now. Confirmed present with real data on Apple's own
# companyfacts (a genuine ~$572M quarterly nonoperating income figure) — a duration
# fact like revenue, not a balance-sheet instant.
# Confirmed ABSENT for every major bank checked (GS, JPM, BAC, WFC, MS): banks
# don't report a distinct "nonoperating income" line the way industrials do —
# interest, trading, and fee income are all core operating income to a bank, with
# no separate below-the-line bucket. No bank substitute added; other_income_cr
# legitimately stays NULL for banks, an honest structural gap, not a bug.
OTHER_INCOME_TAGS = ["NonoperatingIncomeExpense", "OtherNonoperatingIncomeExpense"]


def _derive_missing_q4(quarterly: dict, annual: dict) -> dict:
    """For each fiscal year where we have the 10-K annual figure and all 3 of the
    other quarters already (from 10-Qs), derive the missing 4th as
    annual - sum(3 known quarters), keyed by the fiscal year-end date. Returns only
    the newly-derived entries — caller merges them into the quarterly dict."""
    derived = {}
    for fy_end, annual_val in annual.items():
        if fy_end in quarterly:
            continue  # already have a real value at this exact date, nothing to derive
        fy_end_date = date.fromisoformat(fy_end)
        candidates = sorted(
            (d for d in quarterly if 60 <= (fy_end_date - date.fromisoformat(d)).days <= 370),
            key=lambda d: date.fromisoformat(d),
        )
        fy_quarters = candidates[-3:]
        if len(fy_quarters) == 3:
            derived[fy_end] = annual_val - sum(quarterly[d] for d in fy_quarters)
    return derived


class SecEdgarSource(DataSource):
    name = "sec_edgar"

    def _headers(self):
        if not UA:
            raise NotConfiguredError(
                "SEC_EDGAR_USER_AGENT not set — SEC requires a descriptive User-Agent "
                "(app name + contact email) on every request, or it blocks you."
            )
        return {"User-Agent": UA}

    def _all_tickers(self) -> list[dict]:
        resp = requests.get(TICKERS_URL, headers=self._headers(), timeout=20)
        resp.raise_for_status()
        return list(resp.json().values())

    def sync_universe(self, conn) -> int:
        rows = 0
        for row in self._all_tickers():
            conn.execute(
                """INSERT INTO stocks (symbol, exchange, name, sector, market) VALUES (?,?,?,?, 'US')
                ON CONFLICT(symbol) DO UPDATE SET exchange=excluded.exchange, name=excluded.name, market=excluded.market""",
                # sector excluded from the update clause on purpose — re-running sync_universe must never
                # clobber real enrichment _sector_for() already wrote (same class of bug found in bse_source.py
                # and fivepaisa_source.py, both of which share this exact pattern).
                (f"NASDAQ:{row['ticker'].upper()}", "NASDAQ", row["title"], "Unclassified"),
            )
            rows += 1
        conn.commit()
        return rows

    def _sector_for(self, cik: int) -> str | None:
        """SIC industry description — feeds sector-benchmark (Layer 2) and sector-momentum
        (Layer 4) once those are built. A different endpoint than companyfacts, one more
        call per symbol."""
        try:
            resp = requests.get(SUBMISSIONS_URL.format(cik=cik), headers=self._headers(), timeout=20)
            resp.raise_for_status()
            return resp.json().get("sicDescription") or None
        except requests.RequestException:
            return None

    @staticmethod
    def _rank_tags(facts: dict, tags: list[str]) -> list[str]:
        """Tags that actually have data, NEWEST data first (ties keep the list's own priority).

        Real bug found live: these series used to take the first tag in the list with ANY data
        and stop. Filers migrate XBRL tags over time, so that first tag is often one the company
        abandoned years ago — ABBV's ASC 606 revenue tag ends in 2018 while `Revenues` runs to
        2026; GOOGL's ends 2025-03, JPM's `Revenues` ends 2014 while `RevenuesNetOfInterestExpense`
        is current. The result was ~1,100 still-trading US stocks frozen at an old quarter.
        A tag needs >= 4 distinct periods to outrank the rest (a stray one-off fact shouldn't win)."""
        gaap = facts.get("facts", {}).get("us-gaap", {})
        info = []
        for i, tag in enumerate(tags):
            node = gaap.get(tag)
            if not node:
                continue
            ends = {f["end"] for u in node.get("units", {}).values() for f in u
                    if f.get("form") in ("10-Q", "10-K") and "end" in f}
            if ends:
                info.append((i, tag, max(ends), len(ends)))
        solid = [x for x in info if x[3] >= 4] or info
        rest = [x for x in info if x not in solid]
        solid.sort(key=lambda x: (x[2], -x[0]), reverse=True)  # newest first; on a tie, earlier in the list wins
        return [x[1] for x in solid + rest]

    def _quarterly_series(self, facts: dict, tags: list[str]) -> dict[str, float]:
        """Return {period_end_date: value} for genuinely single-quarter (~80-100 day) 10-Q facts.
        The tag with the newest data is primary; older tags only FILL periods it lacks (never
        override it), so a tag migration yields one continuous history."""
        out: dict[str, float] = {}
        for tag in self._rank_tags(facts, tags):
            node = facts.get("facts", {}).get("us-gaap", {}).get(tag)
            got: dict[str, float] = {}
            for unit_facts in node.get("units", {}).values():
                for f in unit_facts:
                    if f.get("form") != "10-Q" or "start" not in f or "end" not in f:
                        continue
                    start, end = date.fromisoformat(f["start"]), date.fromisoformat(f["end"])
                    if not (70 <= (end - start).days <= 125):
                        continue  # skip YTD-cumulative facts (>=170d), keep single quarters incl. 12/16-week retail ones
                    got[f["end"]] = f["val"]  # last filed wins if there are restatements for the same period
            for k, v in got.items():
                out.setdefault(k, v)
        return out

    def _annual_series(self, facts: dict, tags: list[str]) -> dict[str, float]:
        """Return {fiscal_year_end_date: value} for full-fiscal-year (~350-380 day)
        10-K facts — the source of the fiscal Q4 figure a 10-Q never reports. Same
        newest-tag-first / fill-gaps rule as _quarterly_series, using the same ranking so
        the quarterly and annual halves of a Q4 derivation come from the same tag."""
        out: dict[str, float] = {}
        for tag in self._rank_tags(facts, tags):
            node = facts.get("facts", {}).get("us-gaap", {}).get(tag)
            got: dict[str, float] = {}
            for unit_facts in node.get("units", {}).values():
                for f in unit_facts:
                    if f.get("form") != "10-K" or "start" not in f or "end" not in f:
                        continue
                    start, end = date.fromisoformat(f["start"]), date.fromisoformat(f["end"])
                    if not (350 <= (end - start).days <= 380):
                        continue
                    got[f["end"]] = f["val"]
            for k, v in got.items():
                out.setdefault(k, v)
        return out

    def _cfo_quarterly(self, facts: dict) -> dict[str, float]:
        """Quarterly operating cash flow. 10-Qs only report it year-to-date (3, 6, 9 months from the fiscal-year start; the
        10-K gives 12), so each quarter is the YTD figure minus the previous YTD figure that shares the same start date."""
        node = facts.get("facts", {}).get("us-gaap", {}).get(CFO_TAG)
        if not node:
            return {}
        by_start: dict[str, dict[str, float]] = {}
        for unit_facts in node.get("units", {}).values():
            for f in unit_facts:
                if f.get("form") not in ("10-Q", "10-K") or "start" not in f or "end" not in f:
                    continue
                by_start.setdefault(f["start"], {})[f["end"]] = f["val"]  # later filings overwrite earlier (restatements)
        out: dict[str, float] = {}
        for start, ends in by_start.items():
            prev_end, prev_val = None, 0.0
            for end in sorted(ends):
                span = (date.fromisoformat(end) - date.fromisoformat(start)).days
                if span < 60 or span > 380:
                    continue
                gap = (date.fromisoformat(end) - date.fromisoformat(prev_end)).days if prev_end else span
                if 70 <= gap <= 125:
                    out[end] = ends[end] - prev_val
                prev_end, prev_val = end, ends[end]
        return out

    def _bank_revenue_series(self, facts: dict) -> tuple[dict[str, float], dict[str, float]]:
        """(quarterly, annual) revenue for a bank that tags none of REVENUE_TAGS: net interest
        income + noninterest income, kept only for periods where BOTH components exist."""
        nii, nonint = BANK_REVENUE_COMPONENT_TAGS
        q_nii, q_non = self._quarterly_series(facts, [nii]), self._quarterly_series(facts, [nonint])
        a_nii, a_non = self._annual_series(facts, [nii]), self._annual_series(facts, [nonint])
        return (
            {p: q_nii[p] + q_non[p] for p in q_nii.keys() & q_non.keys()},
            {p: a_nii[p] + a_non[p] for p in a_nii.keys() & a_non.keys()},
        )

    def _instant_series(self, facts: dict, tags: list[str], prefer: str = "last", merge_tags: bool = False) -> dict[str, float]:
        """Return {period_end_date: value} for point-in-time balance sheet facts — no
        duration filter, since these have no start date (a snapshot, not a flow).
        Accepts both 10-Q and 10-K: unlike P&L, the fiscal-year-end balance sheet
        needs no derivation — the 10-K reports it directly as of that date, so
        excluding 10-K here (as this used to) silently dropped one point-in-time
        snapshot per company per year for no reason.

        prefer="last" (default, unchanged behavior) keeps the most-recently-filed
        value for a given date — fine for debt/cash/equity/assets, where a later
        filing usually just corrects an error. prefer="first" keeps the value as
        FIRST reported instead: needed for shares_outstanding, where a stock split
        makes every later filing retroactively restate historical comparative share
        counts to the post-split figure (confirmed live: NVIDIA's FY2024 10-K
        reported 2,464,000,000 shares at 2024-01-28; the FOLLOWING year's 10-K
        restates that same date's comparative to 24,643,000,000 — the split-adjusted
        number). Blending first-reported and later-restated values for the same
        date produced a non-monotonic series with false ratio spikes, corrupting
        the corporate-action detector in db.py (an observed eps_adjusted of -23.73
        for GOOGL, from a spurious jump-then-revert around its real 2022 split).

        merge_tags=False (default) keeps the original "first tag with ANY data
        wins, exclusively" behavior — fine when a company only ever uses one tag
        for a field's whole history. merge_tags=True instead unions every tag's
        periods together: needed for DEBT_TAGS, where a real gap was found live —
        many filers (confirmed on Exxon, AbbVie) switched XBRL tags for debt
        around the 2019 lease-accounting standard change (ASC 842), so an old tag
        like LongTermDebtNoncurrent still has real historical data (winning the
        exclusive-first-match) while the CURRENT data sits under a newer tag
        further down the list that never gets tried. Safe here because a tag
        migration means at most one tag has data for any given period — there's
        nothing to pick between, just gaps to fill from whichever tag reported
        that specific date."""
        gaap = facts.get("facts", {}).get("us-gaap", {})
        out = {}
        # merge_tags=True keeps its original list-order union (DEBT_TAGS semantics, unchanged).
        # Otherwise: newest-data tag is primary and the others only fill periods it lacks —
        # the same abandoned-tag fix as _quarterly_series (see _rank_tags).
        ordered = tags if merge_tags else self._rank_tags(facts, tags)
        for tag in ordered:
            node = gaap.get(tag)
            if not node:
                continue
            local = {}
            for unit_facts in node.get("units", {}).values():
                for f in unit_facts:
                    if f.get("form") not in ("10-Q", "10-K") or "end" not in f:
                        continue
                    if prefer == "first" and f["end"] in local:
                        continue
                    local[f["end"]] = f["val"]
            if merge_tags:
                out.update(local)
            else:
                for k, v in local.items():
                    out.setdefault(k, v)
        return out

    def _cover_page_shares_series(self, facts: dict) -> list[tuple[str, float]]:
        """dei:EntityCommonStockSharesOutstanding — the cover-page share count every
        10-Q/10-K reports, regardless of whether that filer also tags the balance
        sheet's us-gaap:CommonStockSharesOutstanding on its 10-Qs. Real gap found
        live: NVIDIA only tags the latter on its annual 10-K (fiscal year-end dates),
        never on a 10-Q, so _instant_series alone left shares_outstanding NULL for
        every one of its quarterly periods. This dei fact's own "end" is the FILING
        (cover-page) date, not the fiscal period end — typically 3-6 weeks later —
        so it can't be keyed by period end the way a real instant fact can; callers
        must match it to the nearest qualifying period end themselves. Unlike the
        balance-sheet fact, this one is not retroactively restated for a later
        split (each filing's cover page only ever reports its own point in time),
        so no first-vs-last ambiguity applies here."""
        node = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding")
        if not node:
            return []
        points = set()
        for unit_facts in node.get("units", {}).values():
            for f in unit_facts:
                if f.get("form") not in ("10-Q", "10-K") or "end" not in f:
                    continue
                points.add((f["end"], f["val"]))
        return sorted(points)

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None, force=False) -> int:
        symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM stocks WHERE market = 'US'").fetchall()]
        symbols = apply_symbol_filter(symbols, limit, symbol)

        if not symbol and not force:
            # Resume, don't restart: this source never had any skip logic at all —
            # every bulk trigger reprocessed the full ~10,000-symbol US universe from
            # scratch (matches the "hours-long background run" already noted in this
            # module's docstring). Needed now specifically to backfill Other Income
            # and multi-period shares_outstanding (both real gaps just fixed) without
            # re-running the entire universe: skip only symbols that already show one
            # of these on at least one row, since that's the signal a symbol has been
            # reprocessed with the current code — a symbol with existing coverage but
            # neither field populated yet still gets reattempted, same as a genuinely
            # new symbol with no coverage at all. An explicit `symbol` re-ingestion
            # always runs regardless, same as every other source.
            #
            # other_income_cr ALONE is not a safe "already reprocessed" signal: banks
            # (confirmed live on GS, JPM, BAC, WFC, MS) never get it populated at all
            # — their income statements have no distinct nonoperating-income line, an
            # honest structural gap, not a transient miss — so every bank would look
            # perpetually "uncovered" and get needlessly reprocessed on every future
            # bulk run forever. shares_outstanding doesn't have that problem: it's
            # backed by dei:EntityCommonStockSharesOutstanding, an SEC cover-page
            # field mandated on every 10-Q/10-K regardless of industry (confirmed
            # populated for both GS and JPM above), so it's the reliable universal
            # half of this OR.
            already_covered = {r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM quarterly_data WHERE source = 'sec_edgar' "
                "AND (other_income_cr IS NOT NULL OR shares_outstanding IS NOT NULL)"
            ).fetchall()}
            symbols = [s for s in symbols if s not in already_covered]

        cik_lookup = {row["ticker"].upper(): int(row["cik_str"]) for row in self._all_tickers()}
        rows_written = 0

        for sym in symbols:
            ticker = sym.split(":")[1]
            time.sleep(RATE_LIMIT_SECONDS)
            cik = CIK_OVERRIDES.get(ticker) or cik_lookup.get(ticker)
            if cik is None:
                continue

            # Sector classification moved out of _process_symbol and run here,
            # unconditionally for every symbol with a resolved CIK — real gap found
            # live: it used to sit after _process_symbol's `if not revenue: return 0`
            # early-return, so any symbol with no usable revenue tag never reached it
            # either, even though SIC classification (a submissions-endpoint lookup,
            # unrelated to companyfacts) doesn't need revenue data at all. Confirmed
            # this was the dominant cause of 49.7% of the US universe showing
            # "Unclassified" — foreign private issuers (ASML, HSBC, Shell, Alibaba,
            # Toyota, etc.) that file an annual 20-F instead of a 10-Q, so they never
            # have usable revenue facts, but still have a real CIK and a real SIC
            # code. Skipped when already classified, so a bulk re-trigger doesn't
            # spend its rate-limit budget re-confirming symbols already done.
            current_sector = conn.execute("SELECT sector FROM stocks WHERE symbol = ?", (sym,)).fetchone()
            if current_sector and current_sector[0] in (None, "Unclassified"):
                time.sleep(RATE_LIMIT_SECONDS)
                sector = self._sector_for(cik)
                if sector:
                    conn.execute("UPDATE stocks SET sector = ? WHERE symbol = ?", (sector, sym))
                    conn.commit()

            try:
                resp = requests.get(FACTS_URL.format(cik=cik), headers=self._headers(), timeout=20)
                if resp.status_code == 404:
                    continue  # no XBRL facts for this entity (e.g. a fund/trust) — expected for many tickers
                resp.raise_for_status()
                facts = resp.json()
            except requests.RequestException:
                continue  # one symbol failing shouldn't kill a multi-hundred-symbol run

            try:
                rows_written += self._process_symbol(conn, sym, facts)
            except Exception:  # noqa: BLE001 — one symbol's edge case must never kill a 10,000-symbol run
                continue
            if progress:
                progress(rows_written)

        return rows_written

    def _process_symbol(self, conn, sym: str, facts: dict) -> int:
        """Parses and writes one symbol's full quarterly history. Raises on unexpected
        data shapes — the caller catches broadly, since XBRL tagging is inconsistent
        enough across ~10,000 companies that some edge case here is inevitable, and it
        must never take the whole run down (see the division-by-zero incident this
        replaced — one pre-revenue company's $0 year-ago quarter killed the entire
        multi-thousand-symbol run because this used to be inline in the main loop,
        outside any per-symbol try/except)."""
        rows_written = 0
        revenue = self._quarterly_series(facts, REVENUE_TAGS)
        net_income = self._quarterly_series(facts, NET_INCOME_TAGS)
        for k, v in self._quarterly_series(facts, NET_INCOME_FILL_TAGS).items():
            net_income.setdefault(k, v)
        op_income = self._quarterly_series(facts, OPERATING_INCOME_TAGS)
        eps = self._quarterly_series(facts, EPS_TAGS)
        other_income = self._quarterly_series(facts, OTHER_INCOME_TAGS)
        annual_revenue = None
        if not revenue:
            revenue, annual_revenue = self._bank_revenue_series(facts)
        if annual_revenue is None:
            annual_revenue = self._annual_series(facts, REVENUE_TAGS)
        # Fallback top-line tags (REIT/BDC/lessor/insurer...) — only when the main tags are empty or stale.
        fb_rank = self._rank_tags(facts, FALLBACK_REVENUE_TAGS)
        fb_tag = [fb_rank[0]] if fb_rank else []  # ONE tag, never a blend of components
        fb_q = self._quarterly_series(facts, fb_tag) if fb_tag else {}
        if fb_q:
            newest = max(revenue) if revenue else None
            if newest is None or max(fb_q) > (date.fromisoformat(newest) + timedelta(days=100)).isoformat():
                for k, v in fb_q.items():
                    revenue.setdefault(k, v)
                for k, v in self._annual_series(facts, fb_tag).items():
                    annual_revenue.setdefault(k, v)
        pre_revenue = not revenue
        if pre_revenue and not net_income:
            return 0  # nothing to show at all (fund/trust/shell)

        # Derive fiscal Q4 (never in a 10-Q) from the 10-K annual figure minus the
        # other 3 quarters — done per metric since tagging consistency varies.
        derived_revenue = _derive_missing_q4(revenue, annual_revenue)
        revenue.update(derived_revenue)

        # Operating income fallbacks (quarterly and annual built the same way so the Q4 derivation stays consistent).
        op_annual = self._annual_series(facts, OPERATING_INCOME_TAGS)
        costs_q, costs_a = self._quarterly_series(facts, [COSTS_TAG]), self._annual_series(facts, [COSTS_TAG])
        gp_q, gp_a = self._quarterly_series(facts, [GROSS_PROFIT_TAG]), self._annual_series(facts, [GROSS_PROFIT_TAG])
        opex_q, opex_a = self._quarterly_series(facts, [OPEX_TAG]), self._annual_series(facts, [OPEX_TAG])

        def _fill_op(op, rev, costs, gp, opex):
            for p, r in rev.items():
                if p in op:
                    continue
                if p in costs:
                    op[p] = r - costs[p]
                elif p in gp and p in opex:
                    op[p] = gp[p] - opex[p]
        _fill_op(op_income, {k: v for k, v in revenue.items() if k not in derived_revenue}, costs_q, gp_q, opex_q)
        _fill_op(op_annual, annual_revenue, costs_a, gp_a, opex_a)
        op_income.update(_derive_missing_q4(op_income, op_annual))
        other_income.update(_derive_missing_q4(other_income, self._annual_series(facts, OTHER_INCOME_TAGS)))
        ni_annual = self._annual_series(facts, NET_INCOME_TAGS)
        for k, v in self._annual_series(facts, NET_INCOME_FILL_TAGS).items():
            ni_annual.setdefault(k, v)
        net_income.update(_derive_missing_q4(net_income, ni_annual))
        eps.update(_derive_missing_q4(eps, self._annual_series(facts, EPS_TAGS)))
        # EPS fallback: net income / weighted average shares, for periods no EPS tag covers.
        wq, wa = self._quarterly_series(facts, WEIGHTED_SHARES_TAGS), self._annual_series(facts, WEIGHTED_SHARES_TAGS)
        for p, n in net_income.items():
            if p in eps:
                continue
            w = wq.get(p)
            if not w:  # derived Q4: approximate with the fiscal year's weighted share count
                w = next((v for k, v in wa.items() if 0 <= (date.fromisoformat(k) - date.fromisoformat(p)).days <= 3), None)
            if w:
                eps[p] = round(n / w, 2)
        derived_periods = set(derived_revenue.keys())

        pbt = self._quarterly_series(facts, PBT_TAGS)
        pbt.update(_derive_missing_q4(pbt, self._annual_series(facts, PBT_TAGS)))
        tax = self._quarterly_series(facts, TAX_TAGS)
        tax.update(_derive_missing_q4(tax, self._annual_series(facts, TAX_TAGS)))
        for p, t in tax.items():  # pre-tax profit when no PBT tag exists: net income + tax expense (approximation)
            if p not in pbt and p in net_income:
                pbt[p] = net_income[p] + t

        debt = self._instant_series(facts, DEBT_TAGS, merge_tags=True)
        # Split-debt fallback — only fills periods DEBT_TAGS has no data for (a period
        # DEBT_TAGS already covers keeps that value; a filer doesn't usually report
        # both a consolidated figure AND the split pair for the same date, so this
        # isn't double-counting, just picking whichever shape that period actually has).
        secured = self._instant_series(facts, [SPLIT_DEBT_TAGS[0]])
        unsecured = self._instant_series(facts, [SPLIT_DEBT_TAGS[1]])
        for end_str in set(secured) | set(unsecured):
            if end_str in debt:
                continue
            s, u = secured.get(end_str), unsecured.get(end_str)
            if s is not None or u is not None:
                debt[end_str] = (s or 0) + (u or 0)
        # Current-debt-only fallback (AAON case, see CURRENT_DEBT_FALLBACK_TAG) — same
        # "only when nothing else filled this period" rule as the split-debt fallback.
        current_debt = self._instant_series(facts, [CURRENT_DEBT_FALLBACK_TAG])
        for end_str, val in current_debt.items():
            if end_str not in debt and val is not None:
                debt[end_str] = val
        ranked = self._rank_tags(facts, FALLBACK_DEBT_TAGS)
        if ranked:
            for end_str, val in self._instant_series(facts, [ranked[0]]).items():
                if end_str not in debt and val is not None:
                    debt[end_str] = val
        interest = self._quarterly_series(facts, INTEREST_EXPENSE_TAGS)
        interest.update(_derive_missing_q4(interest, self._annual_series(facts, INTEREST_EXPENSE_TAGS)))
        cur_assets = self._instant_series(facts, CURRENT_ASSETS_TAGS)
        inventory = self._instant_series(facts, INVENTORY_TAGS)
        cfo = self._cfo_quarterly(facts)
        cash = self._instant_series(facts, CASH_TAGS)
        equity = self._instant_series(facts, EQUITY_TAGS)
        assets = self._instant_series(facts, ASSETS_TAGS)
        current_liab = self._instant_series(facts, CURRENT_LIABILITIES_TAGS)
        shares_outstanding = self._instant_series(facts, SHARES_OUTSTANDING_TAGS, prefer="first")
        cover_page_shares = self._cover_page_shares_series(facts)

        periods = sorted(set(net_income) | set(op_income)) if pre_revenue else sorted(revenue.keys())  # full history
        latest_period = periods[-1] if periods else None
        for end_str in periods:
            end = date.fromisoformat(end_str)
            rev = revenue.get(end_str)
            op = op_income.get(end_str)
            ni = net_income.get(end_str)
            e = eps.get(end_str)
            other_income_val = other_income.get(end_str)
            opm_pct = round(op / rev * 100, 2) if rev and op is not None else None

            prior_key = next((k for k in revenue if abs((date.fromisoformat(k) - (end - timedelta(days=365))).days) <= 10), None)
            prior_rev = revenue.get(prior_key) if prior_key else None
            # guard the actual denominator (prior_rev), not rev — a company that
            # reported $0 revenue in the year-ago quarter (pre-revenue, spin-off)
            # would otherwise divide by zero here
            growth_pct = safe_growth_pct(rev, prior_rev)

            debt_val = debt.get(end_str)
            cash_val = cash.get(end_str)
            net_debt_val = (debt_val - cash_val) if debt_val is not None and cash_val is not None else None

            pbt_val = pbt.get(end_str)
            tax_val = tax.get(end_str)
            tax_pct = round(tax_val / pbt_val * 100, 1) if pbt_val and tax_val is not None else None

            # Shares outstanding is an instant (point-in-time) fact available at
            # EVERY period end, not just the latest — real gap found live: it used
            # to only get captured on the symbol's single latest period (same
            # limitation bse_source.py's book_value_per_share/equity_share_capital
            # had), which meant there was never more than one data point per
            # symbol — nowhere near enough to ever detect a real stock split (needs
            # at least 2 points to compute a ratio). Pulled out of the latest-only
            # block below so every period gets it when SEC actually reported one.
            shares_val = shares_outstanding.get(end_str)
            if shares_val is None and cover_page_shares:
                # Fall back to the nearest cover-page filing within 50 days after
                # this quarter's end (SEC's filing deadline is 40-45 days after
                # quarter end for most filers) — covers filers like NVIDIA that
                # never tag the balance-sheet fact on a 10-Q, only on the 10-K.
                cutoff = (end + timedelta(days=50)).isoformat()
                candidates = [v for d, v in cover_page_shares if end_str <= d <= cutoff]
                if candidates:
                    shares_val = candidates[0]

            # Ratios (ROE/ROCE/D-E) are trailing stats, stored only on the latest
            # period — same convention mock data uses. TTM = sum of this and the 3
            # periods before it; needs 4 consecutive quarters, so skip if we don't
            # have that much history yet for this symbol. P/E stays NULL here — it
            # needs a current price, which this source doesn't provide (Tiingo does).
            roe_pct = roce_pct = debt_equity = book_value_per_share = None
            if end_str == latest_period:
                idx = periods.index(end_str)
                if idx >= 3:
                    ttm_periods = periods[idx - 3: idx + 1]
                    ni_vals = [net_income.get(p) for p in ttm_periods]
                    op_vals = [op_income.get(p) for p in ttm_periods]
                    equity_val = equity.get(end_str)
                    assets_val = assets.get(end_str)
                    cur_liab_val = current_liab.get(end_str)

                    if all(v is not None for v in ni_vals) and equity_val:
                        roe_pct = round(sum(ni_vals) / equity_val * 100, 1)
                    if all(v is not None for v in op_vals) and assets_val is not None and cur_liab_val is not None:
                        denom = assets_val - cur_liab_val
                        if denom:
                            roce_pct = round(sum(op_vals) / denom * 100, 1)
                    if debt_val is not None and equity_val:
                        debt_equity = round(debt_val / equity_val, 2)

                # Book value per share = equity / shares outstanding — simpler than
                # bse_source.py's version since SEC EDGAR reports shares outstanding
                # directly (no crores/face-value unit conversion needed for India).
                # shares_val (now captured every period, see above) is also persisted
                # on its own (shares_outstanding column) — analytics.py divides
                # institutional_holdings.total_shares_held by it at read time to get
                # institutional ownership %, the same pattern already used for
                # Layer 2/4 sector benchmarking.
                equity_val = equity.get(end_str)
                if equity_val and shares_val:
                    book_value_per_share = round(equity_val / shares_val, 2)

            conn.execute(
                """INSERT INTO quarterly_data
                (symbol, period_end_date, quarter_label, source, revenue_cr, operating_profit_cr,
                 opm_pct, other_income_cr, net_profit_cr, eps, revenue_growth_pct, total_debt_cr, net_debt_cr, cash_cr,
                 roe_pct, roce_pct, debt_equity, pbt_cr, tax_pct, book_value_per_share, shares_outstanding,
                 finance_cost_cr, current_assets_cr, current_liabilities_cr, inventory_cr, cfo_cr)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, period_end_date) DO UPDATE SET
                    quarter_label=excluded.quarter_label, source=excluded.source,
                    revenue_cr=excluded.revenue_cr, operating_profit_cr=excluded.operating_profit_cr,
                    opm_pct=excluded.opm_pct, net_profit_cr=excluded.net_profit_cr, eps=excluded.eps,
                    revenue_growth_pct=excluded.revenue_growth_pct, total_debt_cr=excluded.total_debt_cr,
                    net_debt_cr=excluded.net_debt_cr, cash_cr=excluded.cash_cr,
                    roe_pct=excluded.roe_pct, roce_pct=excluded.roce_pct, debt_equity=excluded.debt_equity,
                    pbt_cr=excluded.pbt_cr, tax_pct=excluded.tax_pct, book_value_per_share=excluded.book_value_per_share,
                    shares_outstanding=excluded.shares_outstanding,
                    finance_cost_cr=COALESCE(excluded.finance_cost_cr, finance_cost_cr),
                    current_assets_cr=COALESCE(excluded.current_assets_cr, current_assets_cr),
                    current_liabilities_cr=COALESCE(excluded.current_liabilities_cr, current_liabilities_cr),
                    inventory_cr=COALESCE(excluded.inventory_cr, inventory_cr),
                    cfo_cr=COALESCE(excluded.cfo_cr, cfo_cr),
                    other_income_cr=COALESCE(excluded.other_income_cr, other_income_cr)
                    -- deliberately NOT overwritten: guidance_vs_delivered (owned by finnhub_source.py).
                    -- INSERT OR REPLACE used to blindly wipe it back to NULL on every sec_edgar re-run —
                    -- same bug class confirmed live and fixed in bse_source.py this session.
                    -- other_income_cr is COALESCEd rather than blindly overwritten like the rest of this
                    -- source's own fields — this field is brand new, a re-run against a symbol whose
                    -- companyfacts happens to have dropped the tag for some reason shouldn't null out an
                    -- already-captured value.""",
                (sym, end_str, f"Q ending {end_str}" + (" (derived)" if end_str in derived_periods else ""), "sec_edgar",
                 rev / 1e6 if rev else None, op / 1e6 if op else None, opm_pct,
                 other_income_val / 1e6 if other_income_val is not None else None,
                 ni / 1e6 if ni else None, e, growth_pct,
                 debt_val / 1e6 if debt_val is not None else None,
                 net_debt_val / 1e6 if net_debt_val is not None else None,
                 cash_val / 1e6 if cash_val is not None else None,
                 roe_pct, roce_pct, debt_equity,
                 pbt_val / 1e6 if pbt_val is not None else None, tax_pct, book_value_per_share, shares_val,
                 interest[end_str] / 1e6 if end_str in interest else None,
                 cur_assets[end_str] / 1e6 if end_str in cur_assets else None,
                 current_liab[end_str] / 1e6 if end_str in current_liab else None,
                 inventory[end_str] / 1e6 if end_str in inventory else None,
                 cfo[end_str] / 1e6 if end_str in cfo else None),
            )
            rows_written += 1

        conn.execute("DELETE FROM quarterly_data WHERE symbol = ? AND source = 'mock'", (sym,))
        conn.commit()
        return rows_written

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("SEC EDGAR doesn't provide price data — use Tiingo for US prices.")
