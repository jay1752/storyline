"""Named shareholders from NSE's shareholding-pattern filings (XBRL) into market.db.holders.

For each stock: NSE's master list gives one filing per quarter with a link to the XBRL file; the file names every promoter-group holder and every
public holder above 1% (mutual funds, foreign funds, insurers) with shares and percentage. Only the latest --filings quarters are read per stock.

Check built in: the promoter-group names must add up to the filing's own promoter total (within 0.6 points). If they do not, the rows are still
stored but with category "Needs check", never guessed into the promoter group.
    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/fill_holders_nse.py [--top 300 | --symbols NSE:MARICO,...] [--filings 2] [--dry]"""
import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime
from xml.etree import ElementTree as ET

import requests

from app.db import DATA_DIR, init_db

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
API = "https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol="
PROMOTER = {"IndividualsOrHUF", "CentralGovernmentOrStateGovernment", "OthersIndianShareholders", "NonResidentIndividualsOrForeignIndividuals",
            "ForeignGovernment", "ForeignInstitutionalInvestor", "ForeignPortfolioInvestor"}
LABEL = {"IndividualsOrHUF": "Promoter group · individuals", "OthersIndianShareholders": "Promoter group · other Indian", "BodiesCorporate": "Bodies corporate",
         "NonResidentIndividualsOrForeignIndividuals": "Promoter group · foreign individuals", "CentralGovernmentOrStateGovernment": "Government",
         "MutualFundsOrUTI": "Mutual fund", "InsuranceCompanies": "Insurance company", "AlternativeInvestmentFund": "Alternative investment fund",
         "FinancialInstitutionsOrBanks": "Bank / financial institution", "DetailsOfSharesHeldByInstitutionsForeignPortfolioInvestorOne": "Foreign portfolio investor",
         "DetailsOfSharesHeldByInstitutionsForeignPortfolioInvestorTwo": "Foreign portfolio investor", "ForeignPortfolioInvestor": "Foreign portfolio investor",
         "OtherNonInstitutions": "Other public", "ResidentIndividualAbove2Lakh": "Individual investor", "ProvidentFundsOrPensionFunds": "Pension / provident fund"}


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_filing(xml: bytes) -> list[dict]:
    """Every named holder: {name, ctx, shares, pct}. pct is in percent points."""
    names, shares, pct = {}, {}, {}
    for el in ET.fromstring(xml).iter():
        ref = el.get("contextRef")
        if not ref:
            continue
        t, key = local(el.tag), ref[2:] if ref.startswith("D_") else ref
        if t == "NameOfTheShareholder" and (el.text or "").strip():
            names[key] = el.text.strip()
        elif t == "NumberOfFullyPaidUpEquityShares" and el.text:
            shares.setdefault(key, float(el.text))
        elif t.startswith("ShareholdingAsAPercentageOfTotalNumberOfShares") and el.text:
            pct.setdefault(key, float(el.text) * 100)
    return [{"name": n, "ctx": k, "shares": shares.get(k), "pct": pct.get(k)} for k, n in names.items() if shares.get(k) is not None]


def cat_of(ctx: str) -> str:
    c = re.sub(r"_?(Context|Public)\d+$", "", ctx)
    if c.startswith("CentralGovernment"):  # filings spell it with and without the trailing "s"
        return "CentralGovernmentOrStateGovernment"
    if "ResidentIndividual" in c:
        return "ResidentIndividualAbove2Lakh"
    return c


PUBLIC_ONLY = {"MutualFundsOrUTI", "InsuranceCompanies", "AlternativeInvestmentFund", "AlternativeInvestmentFunds", "ProvidentFundsOrPensionFunds",
               "ResidentIndividualAbove2Lakh", "OtherNonInstitutions"}


def classify(rows: list[dict], promoter_total: float | None) -> None:
    """Sets row['category']. Promoter-group rows are accepted only when they add up to the filing's promoter total.

    The filing does not say which holder rows belong to the promoter group (bodies corporate, foreign companies and banks appear on both sides), so the
    ambiguous categories are tried in every combination and the one whose sum is closest to the filing's promoter total (within 0.6 points) wins."""
    from itertools import combinations

    by: dict[str, float] = {}
    for r in rows:
        by[cat_of(r["ctx"])] = by.get(cat_of(r["ctx"]), 0) + (r["pct"] or 0)
    base = {c for c in by if c in PROMOTER}
    amb = [c for c in by if c not in PROMOTER and c not in PUBLIC_ONLY and "PortfolioInvestor" not in c][:10]
    chosen, ok = base, False
    if promoter_total is not None:
        best = None
        for n in range(0, len(amb) + 1):
            for combo in combinations(amb, n):
                diff = abs(sum(by[c] for c in base) + sum(by[c] for c in combo) - promoter_total)
                if diff <= 0.6 and (best is None or diff < best[0] - 1e-9):
                    best = (diff, set(combo))
        if best is not None:
            chosen, ok = base | best[1], True
    for r in rows:
        c = cat_of(r["ctx"])
        r["category"] = ("Promoter group" if ok else "Needs check") if c in chosen else LABEL.get(c, re.sub(r"(?<!^)(?=[A-Z])", " ", c))


def cross_check(m: sqlite3.Connection) -> dict:
    """The master list's own promoter total can be wrong, so every reconciled filing is also compared with the promoter % we already hold for that quarter
    (IndianAPI / BSE). A difference over 1 point demotes the promoter rows to "Needs check"."""
    demoted = compared = 0
    for sym, per, total in m.execute("SELECT symbol, period_end, SUM(pct) FROM holders WHERE category = 'Promoter group' GROUP BY symbol, period_end").fetchall():
        ours = m.execute("SELECT promoter_pct FROM quarterly_data WHERE symbol=? AND period_end_date=? AND promoter_pct IS NOT NULL LIMIT 1", (sym, per)).fetchone()
        if not ours:
            continue
        compared += 1
        if abs(ours[0] - total) > 1.0:
            m.execute("UPDATE holders SET category='Needs check' WHERE symbol=? AND period_end=? AND category='Promoter group'", (sym, per))
            demoted += 1
    m.commit()
    return {"compared": compared, "demoted": demoted}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=300)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--filings", type=int, default=2)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    init_db()
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=300)
    if a.symbols:
        syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    else:
        syms = [r[0] for r in m.execute("SELECT symbol FROM stock_metrics WHERE market='IN' AND symbol LIKE 'NSE:%' AND mcap IS NOT NULL ORDER BY mcap DESC LIMIT ?", (a.top,))]
    print(len(syms), "stocks", flush=True)

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Referer": "https://www.nseindia.com/"})

    def warm():
        s.get("https://www.nseindia.com/", timeout=25)

    warm()
    stats = {"stocks_ok": 0, "filings": 0, "rows": 0, "promoter_ok": 0, "needs_check": 0, "no_filing": 0, "errors": 0}
    for i, sym in enumerate(syms, 1):
        nse = sym.split(":", 1)[1]
        try:
            r = s.get(API + requests.utils.quote(nse), timeout=25)
            if r.status_code != 200 or not r.text.startswith("["):
                warm()
                time.sleep(2)
                r = s.get(API + requests.utils.quote(nse), timeout=25)
            filings = r.json() if r.text.startswith("[") else []
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            print(sym, "master failed:", type(e).__name__, flush=True)
            time.sleep(2)
            continue
        by_date = {}
        for f in filings:  # one filing per quarter: the most recently submitted (a revision replaces the original)
            if f.get("xbrl") and f.get("date"):
                d = datetime.strptime(f["date"], "%d-%b-%Y").strftime("%Y-%m-%d")
                if d not in by_date or (f.get("recordId") or 0) > (by_date[d].get("recordId") or 0):
                    by_date[d] = f
        if not by_date:
            stats["no_filing"] += 1
            continue
        got = 0
        for d in sorted(by_date, reverse=True)[: a.filings]:
            if m.execute("SELECT 1 FROM holders WHERE symbol=? AND period_end=? LIMIT 1", (sym, d)).fetchone():
                got += 1
                continue
            f = by_date[d]
            try:
                x = requests.get(f["xbrl"], headers={"User-Agent": UA}, timeout=60)
                x.raise_for_status()
                rows = parse_filing(x.content)
                tot = float(f["pr_and_prgrp"]) if f.get("pr_and_prgrp") not in (None, "") else None
                classify(rows, tot)
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                print(sym, d, "filing failed:", type(e).__name__, str(e)[:60], flush=True)
                continue
            keep = [r for r in rows if r["category"] in ("Promoter group", "Needs check") or (r["pct"] or 0) >= 1.0]
            keep = [r for r in keep if r["shares"] and r["shares"] > 0]
            stats["filings"] += 1
            stats["rows"] += len(keep)
            stats["promoter_ok" if any(r["category"] == "Promoter group" for r in keep) else "needs_check"] += 1
            if not a.dry:
                filed = datetime.strptime(f["submissionDate"], "%d-%b-%Y").strftime("%Y-%m-%d") if f.get("submissionDate") else None
                m.executemany("INSERT OR REPLACE INTO holders (symbol, period_end, holder_name, category, shares, pct, filed_on) VALUES (?,?,?,?,?,?,?)",
                              [(sym, d, r["name"], r["category"], r["shares"], round(r["pct"], 4) if r["pct"] is not None else None, filed) for r in keep])
                m.commit()
            got += 1
            time.sleep(0.4)
        stats["stocks_ok"] += 1 if got else 0
        if i % 25 == 0:
            print(f"{i}/{len(syms)} {stats}", flush=True)
        time.sleep(1.0)
    if not a.dry:
        print("second check against the promoter % we already hold:", cross_check(m), flush=True)
    print("DONE", stats, flush=True)


if __name__ == "__main__":
    sys.exit(main())
