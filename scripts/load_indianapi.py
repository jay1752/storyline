"""Load the raw IndianAPI JSON (data/indianapi/raw) into the staging DB data/indianapi.db. Reads market.db read-only (for the
identity check) and writes ONLY indianapi.db. Idempotent: each stock's rows are replaced wholesale.

    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/load_indianapi.py [--limit N]

Identity gate (same test as the fetcher): a stock whose latest quarters disagree with BSE revenue by >15% (median) is
'held_out' - nothing of it is loaded except the status row. 'unchecked' = fewer than 2 comparable quarters; loaded but flagged."""
import argparse
import calendar
import importlib.util
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from app.db import DATA_DIR
from app.indianapi_db import get_indianapi_conn

RAW = DATA_DIR / "indianapi" / "raw"
MONTHS = {m: i for i, m in enumerate(calendar.month_abbr) if m}
STATEMENT = {"quarter_results": ("Q", "PL"), "yoy_results": ("A", "PL"), "balancesheet": ("A", "BS"), "cashflow": ("A", "CF"), "ratios": ("A", "RATIO")}
HOLDING = {"shareholding_pattern_quarterly": "Q", "shareholding_pattern_yearly": "A"}
HORIZON = {"10 Years:": "10Y", "5 Years:": "5Y", "3 Years:": "3Y", "1 Year:": "1Y", "TTM:": "TTM", "Last Year:": "Last Year"}


def _spec():
    s = importlib.util.spec_from_file_location("fetcher", Path(__file__).with_name("indianapi_backfill.py"))
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def month_end(label: str):
    try:
        mon, yr = label.split()
        return date(int(yr), MONTHS[mon], calendar.monthrange(int(yr), MONTHS[mon])[1]).isoformat()
    except (ValueError, KeyError):
        return None  # 'TTM' and any other non-date label


def num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(re.sub(r"[%,\s]", "", str(v)))
    except ValueError:
        return None


def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def load_stats(c, sym: str, d: dict) -> tuple[int, str]:
    n = 0
    qr = d.get("quarter_results") or {}
    layout = "standard" if "Sales" in qr else "bank"
    for sec, (freq, stmt) in STATEMENT.items():
        for metric, series in (d.get(sec) or {}).items():
            if not isinstance(series, dict):
                continue
            for label, v in series.items():
                pe = month_end(label)
                if pe and num(v) is not None:
                    c.execute("INSERT OR REPLACE INTO ia_fact VALUES (?,?,?,?,?,?)", (sym, pe, freq, stmt, metric, num(v)))
                    n += 1
    for sec, freq in HOLDING.items():
        for cat, series in (d.get(sec) or {}).items():
            for label, v in (series or {}).items() if isinstance(series, dict) else []:
                pe = month_end(label)
                if pe and num(v) is not None:
                    c.execute("INSERT OR REPLACE INTO ia_holding VALUES (?,?,?,?,?)", (sym, pe, freq, cat, num(v)))
                    n += 1
    for metric, series in (d.get("profit_loss_stats") or {}).items():
        for h, v in (series or {}).items() if isinstance(series, dict) else []:
            if h in HORIZON and num(v) is not None:
                c.execute("INSERT OR REPLACE INTO ia_growth VALUES (?,?,?,?)", (sym, metric, HORIZON[h], num(v)))
                n += 1
    return n, layout


def load_target(c, sym: str, t: dict) -> int:
    pt = t.get("priceTarget") or {}
    if not pt.get("Mean"):
        return 0
    c.execute("INSERT OR REPLACE INTO ia_target VALUES (?,?,?,?,?,?,?,?,?,?)",
              (sym, pt.get("Mean"), pt.get("High"), pt.get("Low"), pt.get("Median"), pt.get("NumberOfEstimates"), pt.get("StandardDeviation"),
               pt.get("CurrencyCode"), json.dumps(t.get("recommendation")), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    for s in ((t.get("priceTargetSnapshots") or {}).get("PriceTargetSnapshot") or []):
        c.execute("INSERT OR REPLACE INTO ia_target_snapshot VALUES (?,?,?,?,?,?,?)",
                  (sym, s.get("Age"), s.get("Mean"), s.get("High"), s.get("Low"), s.get("Median"), s.get("NumberOfEstimates")))
    return 1


def load_estimates(c, sym: str, measure: str, freq: str, doc: dict) -> int:
    n = 0
    for p in doc.get("periods") or []:
        fp = p.get("FiscalPeriod") or {}
        if not fp.get("Year"):
            continue
        key = (sym, measure, freq, str(fp.get("Type")), int(fp["Year"]))
        for e in ((p.get("Estimates") or {}).get("Estimate") or [])[:1]:
            if e.get("Mean") is None:
                continue
            c.execute("INSERT OR REPLACE INTO ia_estimate VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (*key, "estimate", e.get("Mean"), e.get("High"), e.get("Low"), e.get("Median"), e.get("NumberOfEstimates"), e.get("StandardDeviation"), None, None, None))
            n += 1
        for a in ((p.get("Actuals") or {}).get("Actual") or [])[:1]:
            if a.get("Reported") is None:
                continue
            c.execute("INSERT OR REPLACE INTO ia_estimate VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (*key, "actual", None, None, None, None, a.get("NumberOfEstimates"), None, a.get("Reported"), a.get("ReportedDate"), a.get("SurprisePercent")))
            n += 1
    return n


def load_profile(c, sym: str, s: dict) -> int:
    cp = s.get("companyProfile") or {}
    reusable = s.get("stockDetailsReusableData") or {}
    risk = s.get("riskMeter") or {}
    c.execute("INSERT OR REPLACE INTO ia_profile VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (sym, cp.get("companyDescription"), s.get("industry") or cp.get("mgIndustry"), cp.get("isInId"), cp.get("exchangeCodeNse"), cp.get("exchangeCodeBse"),
               num(reusable.get("price")), reusable.get("date"), risk.get("categoryName"), risk.get("stdDev"),
               json.dumps((cp.get("officers") or {}).get("officer")), json.dumps(cp.get("peerCompanyList")), json.dumps(s.get("keyMetrics")),
               json.dumps(s.get("analystView")), json.dumps((s.get("recosBar") or {}).get("stockAnalyst")),
               json.dumps(s.get("stockCorporateActionData")), json.dumps(s.get("recentNews"))))
    return 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    fetcher = _spec()
    market = sqlite3.connect(f"file:{DATA_DIR}/market.db?mode=ro", uri=True)
    c = get_indianapi_conn()
    tickers = sorted(p.stem for p in (RAW / "historical_stats").glob("*.json"))
    if a.limit:
        tickers = tickers[:a.limit]
    tally = {"loaded": 0, "unchecked": 0, "held_out": 0, "facts": 0, "profiles": 0, "targets": 0, "estimates": 0, "bad_file": 0}
    for t in tickers:
        sym = f"NSE:{t}"
        d = load_json(RAW / "historical_stats" / f"{t}.json")
        if not isinstance(d, dict) or "quarter_results" not in d:
            tally["bad_file"] += 1
            continue
        ident = fetcher.identity_ok(t, market)
        status = "held_out" if ident is False else ("loaded" if ident else "unchecked")
        for tbl in ("ia_fact", "ia_holding", "ia_growth", "ia_target", "ia_target_snapshot", "ia_estimate", "ia_profile"):
            c.execute(f"DELETE FROM {tbl} WHERE symbol = ?", (sym,))
        if status == "held_out":
            c.execute("INSERT OR REPLACE INTO ia_stock (symbol, status, identity_ok) VALUES (?,?,0)", (sym, "held_out"))
            tally["held_out"] += 1
            continue
        n, layout = load_stats(c, sym, d)
        tally["facts"] += n
        prof = load_json(RAW / "stock" / f"{t}.json")
        has_prof = 0
        if isinstance(prof, dict) and prof.get("companyName"):
            has_prof = load_profile(c, sym, prof)
            tally["profiles"] += 1
        name = (prof or {}).get("companyName") if isinstance(prof, dict) else None
        c.execute("INSERT OR REPLACE INTO ia_stock (symbol, ia_name, layout, status, identity_ok, has_profile) VALUES (?,?,?,?,?,?)",
                  (sym, name, layout, status, None if ident is None else 1, has_prof))
        tg = load_json(RAW / "stock_target_price" / f"{t}.json")
        if isinstance(tg, dict):
            tally["targets"] += load_target(c, sym, tg)
        for folder, measure, freq in (("stock_forecasts_EPS", "EPS", "Annual"), ("stock_forecasts_SAL", "SAL", "Annual"), ("stock_forecasts_NET", "NET", "Annual"), ("stock_forecasts_EPSq", "EPS", "Interim")):
            doc = load_json(RAW / folder / f"{t}.json")
            if isinstance(doc, dict):
                tally["estimates"] += load_estimates(c, sym, measure, freq, doc)
        tally[status] += 1
        if sum(tally[k] for k in ("loaded", "unchecked", "held_out")) % 500 == 0:
            c.commit()
    c.commit()
    print(json.dumps(tally))


if __name__ == "__main__":
    sys.exit(main())
