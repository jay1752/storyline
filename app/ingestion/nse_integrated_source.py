"""NSE "Integrated Filing - Financials" — quarterly results for NSE-listed companies that BSE's feed doesn't cover.

Why: ~107 currently-listed NSE symbols (plus a few whose BSE history stopped) had no fundamentals at all, because
bse_source.py only sees what BSE's scrip list resolves. NSE's `/api/integrated-filing-results?index=equities&symbol=X`
lists every filing since Dec-2024 (new SEBI integrated format); each "Integrated Filing- Financials" entry links a plain
XBRL file on nsearchives.nseindia.com — the same in-capmkt tag names bse_source.py already knows, values in full rupees.
Plain User-Agent only (a browser-mimicking header set is what Akamai flags).

What it writes: one quarterly_data row per quarter (source='nse'), ONLY for periods the symbol doesn't already have, so
it can never overwrite a BSE/MoneyControl row. Consolidated is preferred over standalone (same convention as the
rest of the India data). Insurers use a different tag vocabulary with no clean revenue line, so their rows carry
net profit/EPS/PBT/tax but no revenue or operating profit, rather than a guessed number."""

import csv
import io
import re
import time
from datetime import date, datetime, timedelta

import requests

from app.ingestion.base import DataSource, apply_symbol_filter, safe_growth_pct

API_URL = "https://www.nseindia.com/api/integrated-filing-results"
EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"}
RATE_LIMIT_SECONDS = 0.5
CRORE = 1e7

REVENUE_TAGS = ["RevenueFromOperations", "InterestEarned"]
NET_PROFIT_TAGS = ["ProfitLossForPeriod", "ProfitLossForThePeriod", "ProfitLossAfterTax", "ProfitLossAfterTaxBeforeExtraordinaryItems"]
PBT_TAGS = ["ProfitBeforeTax", "ProfitLossFromOrdinaryActivitiesBeforeTax", "ProfitOrLossBeforeTax", "ProfitLossBeforeTax"]
TAX_TAGS = ["TaxExpense", "ProvisionForTax", "ProvisionsForTaxes"]
EPS_TAGS = ["BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "BasicEarningsLossPerShareFromContinuingOperations",
            "BasicEarningsPerShareBeforeExtraordinaryItems",
            "BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized",
            "BasicAndDilutedEPSBeforeExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized"]
_CTX_FACT = re.compile(r'<(?:[\w\-]+):(\w+)\b[^>]*?contextRef="OneD"[^>]*>([^<]+)<')


def _facts(xml: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag, val in _CTX_FACT.findall(xml):
        out.setdefault(tag, val.strip())
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _first(facts: dict, tags: list[str]):
    for t in tags:
        v = _num(facts.get(t))
        if v is not None:
            return v
    return None


def parse_filing(xml: str) -> dict | None:
    """One financials XBRL -> a row dict in ₹ Cr (EPS in ₹), or None if it isn't a quarterly result."""
    f = _facts(xml)
    if f.get("TypeOfReportingPeriod", "Quarterly") != "Quarterly":
        return None
    end = f.get("DateOfEndOfReportingPeriod")
    if not end:
        return None
    rev = _first(f, REVENUE_TAGS)
    np_ = _first(f, NET_PROFIT_TAGS)
    if rev is None and np_ is None:
        return None
    expenses = _num(f.get("Expenses"))
    op = (rev - expenses + (_num(f.get("FinanceCosts")) or 0) + (_num(f.get("DepreciationDepletionAndAmortisationExpense")) or 0)) \
        if rev is not None and expenses is not None else None
    pbt, tax = _first(f, PBT_TAGS), _first(f, TAX_TAGS)
    if pbt and tax is not None and np_ is not None and abs(pbt - np_ - tax) > max(0.03 * abs(pbt), 1e5):
        tax = None  # tax line doesn't reconcile with PBT - net profit (minorities, exceptional items): leave it blank
    cr = lambda v: None if v is None else round(v / CRORE, 4)
    return {
        "end": end, "revenue": cr(rev), "op": cr(op), "other_income": cr(_num(f.get("OtherIncome"))), "np": cr(np_),
        "pbt": cr(pbt), "tax_pct": round(tax / pbt * 100, 1) if pbt and tax is not None else None,
        "eps": _first(f, EPS_TAGS), "share_capital": cr(_num(f.get("PaidUpValueOfEquityShareCapital"))),
    }


class NseIntegratedSource(DataSource):
    name = "nse_integrated"

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("nse_integrated only provides quarterly fundamentals.")

    def _listed(self) -> set[str]:
        r = requests.get(EQUITY_LIST_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return {row["SYMBOL"].strip() for row in csv.DictReader(io.StringIO(r.text))}

    def _targets(self, conn) -> list[str]:
        """Listed NSE symbols with no fundamentals, or whose newest period is more than a quarter behind."""
        listed = self._listed()
        rows = conn.execute(
            """SELECT s.symbol, (SELECT MAX(period_end_date) FROM quarterly_data q WHERE q.symbol = s.symbol) AS latest
               FROM stocks s WHERE s.market = 'IN'"""
        ).fetchall()
        cutoff = (date.today() - timedelta(days=150)).isoformat()
        return [r["symbol"] for r in rows if r["symbol"].split(":", 1)[1] in listed and (r["latest"] is None or r["latest"] < cutoff)]

    def _best_filings(self, symbol: str) -> list[dict]:
        r = requests.get(API_URL, params={"index": "equities", "symbol": symbol}, headers=HEADERS, timeout=30)
        r.raise_for_status()
        fins = [x for x in (r.json().get("data") or []) if "Financ" in (x.get("type") or "") and x.get("xbrl")]
        best: dict[str, dict] = {}
        for x in fins:
            key = x["qe_Date"]
            rank = (x.get("consolidated") == "Consolidated", x.get("creation_Date") or "")  # consolidated, then newest revision
            if key not in best or rank > best[key][0]:
                best[key] = (rank, x)
        return [v[1] for v in best.values()]

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        symbols = [symbol] if symbol else self._targets(conn)
        symbols = apply_symbol_filter(symbols, limit, symbol)
        written = 0
        for sym in symbols:
            ticker = sym.split(":", 1)[1]
            time.sleep(RATE_LIMIT_SECONDS)
            try:
                filings = self._best_filings(ticker)
            except (requests.RequestException, ValueError):
                continue
            parsed = []
            for x in filings:
                time.sleep(RATE_LIMIT_SECONDS)
                try:
                    resp = requests.get(x["xbrl"], headers=HEADERS, timeout=30)
                    resp.raise_for_status()
                    row = parse_filing(resp.text)
                except requests.RequestException:
                    continue
                if row:
                    parsed.append(row)
            parsed.sort(key=lambda r: r["end"])
            by_end = {r["end"]: r for r in parsed}
            existing = {r[0] for r in conn.execute("SELECT period_end_date FROM quarterly_data WHERE symbol = ?", (sym,))}
            for r in parsed:
                if r["end"] in existing:
                    continue
                prior = next((v for k, v in by_end.items() if abs((date.fromisoformat(k) - (date.fromisoformat(r["end"]) - timedelta(days=365))).days) <= 10), None)
                growth = safe_growth_pct(r["revenue"], prior["revenue"]) if prior and r["revenue"] else None
                opm = round(r["op"] / r["revenue"] * 100, 2) if r["op"] is not None and r["revenue"] else None
                conn.execute(
                    """INSERT INTO quarterly_data (symbol, period_end_date, quarter_label, source, revenue_cr, operating_profit_cr, opm_pct,
                       other_income_cr, net_profit_cr, eps, revenue_growth_pct, pbt_cr, tax_pct, equity_share_capital_cr)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(symbol, period_end_date) DO NOTHING""",
                    (sym, r["end"], f"Q ending {r['end']}", "nse", r["revenue"], r["op"], opm, r["other_income"], r["np"], r["eps"],
                     growth, r["pbt"], r["tax_pct"], r["share_capital"]),
                )
                written += 1
            conn.commit()
            if progress:
                progress(written)
        return written
