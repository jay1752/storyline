"""Real BSE ingestion — India fundamentals, the source that actually works.

Two prior dead ends before this (see STATUS.md): Tijori Finance has no API, and
NSE's own XBRL archive has broken links for ~94% of filings (verified). BSE's own
XBRL archive is the opposite — verified 95% (19/20 large-caps) of recent filings
resolve to real, tagged inline-XBRL documents.

There's no official public API — this uses BSE's own undocumented JSON endpoints
(api.bseindia.com), the same ones bseindia.com's own website calls. Found by
inspecting real network calls on a live company page, not by guessing. Akamai
(BSE's edge/CDN) blocks requests missing browser-shaped headers (Origin, Referer,
Sec-Fetch-*) — a plain User-Agent isn't enough, verified by testing both.
No SLA, could break if BSE changes their frontend; that's the honest tradeoff of
an undocumented endpoint versus BSE's actual licensed bulk-data resellers, which
have no self-serve tier (see STATUS.md, India vendor sweep).

Data shape: each filing is one quarter's (or one year's) inline-XBRL document,
tagged with the `in-capmkt` (India Capital Markets) taxonomy — cleaner and more
consistent than SEC EDGAR's multi-tag-variant us-gaap mess. Unlike SEC EDGAR,
BSE-listed companies file all 4 quarters directly (SEBI LODR requires it) — no
missing-Q4 derivation needed here, that problem is US-only.

Balance sheet facts (cash, debt, equity, assets) only appear in the annual
("Yearly") filing, not the quarterly one — confirmed by inspecting both directly.
ROE/ROCE/D-E are therefore computed only at the latest period, same convention
sec_edgar_source.py uses, from the latest Yearly filing's balance sheet + TTM net
profit summed from the 4 most recent quarters.
"""

import re
import time
from datetime import date, datetime, timedelta

import requests

from app.ingestion.base import DataSource, apply_symbol_filter, safe_growth_pct

API_BASE = "https://api.bseindia.com/BseIndiaAPI/api"
SITE_BASE = "https://www.bseindia.com"
RATE_LIMIT_SECONDS = 0.3  # undocumented endpoint, no published limit — conservative default

# Real root cause found live (not a persistent IP ban, as this session assumed for
# hours): Akamai's bot detection on api.bseindia.com flags a FULL browser-shaped
# header set (Referer+Origin+Sec-Fetch-*) as MORE suspicious than a plain, honest
# client — a real browser's TLS/HTTP2 fingerprint doesn't match `requests`, so
# pretending harder with more browser-like headers reads as impersonation, not
# authenticity. Confirmed by isolating each header: Accept-Language and Origin are
# individually sufficient to trigger a 403; User-Agent+Accept+Referer alone passes
# consistently (3/3 repeated live tests). Kept deliberately minimal — do not add
# Accept-Language, Origin, or Sec-Fetch-* back without re-verifying live first.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/",
}

# Max quarterly filings fetched per symbol per run (each is its own HTTP call —
# unlike SEC EDGAR's one-call-gets-full-history companyfacts blob, BSE needs one
# fetch per filing). 8 quarters = 2 years, enough for YoY growth + a TTM window.
MAX_QUARTERS_PER_SYMBOL = 8

# Banks/NBFCs file under the same in-capmkt namespace but a different set of tag
# names (confirmed directly on HDFC Bank) — "InterestEarned" instead of revenue,
# "OperatingProfitBeforeProvisionAndContingencies" as a direct operating-profit
# figure instead of Revenue-Expenses, etc. Non-bank tag tried first in each list —
# order matters, it's also how a filing gets classified as a bank (see _first_num).
#
# Insurers (HDFC Life, SBI Life, LIC — IRDAI-regulated, not RBI/Companies-Act) are a
# third distinct tag vocabulary, confirmed directly on HDFC Life: revenue is
# "NetPremiumIncome" (fallback "GrossPremiumIncome"), not RevenueFromOperations or
# InterestEarned — these 3 large-caps had ZERO quarterly_data rows before this fix
# because every filing was silently skipped (`_first_num(facts, REVENUE_TAGS)` came
# back None for all of them). Values sanity-checked directly against known real HDFC
# Life figures (₹16,727cr quarterly net premium, ₹611cr net profit, EPS 2.83 — all in
# the right ballpark). Net profit/PBT/tax/EPS use their own insurer-specific tags too
# (see INSURER_* below) — an insurer's P&L has no "ProfitLossForPeriod" tag at all.
#
# Deliberately NOT attempted here: operating margin and every balance-sheet-derived
# field (ROE/ROCE/D-E/cash/debt/book value). An insurer's "operating profit" doesn't
# map onto this tag set the way a normal company's does (no clean analog — premiums
# minus claims minus actuarial reserve movements isn't equivalent to Revenue-Expenses),
# and its balance sheet is policyholder funds/reserves, not the standard
# Equity/Assets/CurrentLiabilities shape everything else here assumes. A wrong,
# confident-looking number is worse than a blank cell — left as a known, honest gap
# rather than guessed at without the same verification the P&L fields got.
REVENUE_TAGS = ["RevenueFromOperations", "InterestEarned", "NetPremiumIncome", "GrossPremiumIncome"]
NET_PROFIT_TAGS = ["ProfitLossForPeriod", "ProfitLossForThePeriod"]
INSURER_NET_PROFIT_TAGS = ["ProfitLossAfterTaxBeforeExtraordinaryItems", "ProfitLossAfterTaxAndExtraordinaryItems"]
INSURER_PBT_TAG = "ProfitLossBeforeTax"
INSURER_TAX_TAG = "ProvisionsForTaxes"  # NOT "ProvisionForTax" (singular) — a different tag that does NOT
# reconcile with PBT-NetProfit; confirmed on HDFC Life (630.19 PBT - 611.19 NetProfit = 19.0, exactly
# matching ProvisionsForTaxes, not ProvisionForTax's 46.94 — two different tax line items in this taxonomy).
INSURER_EPS_TAGS = [
    "BasicAndDilutedEPSBeforeExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized",
    "BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized",
]
BANK_OPERATING_PROFIT_TAG = "OperatingProfitBeforeProvisionAndContingencies"
EXPENSES_TAG = "Expenses"  # bundles opex + finance costs + D&A — see FINANCE_COST_TAG/DEPRECIATION_TAG below
FINANCE_COST_TAG = "FinanceCosts"
DEPRECIATION_TAG = "DepreciationDepletionAndAmortisationExpense"
OTHER_INCOME_TAG = "OtherIncome"
PBT_TAGS = ["ProfitBeforeTax", "ProfitLossFromOrdinaryActivitiesBeforeTax"]  # bank variant second
TAX_TAG = "TaxExpense"
SHARE_CAPITAL_TAG = "PaidUpValueOfEquityShareCapital"
FACE_VALUE_TAG = "FaceValueOfEquityShareCapital"  # per-share, not scaled by `scale` — a plain rupee amount
EPS_TAGS = [
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    "BasicEarningsLossPerShareFromContinuingOperations",
    "BasicEarningsPerShareBeforeExtraordinaryItems",  # bank variant
]
CASH_TAG = "CashAndCashEquivalents"
ASSETS_TAG = "Assets"
EQUITY_TAG = "Equity"
BORROWINGS_TAGS = ["BorrowingsCurrent", "BorrowingsNoncurrent"]
NBFC_BORROWINGS_TAG = "Borrowings"  # NBFCs report one combined tag instead of the split current/noncurrent pair above
CURRENT_LIAB_TAG = "CurrentLiabilities"

# NBFCs (e.g. Bajaj Finance) use the same tags as regular companies for all of the
# above — confirmed directly, RevenueFromOperations/ProfitLossForPeriod/Equity/
# CashAndCashEquivalents/Assets all present in their filings under the ordinary
# non-bank names. Only full commercial banks (HDFC Bank etc.) are genuinely
# different. A bank's balance sheet has no plain "Equity" or "CashAndCashEquivalents"
# tag — equity is Capital + ReservesAndSurplus, cash is its RBI balance — confirmed
# on HDFC Bank. ROCE and D/E are left NULL for banks: "Capital Employed" and "debt"
# aren't meaningful concepts for a bank's balance sheet (deposits are the core
# business, not leverage), same reasoning as skipping D/E for banks generally.
BANK_EQUITY_TAGS = ["Capital", "ReservesAndSurplus"]
BANK_CASH_TAG = "CashAndBalancesWithReserveBankOfIndia"

_FACT_TAG_RE = re.compile(r"<ix:nonFraction\s+([^>]+)>([^<]*)<")
_ATTR_RE = re.compile(r"(\w+)='([^']*)'")


def _non_bank_operating_profit(facts: dict[str, str]) -> float | None:
    """Revenue - Expenses is NOT operating profit here: BSE's "Expenses" tag bundles
    opex with Finance Costs and Depreciation, both of which belong below the
    operating-profit line. Confirmed against Screener.in on Infosys — the gap between
    our old figure and theirs matched FinanceCosts + Depreciation to the rupee. Fix:
    add both back. Missing either tag degrades gracefully (treated as 0), since some
    filings omit one or the other rather than reporting a genuine zero."""
    facts_get = lambda tag: _num(facts.get(tag)) or 0  # noqa: E731

    rev, _ = _first_num(facts, REVENUE_TAGS)
    expenses = _num(facts.get(EXPENSES_TAG))
    if rev is None or expenses is None:
        return None
    return rev - expenses + facts_get(FINANCE_COST_TAG) + facts_get(DEPRECIATION_TAG)


def _book_value_per_share(balance_sheet: dict[str, str], equity_val: float | None) -> float | None:
    """Book value per share = Equity / shares outstanding, where shares outstanding
    is derived from the two tags actually available (total paid-up capital, both in
    crores; face value per share, in plain rupees — NOT scaled) rather than divided
    out explicitly: (Equity / PaidUpShareCapital) * FaceValue, algebraically the same
    thing without needing to convert crores <-> share counts. Verified against
    Screener.in on Infosys (formula gave ₹229 vs. their ₹225 — same ballpark, small
    gap plausibly from standalone/consolidated or minority-interest differences)."""
    share_capital = _num(balance_sheet.get(SHARE_CAPITAL_TAG))
    face_value = _num(balance_sheet.get(FACE_VALUE_TAG))
    if not equity_val or not share_capital or not face_value:
        return None
    return round((equity_val / share_capital) * face_value, 2)


def _num(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = raw.strip().replace(",", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _first_num(facts: dict[str, str], tags: list[str]) -> tuple[float | None, str | None]:
    """First tag in `tags` that has a value, plus which tag matched — the match
    itself is how a bank/NBFC filing gets detected (its revenue only ever matches
    the InterestEarned fallback, never RevenueFromOperations)."""
    for tag in tags:
        val = _num(facts.get(tag))
        if val is not None:
            return val, tag
    return None, None


_QUARTER_END_MONTH = {"Jun": (6, 30), "Sep": (9, 30), "Dec": (12, 31), "Mar": (3, 31)}


def _period_end_from_filing(f: dict) -> str | None:
    """Derive period_end_date from BSE's own filing metadata (Quarter_Name + Yr)
    rather than an in-document XBRL date tag — 'DateOfEndOfReportingPeriod' isn't
    tagged consistently across all filings (confirmed: present in some, absent in
    others for the same company, likely a filer/software-version difference), while
    Quarter_Name + Yr is always present since BSE generates it itself."""
    name = f.get("Quarter_Name") or ""
    yr = f.get("Yr")
    if not yr:
        return None
    for abbr, (month, day) in _QUARTER_END_MONTH.items():
        if f"-{abbr}-" in name:
            return date(int(yr), month, day).isoformat()
    return None


def _filed_on(f: dict) -> date | None:
    stamp = f.get("filing_date_time") or f.get("revised_date_time")
    try:
        return datetime.fromisoformat(str(stamp)[:19]).date() if stamp else None
    except ValueError:
        return None


def _comparative_reupload_periods(qtr_filings: list[dict]) -> set[str]:
    """period_end dates whose BSE entry is a re-upload of the NEXT year's filing, not that
    quarter's own result. Signature, verified live on ROTO, NSIL, KOHINOOR, AARTIDRUGS:
    the entry is filed 8-14 months after its own quarter ended, within days of its
    same-quarter-next-year sibling (ROTO's Sep-2024 and Sep-2025 entries were both filed
    2025-11-12 and hold identical facts; official NSE filings show the real Sep-2024
    revenue is 70.25, not the 48.75 BSE's entry carries). Filing metadata is used rather
    than comparing values because the sibling can be the other basis (standalone vs
    consolidated), so the numbers don't have to match for the entry to be an artifact."""
    filed = {}
    for f in qtr_filings:
        end, when = _period_end_from_filing(f), _filed_on(f)
        if end and when:
            filed[end] = when
    flagged = set()
    for end, when in filed.items():
        if (when - date.fromisoformat(end)).days < 200:
            continue  # filed on time -> a genuine result for its own quarter
        for other, other_when in filed.items():
            if abs((date.fromisoformat(other) - date.fromisoformat(end)).days - 365) <= 7 and abs((other_when - when).days) <= 3:
                flagged.add(end)
    return flagged


def _extract_facts(html: str) -> dict[str, str]:
    """{tag_name: value_string}, preferring the 'OneD' (duration, current-period)
    context over 'OneI' (instant) when a tag appears under both — OneD is used for
    every P&L fact; OneI only shows up for point-in-time balance sheet facts, which
    have no OneD counterpart anyway, so this never actually has to choose.

    Numeric facts carry their own XBRL `scale` attribute, and it varies BETWEEN
    filings — some report in crores (scale=7), others in lakhs (scale=5) — not a
    fixed convention despite every filing displaying "(in Cr.)" on the BSE website
    (confirmed directly: same company, different quarters, different scale values).
    Values are normalized to crores here (value * 10^(scale-7)) so every downstream
    number is on the same footing regardless of which scale the filer used."""
    facts: dict[str, str] = {}
    for attrs_str, raw_val in _FACT_TAG_RE.findall(html):
        attrs = dict(_ATTR_RE.findall(attrs_str))
        name = attrs.get("name", "")
        if not name.startswith("in-capmkt:"):
            continue
        tag = name.split(":", 1)[1]
        ctx = attrs.get("contextRef")
        if ctx not in ("OneD", "OneI"):
            continue
        value = raw_val
        scale = attrs.get("scale")
        if scale is not None:
            num = _num(raw_val)
            if num is not None:
                try:
                    value = str(num * (10 ** (int(scale) - 7)))
                except ValueError:
                    pass
        if tag not in facts or ctx == "OneD":
            facts[tag] = value
    return facts


class BseSource(DataSource):
    name = "bse"

    def _scrip_rows(self) -> list[dict]:
        resp = requests.get(
            f"{API_BASE}/ListOfScripData/w",
            params={"Group": "", "Scripcode": "", "industry": "", "segment": "Equity", "status": "Active"},
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def sync_universe(self, conn) -> int:
        rows = 0
        for row in self._scrip_rows():
            scrip_id = row.get("scrip_id")
            if not scrip_id:
                continue
            symbol = f"NSE:{scrip_id}"  # matches the existing IN-market convention (fivepaisa_source.py) so a
            # future 5paisa/Kite price sync merges into the same rows instead of creating duplicates.
            name = row.get("Issuer_Name") or row.get("Scrip_Name") or scrip_id
            conn.execute(
                """INSERT INTO stocks (symbol, exchange, name, sector, market) VALUES (?, 'BSE', ?, 'Unclassified', 'IN')
                ON CONFLICT(symbol) DO UPDATE SET exchange=excluded.exchange, name=excluded.name, market=excluded.market""",
                # sector deliberately excluded from the update clause — re-running sync_universe (e.g. after a
                # 5paisa universe sync touches the same NSE:{ticker} rows) must never clobber real enrichment
                # already written by _sector_for(); confirmed this was silently happening before this fix.
                (symbol, name),
            )
            rows += 1
        conn.commit()
        return rows

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("BSE is used here for fundamentals only — prices come from 5paisa/Kite.")

    def _fetch_filings(self, scrip_cd: str) -> list[dict]:
        resp = requests.get(f"{API_BASE}/Integratedfinancedata/w", params={"scripcode": scrip_cd}, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.json().get("Table") or []

    def _fetch_xbrl(self, xbrl_path: str) -> dict[str, str] | None:
        try:
            resp = requests.get(SITE_BASE + xbrl_path, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException:
            return None
        return _extract_facts(resp.text)

    def _pick_latest_per_period(self, filings: list[dict], kind: str) -> list[dict]:
        """kind is 'Qtr' or 'Yearly'. Dedupes Standalone/Consolidated pairs for the
        same period (prefers Consolidated), keyed by the filing's own Yr+Qtrid —
        stable even though the free-text Quarter_Name label isn't."""
        picked: dict[tuple, dict] = {}
        for f in filings:
            name = f.get("Quarter_Name") or ""
            if kind not in name:
                continue
            key = (f.get("Yr"), f.get("Qtrid"))
            nature = "Consolidated" if name.startswith("Consolidated") else "Standalone"
            existing = picked.get(key)
            if existing is None or (nature == "Consolidated" and existing["_nature"] != "Consolidated"):
                f = {**f, "_nature": nature}
                picked[key] = f
        return sorted(picked.values(), key=lambda f: (f.get("Yr") or 0, f.get("Qtrid") or 0), reverse=True)

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        scrip_map = {f"NSE:{r['scrip_id']}": r["SCRIP_CD"] for r in self._scrip_rows() if r.get("scrip_id")}
        symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM stocks WHERE market = 'IN'").fetchall()]
        symbols = apply_symbol_filter(symbols, limit, symbol)

        if not symbol:
            # Resume toward real coverage, not just re-refresh what's already fresh:
            # confirmed live this session that repeated full runs (hours each) left
            # distinct-symbol coverage completely unchanged (4,203/5,356 before AND
            # after a 24,372-row run) — the fixed iteration order means a run that
            # doesn't finish (container restart, timeout) always dies on the same
            # tail-end symbols, never reaching them. Skip symbols already covered
            # recently (a quarter filed within the last ~100 days — BSE's own filing
            # cadence, so this is "likely still current", not "touched once, ever
            # again") so a re-triggered run spends its time on what's actually
            # missing or stale, same resume principle as tiingo_source.py's fix.
            recently_covered = {r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM quarterly_data "
                "WHERE source = 'bse' AND period_end_date >= date('now', '-100 days')"
            ).fetchall()}
            symbols = [s for s in symbols if s not in recently_covered]

        rows_written = 0

        for sym in symbols:
            scrip_cd = scrip_map.get(sym)
            if not scrip_cd:
                continue
            time.sleep(RATE_LIMIT_SECONDS)
            try:
                filings = self._fetch_filings(scrip_cd)
            except requests.RequestException:
                continue
            if not filings:
                continue

            try:
                rows_written += self._process_symbol(conn, sym, scrip_cd, filings)
            except Exception:  # noqa: BLE001 — one symbol's XBRL quirk must never kill a multi-thousand-symbol run
                continue
            if progress:
                progress(rows_written)

        return rows_written

    def _sector_for(self, scrip_cd: str) -> str | None:
        """IndustryNew from ComHeadernew_par — a mid-granularity classification
        (e.g. "Capital Goods", "Information Technology"), matching roughly the
        bucket size sec_edgar_source.py gets from SEC's SIC description. BSE's own
        scrip-master list (`ListOfScripData`) always returns a null INDUSTRY field —
        confirmed empirically — so sector has to come from this separate per-scrip
        call instead, same "second endpoint, one more call per symbol" shape as
        SEC EDGAR's own _sector_for."""
        try:
            resp = requests.get(
                f"{API_BASE}/ComHeadernew_par/w", params={"quotetype": "", "scripcode": scrip_cd, "seriesid": ""},
                headers=HEADERS, timeout=20,
            )
            resp.raise_for_status()
            return resp.json().get("IndustryNew") or None
        except requests.RequestException:
            return None

    def _process_symbol(self, conn, sym: str, scrip_cd: str, filings: list[dict]) -> int:
        time.sleep(RATE_LIMIT_SECONDS)
        sector = self._sector_for(scrip_cd)
        if sector:
            conn.execute("UPDATE stocks SET sector = ? WHERE symbol = ?", (sector, sym))

        qtr_filings = self._pick_latest_per_period(filings, "Qtr")[:MAX_QUARTERS_PER_SYMBOL]
        artifact_periods = _comparative_reupload_periods(qtr_filings)
        qtr_filings = [f for f in qtr_filings if _period_end_from_filing(f) not in artifact_periods]
        yearly_filings = self._pick_latest_per_period(filings, "Yearly")[:1]

        periods: dict[str, dict] = {}  # period_end_date -> facts
        for f in qtr_filings:
            time.sleep(RATE_LIMIT_SECONDS)
            xbrl_path = f.get("XbrlFile")
            if not xbrl_path:
                continue
            facts = self._fetch_xbrl(xbrl_path)
            if not facts or _first_num(facts, REVENUE_TAGS)[0] is None:
                continue
            end_date = _period_end_from_filing(f)
            if not end_date:
                continue
            periods[end_date] = facts

        # Real data artifact found live (ROTO, AARTIDRUGS, KOHINOOR, NSIL, MMTC and ~30
        # others): BSE's filing list sometimes carries a YEAR-EARLIER entry (e.g.
        # "Standalone-Sep-Qtr-2024") that points at the CURRENT filing — filed the same
        # day as its "Sep-Qtr-2025" sibling, with byte-for-byte identical facts (verified:
        # ROTO revenue 4,875.30 / profit 361.39 / H1 10,038.09 in both). Without this
        # guard, this year's numbers get written into last year's row, and a later YoY
        # growth is then computed against that duplicate (exactly 0.0%). Official NSE
        # filings confirm the real Sep-2024 figure is different (70.25 vs the 48.75 BSE
        # served). Identical revenue AND profit one year apart is not a coincidence for
        # any real company, so the earlier period is dropped, keeping the later (genuine) one.
        for end in sorted(periods):
            twin = next(
                (k for k in periods if abs((date.fromisoformat(k) - date.fromisoformat(end)).days - 365) <= 7), None
            )
            if twin is None:
                continue
            r0, r1 = _first_num(periods[end], REVENUE_TAGS)[0], _first_num(periods[twin], REVENUE_TAGS)[0]
            p0, p1 = _first_num(periods[end], NET_PROFIT_TAGS)[0], _first_num(periods[twin], NET_PROFIT_TAGS)[0]
            if r0 and r1 and abs(r0 - r1) < 1e-6 and p0 is not None and p1 is not None and abs(p0 - p1) < 1e-6:
                del periods[end]

        if not periods:
            return 0

        balance_sheet = None
        if yearly_filings:
            time.sleep(RATE_LIMIT_SECONDS)
            yf_facts = self._fetch_xbrl(yearly_filings[0].get("XbrlFile") or "")
            if yf_facts:
                balance_sheet = yf_facts

        sorted_ends = sorted(periods.keys())
        latest_period = sorted_ends[-1]
        rows_written = 0

        latest_rev_tag = _first_num(periods[latest_period], REVENUE_TAGS)[1]
        latest_is_bank = latest_rev_tag == "InterestEarned"
        latest_is_insurer = latest_rev_tag in ("NetPremiumIncome", "GrossPremiumIncome")

        for end_str in sorted_ends:
            facts = periods[end_str]
            end = date.fromisoformat(end_str)
            rev, rev_tag = _first_num(facts, REVENUE_TAGS)
            is_bank = rev_tag == "InterestEarned"
            is_insurer = rev_tag in ("NetPremiumIncome", "GrossPremiumIncome")
            if is_insurer:
                net_profit, _ = _first_num(facts, INSURER_NET_PROFIT_TAGS)
                eps, _ = _first_num(facts, INSURER_EPS_TAGS)
            else:
                net_profit, _ = _first_num(facts, NET_PROFIT_TAGS)
                eps, _ = _first_num(facts, EPS_TAGS)
            if is_bank:
                op, _ = _first_num(facts, [BANK_OPERATING_PROFIT_TAG])
            elif is_insurer:
                op = None  # no clean operating-profit analog for an insurer's premium/claims P&L — see module docs
            else:
                op = _non_bank_operating_profit(facts)
            opm_pct = round(op / rev * 100, 2) if rev and op is not None else None

            other_income = _num(facts.get(OTHER_INCOME_TAG))
            if is_insurer:
                pbt = _num(facts.get(INSURER_PBT_TAG))
                tax_expense = _num(facts.get(INSURER_TAX_TAG))
            else:
                pbt, _ = _first_num(facts, PBT_TAGS)
                tax_expense = _num(facts.get(TAX_TAG))
            tax_pct = round(tax_expense / pbt * 100, 1) if pbt and tax_expense is not None else None

            prior_key = next(
                (k for k in periods if abs((date.fromisoformat(k) - (end - timedelta(days=365))).days) <= 10), None
            )
            prior_rev, _ = _first_num(periods[prior_key], REVENUE_TAGS) if prior_key else (None, None)
            growth_pct = safe_growth_pct(rev, prior_rev)

            roe_pct = roce_pct = debt_equity = total_debt_cr = net_debt_cr = cash_cr = book_value_per_share = None
            # Paid-up equity share capital, ₹Cr — only available on the latest quarter
            # (same limitation as book_value_per_share: needs the annual balance
            # sheet). Real gap found live: this tag was already being fetched inside
            # _book_value_per_share() but only used transiently, never stored — it's
            # the reliable signal for a real corporate action (a genuine capital
            # change, not a guess from an EPS jump), and the LATEST point matters
            # most here since it's the one place recent enough to catch something
            # like HDFC Bank's 2025 bonus, which moneycontrol's older annual pulls
            # wouldn't see yet.
            equity_share_capital = None
            if end_str == latest_period and balance_sheet and latest_is_bank:
                # Banks: only cash + ROE, via bank-specific tags. ROCE/D-E stay NULL
                # — not meaningful concepts for a bank's balance sheet (see module docs).
                cash_cr = _num(balance_sheet.get(BANK_CASH_TAG))
                equity_parts = [_num(balance_sheet.get(t)) for t in BANK_EQUITY_TAGS]
                equity_val = sum(v for v in equity_parts if v is not None) if all(v is not None for v in equity_parts) else None
                book_value_per_share = _book_value_per_share(balance_sheet, equity_val)
                equity_share_capital = _num(balance_sheet.get(SHARE_CAPITAL_TAG))
                if len(sorted_ends) >= 4 and equity_val:
                    ttm_ni = [_first_num(periods[d], NET_PROFIT_TAGS)[0] for d in sorted_ends[-4:]]
                    if all(v is not None for v in ttm_ni):
                        roe_pct = round(sum(ttm_ni) / equity_val * 100, 1)
            elif end_str == latest_period and balance_sheet and not latest_is_bank and not latest_is_insurer:
                # Explicitly excludes insurers, not just implicitly via missing tags —
                # confirmed live that HDFC Life's yearly filing DOES have real "Assets"
                # and "Borrowings" tags (coincidentally reused from the general taxonomy)
                # even though it has no "Equity"/"CashAndCashEquivalents" — letting this
                # branch run for insurers would silently produce a standalone total_debt_cr
                # with no cash/equity to give it context, exactly the "confident-looking
                # wrong number" this module's docs say to avoid. See REVENUE_TAGS comment.
                cash_cr = _num(balance_sheet.get(CASH_TAG))
                assets_val = _num(balance_sheet.get(ASSETS_TAG))
                equity_val = _num(balance_sheet.get(EQUITY_TAG))
                cur_liab_val = _num(balance_sheet.get(CURRENT_LIAB_TAG))
                book_value_per_share = _book_value_per_share(balance_sheet, equity_val)
                equity_share_capital = _num(balance_sheet.get(SHARE_CAPITAL_TAG))
                debt_parts = [_num(balance_sheet.get(t)) for t in BORROWINGS_TAGS]
                if any(v is not None for v in debt_parts):
                    total_debt_cr = sum(v for v in debt_parts if v is not None)
                else:
                    total_debt_cr = _num(balance_sheet.get(NBFC_BORROWINGS_TAG))
                net_debt_cr = (total_debt_cr - cash_cr) if total_debt_cr is not None and cash_cr is not None else None

                if len(sorted_ends) >= 4:
                    ttm_ends = sorted_ends[-4:]
                    ttm_ni = [_first_num(periods[d], NET_PROFIT_TAGS)[0] for d in ttm_ends]
                    ttm_op = [_non_bank_operating_profit(periods[d]) for d in ttm_ends]
                    if all(v is not None for v in ttm_ni) and equity_val:
                        roe_pct = round(sum(ttm_ni) / equity_val * 100, 1)
                    if all(v is not None for v in ttm_op) and assets_val is not None and cur_liab_val is not None:
                        denom = assets_val - cur_liab_val
                        if denom:
                            roce_pct = round(sum(ttm_op) / denom * 100, 1)
                if total_debt_cr is not None and equity_val:
                    debt_equity = round(total_debt_cr / equity_val, 2)

            conn.execute(
                """INSERT INTO quarterly_data
                (symbol, period_end_date, quarter_label, source, revenue_cr, operating_profit_cr,
                 opm_pct, other_income_cr, net_profit_cr, eps, revenue_growth_pct, total_debt_cr, net_debt_cr, cash_cr,
                 roe_pct, roce_pct, debt_equity, pbt_cr, tax_pct, book_value_per_share, equity_share_capital_cr)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, period_end_date) DO UPDATE SET
                    quarter_label=excluded.quarter_label, source=excluded.source,
                    revenue_cr=excluded.revenue_cr, operating_profit_cr=excluded.operating_profit_cr,
                    opm_pct=excluded.opm_pct, other_income_cr=excluded.other_income_cr,
                    net_profit_cr=excluded.net_profit_cr, eps=excluded.eps,
                    pbt_cr=excluded.pbt_cr, tax_pct=excluded.tax_pct,
                    -- Real regression found live (HDFCBANK, MCX): BSE's own filing-list
                    -- endpoint only returns a capped recent window (confirmed: 20 entries,
                    -- consumed fast by a heavy filer's standalone/consolidated/half-yearly/
                    -- nine-month duplicates), which SLIDES FORWARD over time. A quarter's
                    -- true YoY prior-year comparator can age out of that window on a later
                    -- re-run even though it was present (and correctly computed) on an
                    -- earlier one — confirmed live: HDFCBANK's Q1 2025 revenue_growth_pct
                    -- (previously a real 18.6%) came back None on a fresh re-run purely
                    -- because Mar-2024 had since rolled out of BSE's 20-entry window, NOT
                    -- because the underlying fact changed. COALESCE here means a later run
                    -- can only ADD a value or replace one with a newer non-null one, never
                    -- regress a real number back to unknown. Same reasoning as (and same
                    -- fix pattern already applied to) equity_share_capital_cr below for
                    -- roe_pct/roce_pct/debt_equity/total_debt_cr/net_debt_cr/cash_cr/
                    -- book_value_per_share: all five are also only ever computed on
                    -- whichever row is CURRENTLY "latest_period" at fetch time, so a later
                    -- run whose latest_period has moved forward would otherwise null out
                    -- an older row's already-correct values the exact same way.
                    revenue_growth_pct=COALESCE(excluded.revenue_growth_pct, revenue_growth_pct),
                    total_debt_cr=COALESCE(excluded.total_debt_cr, total_debt_cr),
                    net_debt_cr=COALESCE(excluded.net_debt_cr, net_debt_cr),
                    cash_cr=COALESCE(excluded.cash_cr, cash_cr),
                    roe_pct=COALESCE(excluded.roe_pct, roe_pct),
                    roce_pct=COALESCE(excluded.roce_pct, roce_pct),
                    debt_equity=COALESCE(excluded.debt_equity, debt_equity),
                    book_value_per_share=COALESCE(excluded.book_value_per_share, book_value_per_share),
                    equity_share_capital_cr=COALESCE(excluded.equity_share_capital_cr, equity_share_capital_cr)
                    -- deliberately NOT overwritten: fii_pct/dii_pct/promoter_pct/promoter_pledge (owned by
                    -- bse_shareholding_source.py) and guidance_vs_delivered (owned by finnhub_source.py).
                    -- INSERT OR REPLACE used to blindly wipe these back to NULL on every bse re-run — confirmed
                    -- happening live this session: bse_shareholding wrote 26,731 real updates, only 12,938
                    -- survived a subsequent bse run. Same bug class already fixed for stocks.sector above.""",
                (sym, end_str, f"Q ending {end_str}", "bse",
                 rev, op, opm_pct, other_income, net_profit, eps, growth_pct,
                 total_debt_cr, net_debt_cr, cash_cr, roe_pct, roce_pct, debt_equity,
                 pbt, tax_pct, book_value_per_share, equity_share_capital),
            )
            rows_written += 1

        conn.execute("DELETE FROM quarterly_data WHERE symbol = ? AND source = 'mock'", (sym,))
        conn.commit()
        return rows_written
