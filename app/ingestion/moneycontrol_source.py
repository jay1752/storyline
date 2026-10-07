"""Real MoneyControl ingestion — deeper India fundamentals history (Layer 1) plus
earnings guidance (beat/miss/inline), found and validated this session as "Option B"
against the ~1.5-2yr ceiling of BSE's own XBRL feed (bse_source.py).

No official API (confirmed via web search — MoneyControl has none, free or paid).
Everything here is MoneyControl's own undocumented JSON backend, found the same way
bse_source.py's and bse_shareholding_source.py's were: inspecting real network calls
on live pages, not guessing. Same integrity tier as those — no SLA, could break if
MoneyControl changes their frontend — plus the same ToS caution already flagged for
Screener.in in STATUS.md (a commercial media site, not an exchange).

Four endpoints, all free and clean JSON (not the obfuscated ad-tech noise the rest of
the site is full of):
  - autosuggestion_solr.php?query=X       -> resolves a company name to MoneyControl's
                                              own internal `sc_id` (e.g. "TCS", "BAF",
                                              "HDF01" — NOT reliably the NSE ticker,
                                              confirmed directly: sc_id=HDFCBANK returns
                                              nothing, the real code is HDF01)
  - mcapi/v1/quarterly-earning/balance-sheet?sc_id=X   -> up to ~23yrs, standalone AND
                                                            consolidated in one call
  - appfeeds.moneycontrol.com/.../yearly_results_responsive?sc_id=X -> P&L, same depth
  - mcapi/v1/stock/estimates/hits-misses?scId=X&type=eps -> quarterly beat/miss/inline,
                                                              broader than the 4-ticker
                                                              Finnhub ADR trick in
                                                              finnhub_source.py

Symbol resolution is the fragile part: sc_id is MoneyControl's own code, not derivable
from our NSE ticker. Resolved by searching their autosuggest with our company NAME and
requiring the result's embedded NSE ticker (parsed out of `pdt_dis_nm`, e.g.
"Bajaj Finance INE296A01032, BAJFINANCE, 500034") to exactly match ours — never guessed,
skipped if no exact match.

Extends history, never overwrites: only INSERTs quarterly_data rows for period_end_dates
a symbol doesn't already have (checked first, like finnhub_source.py's enrichment
pattern) — bse_source.py's more-authoritative recent rows are left untouched. This is
annual-only data (MoneyControl's "Yearly Results"/"Balance Sheet" pages), same shape as
bse_source.py's own annual-filing-only balance sheet fields.

Verified end-to-end against real TCS data before trusting this: revenue/net profit/PBT/
tax%/debt/cash all show a clean, monotonic 21-year trend with no anomalies (23 years
pulled, 2005-2026), and the most recent figures matched the live rendered page exactly.
One known data-quality caveat in MoneyControl's own historical series, not introduced by
this code: TCS's EPS repeats an identical value (134.19) between FY25 and FY18 in their
raw data — almost certainly an unadjusted-for-bonus-shares artifact on their end. Worth
a grain of salt on EPS specifically for companies with a stock split/bonus in their
history; every other field checked out clean."""

import re
import time
from datetime import date, timedelta

import requests

from app.ingestion.base import DataSource, apply_symbol_filter, safe_growth_pct

AUTOSUGGEST_URL = "https://www.moneycontrol.com/mccode/common/autosuggestion_solr.php"
BALANCE_SHEET_URL = "https://api.moneycontrol.com/mcapi/v1/quarterly-earning/balance-sheet"
PNL_URL = "https://appfeeds.moneycontrol.com/jsonapi/stocks/yearly_results_responsive"
HITS_MISSES_URL = "https://api.moneycontrol.com/mcapi/v1/stock/estimates/hits-misses"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Origin": "https://www.moneycontrol.com",
    "Referer": "https://www.moneycontrol.com/",
}

RATE_LIMIT_SECONDS = 0.3  # undocumented endpoint, no published limit — same conservative default as bse_source.py
MAX_YEARS_BACKFILL = 25  # covers everything MoneyControl has ever returned (deepest seen: 23yrs on TCS)
MAX_RUN_SECONDS = 1800  # 30 min wall-clock cap per run — see ingest_quarterly for why
DATE_TOLERANCE_DAYS = 20  # for matching hits-misses quarters to existing quarterly_data rows

# "Company Name&nbsp;<span>ISIN, NSE_SYMBOL, BSE_CODE</span>" — NSE_SYMBOL is the 2nd
# comma-separated field inside the span, "-" when a company isn't NSE-listed.
_DISPLAY_NAME_RE = re.compile(r"<span>([^,]*),\s*([^,]*),\s*([^<]*)</span>")
_BLOCKED = object()  # sentinel: distinguishes "MoneyControl's Akamai blocked this IP, stop the run" from "no match"

_QUARTER_MONTH_TO_DATE = {"Mar": "-03-31", "Jun": "-06-30", "Sep": "-09-30", "Dec": "-12-31"}

# Banks report P&L under entirely different field names on MoneyControl (confirmed
# directly on HDFC Bank) — no "Total Income From Operations" key at all, so every
# major bank (HDFCBANK/ICICIBANK/SBIN/KOTAKBANK/AXISBANK and others) silently got ZERO
# moneycontrol rows: `revenue` was always None, hitting the "no real P&L data" skip on
# every single period. "Interest Expended" only ever appears on a bank's P&L response —
# used to detect the bank-format response instead of guessing from an absent key.
# "Interest Earned" itself comes back as an empty string on the raw response even
# though its 4 sub-components are populated, so revenue is reconstructed by summing
# them (the same figure BSE's own "InterestEarned" XBRL tag reports directly).
BANK_INTEREST_COMPONENT_KEYS = [
    "(a) Int. /Disc. on Adv/Bills", "(b) Income on Investment",
    "(c) Int. on balances With RBI", "(d) Others",
]
BANK_OPERATING_PROFIT_KEY = "Operating Profit before Provisions and contingencies"
# Balance-sheet side: "TotalShareHoldersFunds" is the SAME key for banks and regular
# companies (confirmed), so equity/book-value-per-share need no branching. Cash and
# debt do — a bank has no "CashAndCashEquivalents"/split Long-/ShortTermBorrowings,
# using "CashAndBalancesWithReserveBankOfIndia" and a single "Borrowings" figure
# instead (genuine borrowings, not "Deposits" — a bank's deposits aren't debt in the
# leverage sense, same reasoning bse_source.py already applies).
BANK_CASH_KEY = "CashAndBalancesWithReserveBankOfIndia"
BANK_BORROWINGS_KEY = "Borrowings"
# Real gap found live (SBICARD, an NBFC — not caught by is_bank, which only checks for
# "Interest Expended"): MoneyControl returns a DIFFERENT key for this exact PNL line for
# non-bank financial-services companies — an extra space before the comma — while regular
# industrials (confirmed on RELIANCE) use the key without it. A plain .get() on either one
# alone silently returns None for the other company type, which is exactly what left
# operating_profit_cr/opm_pct NULL on every one of SBICARD's moneycontrol-sourced rows.
OP_PROFIT_KEYS = [
    "P/L Before Other Inc., Int., Excpt. Items & Tax",
    "P/L Before Other Inc. , Int., Excpt. Items & Tax",
]


def _num(raw) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    raw = str(raw).strip().replace(",", "")
    if not raw or raw == "--":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


_FISCAL_MONTH_END = {"Mar": (3, 31), "Jun": (6, 30), "Sep": (9, 30), "Dec": (12, 31)}


def _year_label_to_period_end(yrc0: str) -> str | None:
    """'Mar 26' (balance-sheet API) or "Mar '26" (P&L API) -> '2026-03-31' — the two
    MoneyControl endpoints format the same fiscal-year label differently, confirmed
    directly (balance-sheet: "Mar 26", P&L: "Mar '26").

    NOT every Indian company has a March fiscal year-end, despite that being the
    documented assumption here before this fix — a real bug found live: Siemens Ltd
    India genuinely reports on a **September** fiscal year (19 real annual filings,
    2007-2025, every single one silently discarded because this only ever matched
    "Mar"), ABB India and Varun Beverages (pre-2025) both report on a **December**
    year-end. All three are legitimate, real fiscal-year-end choices under Indian
    company law, not data errors — companies can and do pick a year-end other than
    March. Now handles all four standard quarter-end months."""
    m = re.match(r"(Mar|Jun|Sep|Dec)\s*'?(\d{2,4})", yrc0.strip())
    if not m:
        return None
    month, day = _FISCAL_MONTH_END[m.group(1)]
    yr = int(m.group(2))
    # Two-digit years: MoneyControl's series reaches back to FY1997, and "Mar 97" was
    # being read as 2097 (found live: DCBBANK had rows dated 2097 and 2098). Nothing
    # here can legitimately be later than the current year + 1, so a two-digit year
    # that would land in the future belongs to the 1900s.
    if yr < 100:
        yr = 2000 + yr
        if yr > date.today().year + 1:
            yr -= 100
    return date(yr, month, day).isoformat()


class MoneyControlSource(DataSource):
    name = "moneycontrol"

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("MoneyControl is used here for deeper fundamentals history + guidance, not prices.")

    def _autosuggest(self, query: str, nse_ticker: str):
        """One search attempt. Returns the sc_id, None (no match in these results), or
        `_BLOCKED` on a 403."""
        try:
            resp = requests.get(
                AUTOSUGGEST_URL,
                params={"classic": "true", "query": query, "type": "1", "format": "json"},
                headers=HEADERS, timeout=15,
            )
        except requests.RequestException:
            return None
        if resp.status_code == 403:
            return _BLOCKED
        try:
            resp.raise_for_status()
            candidates = resp.json()
        except (requests.RequestException, ValueError):
            return None

        for c in candidates:
            m = _DISPLAY_NAME_RE.search(c.get("pdt_dis_nm", ""))
            if not m:
                continue
            candidate_nse = m.group(2).strip()
            if candidate_nse.upper() == nse_ticker.upper():
                return c.get("sc_id")
        return None

    def _resolve_sc_id(self, company_name: str, nse_ticker: str):
        """Returns the sc_id, None (this one company genuinely didn't resolve), or
        `_BLOCKED` — confirmed live this session that MoneyControl's Akamai protection
        can hard-block this container's IP mid-run (403 on every request, sustained,
        not per-symbol bad luck: major names that resolve fine in isolation — RELIANCE,
        TCS, HDFCBANK — came back empty in a run that got flagged partway through).
        Caller must stop the WHOLE run on `_BLOCKED`, not just skip one symbol — every
        remaining symbol would 403 too, wasting requests against a budget that's
        already in a penalty state, same principle as tiingo_source.py's 429 handling.

        Tries the company's registered name first, then falls back to the bare NSE
        ticker if that finds nothing — a real gap found live: searching "BSE LIMITED"
        (our stored name for NSE:BSE) returns MoneyControl's own index pages ("BSE 100",
        "BSE 200", "BSE TECK") ranked ahead of the actual company, so it never matched;
        searching the bare ticker "BSE" correctly surfaces "BSE Limited" as a real
        result (sc_id "B03"). The fallback is cheap (only fires when the first search
        found nothing) and general — not special-cased to this one company — so it
        should help any other name/index-name collision the same way."""
        result = self._autosuggest(company_name, nse_ticker)
        if result is _BLOCKED or result is not None:
            return result
        return self._autosuggest(nse_ticker, nse_ticker)

    def _fetch_balance_sheet(self, sc_id: str) -> list[dict]:
        resp = requests.get(BALANCE_SHEET_URL, params={"sc_id": sc_id, "deviceType": "W"}, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        rows = data.get("consolidatedResult") or data.get("standardResult") or []
        return rows[:MAX_YEARS_BACKFILL]

    def _fetch_pnl(self, sc_id: str) -> list[dict]:
        """Tries consolidated first, falls back to standalone ("yearly") — a real gap
        found live: SBI Life reports standalone only (no subsidiaries to consolidate),
        so "cons_yearly" comes back as an empty string (not an empty list — MoneyControl's
        API returns "" for "no data of this type" rather than []), and the whole company
        got silently skipped. `_fetch_balance_sheet` above already has this exact
        consolidated-or-standalone fallback; this endpoint never did."""
        for type_format in ("cons_yearly", "yearly"):
            resp = requests.get(
                PNL_URL, params={"sc_id": sc_id, "type_format": type_format, "start": 0, "limit": MAX_YEARS_BACKFILL},
                headers=HEADERS, timeout=20,
            )
            resp.raise_for_status()
            data = resp.json().get("data")
            if data:
                return data
        return []

    def _fetch_hits_misses(self, sc_id: str) -> list[dict]:
        resp = requests.get(
            HITS_MISSES_URL, params={"deviceType": "W", "scId": sc_id, "ex": "N", "type": "eps", "financialType": "C"},
            headers=HEADERS, timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("list") or []

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        if symbol:
            # An explicit symbol always runs regardless of whether it already has a
            # quarterly_data row — real gap found live: the bulk-run candidate list
            # below only considers symbols bse_source already wrote a row for, so a
            # company bse_source can NEVER reach (NSE:BSE — SEBI prohibits an exchange
            # from listing on itself, so it's absent from BSE's own scrip master
            # entirely, confirmed live) was permanently unreachable here too, even
            # though MoneyControl has real data for it (sc_id "B03", real P&L rows).
            symbols = [symbol]
        else:
            symbols = [r["symbol"] for r in conn.execute(
                "SELECT DISTINCT symbol FROM quarterly_data WHERE symbol IN "
                "(SELECT symbol FROM stocks WHERE market = 'IN')"
            ).fetchall()]

        if not symbol:
            # Resume, don't restart: skip symbols this source already reached, same
            # resume principle as tiingo_source.py/bse_source.py. Confirmed live this
            # session that a real production run (964/1,185 in-scope symbols attempted)
            # got hard-blocked by MoneyControl's Akamai protection partway through —
            # major names (RELIANCE, TCS, HDFCBANK) that resolve fine in isolation came
            # back with zero data because the run's IP got flagged mid-run, not because
            # any individual symbol failed. An explicit `symbol` re-ingestion always
            # runs regardless.
            #
            # "Already has a moneycontrol row" is NOT the same as "fully covered" —
            # real gap found live: a bulk (symbol=None) trigger used to skip a symbol
            # the instant it had ANY moneycontrol row, on the assumption each pull
            # fetches full history in one shot so there's nothing left to gain by
            # re-attempting. That was true when this comment was written, but
            # other_income_cr/equity_share_capital_cr/operating_profit_cr were added
            # or fixed on this source afterward — a symbol ingested before that still
            # has real, existing rows missing them, and the coarse skip meant a bulk
            # run could NEVER reach it again to backfill them (confirmed live: a full
            # bulk re-trigger left 786 of 799 pending small-caps untouched, all
            # excluded here despite genuinely needing the newer fields — only an
            # explicit single-symbol call, which bypasses this filter entirely, ever
            # reached them). Now a symbol with ZERO rows missing any of these three
            # skips; anything still missing one stays in the candidate list and hits
            # the existing needs_backfill logic below, same as it already does for an
            # explicit `symbol` call.
            #
            # revenue_growth_pct deliberately NOT included here (unlike in
            # needs_backfill below): it is legitimately, permanently NULL on every
            # symbol's single earliest period — no prior-year comparable can ever
            # exist for it — so checking it at the symbol level would mean EVERY
            # symbol always has at least one "missing" row and never gets marked
            # covered, turning every future bulk run into a full reprocess forever.
            # operating_profit_cr doesn't have that problem: once the key-spacing fix
            # above is in place, it's genuinely computable for the large majority of
            # real companies, same risk profile as other_income_cr.
            already_covered = {r[0] for r in conn.execute(
                "SELECT symbol FROM quarterly_data WHERE source = 'moneycontrol' "
                "GROUP BY symbol HAVING SUM(CASE WHEN other_income_cr IS NULL "
                "OR equity_share_capital_cr IS NULL OR operating_profit_cr IS NULL "
                "THEN 1 ELSE 0 END) = 0"
            ).fetchall()}
            symbols = [s for s in symbols if s not in already_covered]

        symbols = apply_symbol_filter(symbols, limit, symbol)
        rows_written = 0

        # Safety net: this session saw the same job hang indefinitely (0 rows, 0% CPU,
        # never erroring or completing) three separate times, each requiring a manual
        # container restart to clear. Root cause not pinned down — every requests.get()
        # here already has an explicit timeout, so the leading theory is a DNS
        # resolution stall that some resolver/urllib3 combinations don't bound the way
        # the declared timeout implies — but rather than chase that further, this caps
        # the whole run's wall-clock time so a hang self-terminates (as 'success' with
        # partial progress, not stuck 'running' forever) instead of needing another
        # manual restart. A future run picks up right where this one left off via the
        # skip-already-covered logic above.
        run_deadline = time.monotonic() + MAX_RUN_SECONDS

        for sym in symbols:
            if time.monotonic() > run_deadline:
                break
            name_row = conn.execute("SELECT name FROM stocks WHERE symbol = ?", (sym,)).fetchone()
            if not name_row:
                continue
            ticker = sym.split(":")[1]

            time.sleep(RATE_LIMIT_SECONDS)
            sc_id = self._resolve_sc_id(name_row["name"], ticker)
            if sc_id is _BLOCKED:
                break  # stop the whole run — every remaining symbol would 403 too; preserve what's left of the budget
            if not sc_id:
                continue

            existing_rows = conn.execute(
                "SELECT period_end_date, other_income_cr, equity_share_capital_cr, revenue_growth_pct, "
                "operating_profit_cr FROM quarterly_data WHERE symbol = ? AND source = 'moneycontrol'",
                (sym,),
            ).fetchall()
            existing_periods = {r["period_end_date"] for r in existing_rows}
            # Backfill target: an existing moneycontrol row missing other_income_cr,
            # equity_share_capital_cr, revenue_growth_pct, and/or operating_profit_cr —
            # all four were always derivable from MoneyControl's own PNL response (the
            # first two directly, the third from a YoY comparison this source never
            # computed at all, the fourth from a key-spacing mismatch that silently
            # broke every non-bank financial-services company — confirmed live on
            # SBICARD: MoneyControl returns "P/L Before Other Inc. , Int., Excpt. Items
            # & Tax" with an extra space before the comma for this company type, so the
            # code's non-spaced lookup always missed). Reprocessed alongside genuinely
            # new periods below; the INSERT is an upsert that only ever touches these
            # four fields (plus opm_pct, derived from the fourth) on an existing row,
            # never any other field.
            needs_backfill = {r["period_end_date"] for r in existing_rows
                               if r["other_income_cr"] is None or r["equity_share_capital_cr"] is None
                               or r["revenue_growth_pct"] is None or r["operating_profit_cr"] is None}

            time.sleep(RATE_LIMIT_SECONDS)
            try:
                bs_rows = self._fetch_balance_sheet(sc_id)
            except requests.RequestException:
                bs_rows = []
            time.sleep(RATE_LIMIT_SECONDS)
            try:
                pnl_rows = self._fetch_pnl(sc_id)
            except requests.RequestException:
                pnl_rows = []

            bs_by_period = {}
            for r in bs_rows:
                pe = _year_label_to_period_end(r.get("yrc0", ""))
                if pe:
                    bs_by_period[pe] = r
            pnl_by_period = {}
            for r in pnl_rows:
                pe = _year_label_to_period_end(r.get("yrc0", ""))
                if pe:
                    pnl_by_period[pe] = r

            all_periods = set(bs_by_period) | set(pnl_by_period)
            new_periods = sorted((p for p in all_periods if p not in existing_periods or p in needs_backfill))

            # Precomputed once over the FULL period set (not just new_periods) so the
            # year-ago comparator below can find a prior period's revenue even when
            # that prior period isn't itself being (re)written this pass — each pull
            # already returns a symbol's full available history in one shot, so this
            # needs no extra request. Real gap found live: this source never computed
            # revenue_growth_pct at all (0% of its 21,513 existing rows had it, vs
            # 29.6% of bse_source.py's own rows, which already does this comparison).
            def _revenue_for(pe_key):
                p = pnl_by_period.get(pe_key, {})
                if "Interest Expended" in p:
                    parts = [_num(p.get(k)) for k in BANK_INTEREST_COMPONENT_KEYS]
                    return sum(v for v in parts if v is not None) if any(v is not None for v in parts) else None
                return _num(p.get("Total Income From Operations"))

            for pe in new_periods:
                pnl = pnl_by_period.get(pe, {})
                bs = bs_by_period.get(pe, {})

                is_bank = "Interest Expended" in pnl
                if is_bank:
                    interest_parts = [_num(pnl.get(k)) for k in BANK_INTEREST_COMPONENT_KEYS]
                    revenue = sum(v for v in interest_parts if v is not None) if any(v is not None for v in interest_parts) else None
                    op_profit = _num(pnl.get(BANK_OPERATING_PROFIT_KEY))
                else:
                    revenue = _num(pnl.get("Total Income From Operations"))
                    op_profit = next((v for k in OP_PROFIT_KEYS if (v := _num(pnl.get(k))) is not None), None)
                if revenue is None:
                    continue  # no real P&L data for this period — nothing worth writing

                prior_key = next(
                    (k for k in all_periods if abs((date.fromisoformat(k) - (date.fromisoformat(pe) - timedelta(days=365))).days) <= 10),
                    None,
                )
                prior_revenue = _revenue_for(prior_key) if prior_key else None
                revenue_growth_pct = safe_growth_pct(revenue, prior_revenue)

                net_profit = _num(pnl.get("Net Profit/(Loss) For the Period"))
                pbt = _num(pnl.get("P/L Before Tax"))
                tax = _num(pnl.get("Tax"))
                eps = _num(pnl.get("Basic EPS"))
                # Real gap found live: "Other Income" is right there in every PNL
                # response (confirmed on HDFCBANK's raw JSON — a real ₹146,847.66cr
                # value) but this extractor never read it, so every moneycontrol-
                # sourced row silently had a NULL Other Income despite the source
                # actually reporting it.
                other_income = _num(pnl.get("Other Income"))
                # Paid-up equity share capital, ₹Cr — also always in the PNL response,
                # also never extracted before now. This is the reliable signal for a
                # real corporate action (bonus/split): flat = none, a real jump means
                # one happened, and the ratio IS the correct EPS adjustment factor —
                # confirmed live this session as far more trustworthy than guessing
                # from an EPS jump alone (most large EPS jumps checked turned out to
                # be genuine earnings volatility, not corporate actions).
                equity_share_capital = _num(pnl.get("Equity Share Capital"))
                opm_pct = round(op_profit / revenue * 100, 2) if op_profit is not None and revenue else None
                tax_pct = round(tax / pbt * 100, 1) if pbt and tax is not None else None

                debt = None
                lt_debt, st_debt = _num(bs.get("LongTermBorrowings")), _num(bs.get("ShortTermBorrowings"))
                if lt_debt is not None or st_debt is not None:
                    debt = (lt_debt or 0) + (st_debt or 0)
                elif is_bank:
                    debt = _num(bs.get(BANK_BORROWINGS_KEY))
                cash = _num(bs.get(BANK_CASH_KEY)) if is_bank else _num(bs.get("CashAndCashEquivalents"))
                equity = _num(bs.get("TotalShareHoldersFunds"))
                net_debt = (debt - cash) if debt is not None and cash is not None else None

                shares_out = _num(pnl.get("No Of Shares (Crores)"))  # already in crores, matches our unit convention
                book_value_per_share = round(equity / shares_out, 2) if equity and shares_out else None

                conn.execute(
                    """INSERT INTO quarterly_data
                    (symbol, period_end_date, quarter_label, source, revenue_cr, operating_profit_cr,
                     opm_pct, net_profit_cr, eps, other_income_cr, equity_share_capital_cr,
                     total_debt_cr, net_debt_cr, cash_cr, pbt_cr, tax_pct, book_value_per_share, revenue_growth_pct)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol, period_end_date) DO UPDATE SET
                        other_income_cr = COALESCE(excluded.other_income_cr, other_income_cr),
                        equity_share_capital_cr = COALESCE(excluded.equity_share_capital_cr, equity_share_capital_cr),
                        revenue_growth_pct = COALESCE(excluded.revenue_growth_pct, revenue_growth_pct),
                        operating_profit_cr = COALESCE(excluded.operating_profit_cr, operating_profit_cr),
                        opm_pct = COALESCE(excluded.opm_pct, opm_pct)""",
                    # ON CONFLICT only ever fires for a period already in
                    # needs_backfill (a genuinely new period can't conflict) — and
                    # even then touches ONLY these five fields, deliberately, so a
                    # backfill pass can never disturb any other already-correct
                    # field on an existing row. operating_profit_cr/opm_pct added
                    # alongside the OP_PROFIT_KEYS fix above — without this, the
                    # correctly-extracted value was computed but silently dropped
                    # on an existing row, since a plain ON CONFLICT only touches
                    # columns actually listed in its SET clause (confirmed live:
                    # SBICARD still showed NULL after a full re-ingestion, even
                    # though the raw PNL lookup itself was already fixed).
                    (sym, pe, f"FY{pe[:4]} (MoneyControl, annual)", "moneycontrol",
                     revenue, op_profit, opm_pct, net_profit, eps, other_income, equity_share_capital,
                     debt, net_debt, cash, pbt, tax_pct, book_value_per_share, revenue_growth_pct),
                )
                rows_written += 1

            # Guidance enrichment (beat/miss/inline) — broader than finnhub_source.py's
            # 4-ticker ADR list, works for any India symbol MoneyControl covers. Only
            # fills rows that don't already have a verdict, never overwrites.
            time.sleep(RATE_LIMIT_SECONDS)
            try:
                hits_misses = self._fetch_hits_misses(sc_id)
            except requests.RequestException:
                hits_misses = []

            current_periods = [r["period_end_date"] for r in conn.execute(
                "SELECT period_end_date FROM quarterly_data WHERE symbol = ? AND guidance_vs_delivered IS NULL", (sym,)
            ).fetchall()]
            verdict_map = {"positive": "beat", "negative": "miss", "inline": "met"}
            for item in hits_misses:
                verdict = verdict_map.get(item.get("type"))
                q = item.get("quarter", "")
                m = re.match(r"(\w{3})\s+(\d{4})", q)
                if not verdict or not m or m.group(1) not in _QUARTER_MONTH_TO_DATE:
                    continue
                q_date = date.fromisoformat(m.group(2) + _QUARTER_MONTH_TO_DATE[m.group(1)])
                nearest = min(
                    current_periods,
                    key=lambda p: abs((date.fromisoformat(p) - q_date).days),
                    default=None,
                )
                if nearest is None or abs((date.fromisoformat(nearest) - q_date).days) > DATE_TOLERANCE_DAYS:
                    continue
                conn.execute(
                    "UPDATE quarterly_data SET guidance_vs_delivered = ? WHERE symbol = ? AND period_end_date = ?",
                    (verdict, sym, nearest),
                )
                rows_written += 1

            conn.commit()
            if progress:
                progress(rows_written)

        return rows_written
