"""Fill market.db.us_annual from SEC companyfacts: one row per US stock-year with sales, operating profit, net profit, balance-sheet totals,
operating cash flow, dividends paid and dividends per share (USD millions, same unit as US quarterly_data). This is what gives the US
screens a dividend payout and a 3-year average ROCE (the India side already has both from IndianAPI annual statements).

Only what a 10-K states is stored; a missing tag stays NULL (a company that pays no dividend simply has no dividend fact).
Resumable: stocks already present are skipped unless --force. Run (Mac or VM):
    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/fill_us_annual.py [--limit N] [--symbols NASDAQ:AAPL,...] [--force]"""
import argparse
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests

from app.db import DATA_DIR
from app.ingestion import sec_edgar_source as sec

DIV_TAGS = ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock", "PaymentsOfOrdinaryDividends"]
DPS_TAGS = ["CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"]
SCHEMA = """CREATE TABLE IF NOT EXISTS us_annual (
    symbol TEXT NOT NULL, period_end_date TEXT NOT NULL,
    revenue_m REAL, operating_profit_m REAL, net_profit_m REAL, assets_m REAL, current_liabilities_m REAL, equity_m REAL,
    cfo_m REAL, dividends_paid_m REAL, dps REAL, fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, period_end_date))"""
_LOCK = threading.Lock()
_LAST = [0.0]


def _throttle(gap: float = 0.13) -> None:
    """One shared clock for all workers: SEC allows ~10 requests a second."""
    with _LOCK:
        wait = _LAST[0] + gap - time.time()
        _LAST[0] = max(time.time(), _LAST[0] + gap)
    if wait > 0:
        time.sleep(wait)


def is_derivative(ticker: str, tickers: set[str]) -> bool:
    """Warrants, units, rights and preferred lines of a company: they carry no filings of their own."""
    if "-" in ticker:
        return bool(re.fullmatch(r"P[A-Z]?|UN?|WT?S?[A-Z]?|WW|RT?|R", ticker.rsplit("-", 1)[1]))
    for suf in ("WW", "WS", "W", "RT", "R", "U"):
        if ticker.endswith(suf) and len(ticker) > len(suf) + 1 and (ticker[:-len(suf)] in tickers or ticker[:-len(suf)] + "U" in tickers):
            return True
    return False


def _instant(src, facts, tags, scale=1e6):
    out: dict[str, float] = {}
    for tag in src._rank_tags(facts, tags):
        node = facts["facts"]["us-gaap"].get(tag)
        got: dict[str, float] = {}
        for unit_facts in node.get("units", {}).values():
            for f in unit_facts:
                if f.get("form") == "10-K" and "end" in f and f.get("fp") == "FY":
                    got[f["end"]] = f["val"] / scale
        for k, v in got.items():
            out.setdefault(k, v)
    return out


def extract(src, facts: dict) -> dict[str, dict]:
    def dur(tags, scale=1e6):
        return {k: v / scale for k, v in src._annual_series(facts, tags).items()}

    rev = dur(sec.REVENUE_TAGS + sec.FALLBACK_REVENUE_TAGS)
    series = {
        "revenue_m": rev, "operating_profit_m": dur(sec.OPERATING_INCOME_TAGS), "net_profit_m": dur(sec.NET_INCOME_TAGS + sec.NET_INCOME_FILL_TAGS),
        "cfo_m": dur([sec.CFO_TAG]), "dividends_paid_m": dur(DIV_TAGS), "dps": dur(DPS_TAGS, scale=1.0),
        "assets_m": _instant(src, facts, sec.ASSETS_TAGS), "current_liabilities_m": _instant(src, facts, sec.CURRENT_LIABILITIES_TAGS),
        "equity_m": _instant(src, facts, sec.EQUITY_TAGS),
    }
    ends = sorted(set(series["revenue_m"]) | set(series["net_profit_m"]) | set(series["operating_profit_m"]))
    return {e: {k: s.get(e) for k, s in series.items()} for e in ends[-8:]}  # last 8 fiscal years is plenty


def fetch(src, sym: str, cik: int):
    for attempt in range(3):
        try:
            _throttle()
            r = requests.get(sec.FACTS_URL.format(cik=cik), headers=src._headers(), timeout=30)
            if r.status_code == 404:
                return {}
            if r.status_code in (429, 503):
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return extract(src, r.json())
        except (requests.RequestException, ValueError, KeyError, AttributeError):
            time.sleep(2)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    src = sec.SecEdgarSource()
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=120)
    m.execute(SCHEMA)
    cik_lookup = {row["ticker"].upper(): int(row["cik_str"]) for row in src._all_tickers()}
    have = {r[0] for r in m.execute("SELECT DISTINCT symbol FROM us_annual")}
    want = {x.strip() for x in a.symbols.split(",") if x.strip()}
    rows = m.execute("""SELECT s.symbol FROM stocks s WHERE s.market='US'
                        AND (SELECT COUNT(*) FROM quarterly_data q WHERE q.symbol=s.symbol AND q.quarter_label NOT LIKE '%annual%') >= 4""").fetchall()
    tickers = {r[0].split(":", 1)[1] for r in rows}
    todo = []
    for (sym,) in rows:
        t = sym.split(":", 1)[1]
        if want and sym not in want:
            continue
        if (sym in have and not a.force) or is_derivative(t, tickers):
            continue
        cik = sec.CIK_OVERRIDES.get(t) or cik_lookup.get(t)
        if cik:
            todo.append((sym, cik))
    if a.limit:
        todo = todo[:a.limit]
    print(f"{len(todo)} stocks to fetch ({len(have)} already done)", flush=True)
    done = wrote = failed = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for (sym, cik), res in zip(todo, ex.map(lambda sc: fetch(src, *sc), todo)):
            done += 1
            if res is None:
                failed += 1
                continue
            if not res:
                m.execute("INSERT OR IGNORE INTO us_annual (symbol, period_end_date) VALUES (?, '0000-00-00')", (sym,))  # marks "fetched, nothing usable"
            for end, v in res.items():
                m.execute("INSERT OR REPLACE INTO us_annual (symbol, period_end_date, revenue_m, operating_profit_m, net_profit_m, assets_m, current_liabilities_m,"
                          " equity_m, cfo_m, dividends_paid_m, dps) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (sym, end, v["revenue_m"], v["operating_profit_m"], v["net_profit_m"], v["assets_m"], v["current_liabilities_m"], v["equity_m"],
                           v["cfo_m"], v["dividends_paid_m"], v["dps"]))
                wrote += 1
            if done % 100 == 0:
                m.commit()
                print(f"{done}/{len(todo)} rows={wrote} failed={failed} {int(time.time() - t0)}s", flush=True)
    m.commit()
    print(f"DONE {done} stocks, {wrote} stock-years, {failed} failed, {int(time.time() - t0)}s", flush=True)


if __name__ == "__main__":
    sys.exit(main())
