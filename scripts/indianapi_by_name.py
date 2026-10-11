"""IndianAPI lookups by COMPANY NAME for stocks whose ticker it resolves wrongly or not at all (tickers with & or -, and stocks whose first answer
failed the identity gate: Bajaj Auto, J&K Bank, M&M Financial, Vedanta...). One historical_stats(all) call per stock, stored in
data/indianapi/raw/historical_stats/<TICKER>.json (the file load_indianapi.py reads) after the same identity test the loader uses (latest quarters'
sales within 15% of the BSE filings); a wrong-entity answer is kept aside in raw/historical_stats_wrong/ and never loaded.
Counts every call in calls.json and stops at --budget.
    docker compose exec -T -w /app -e PYTHONPATH=/app -e INDIANAPI_KEY=... app python scripts/indianapi_by_name.py [--budget 9900] [--dry]"""
import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import requests

BASE = os.environ.get("INDIANAPI_HOST", "https://stock.indianapi.in")


def _fetcher():
    p = Path(__file__).with_name("indianapi_backfill.py")
    s = importlib.util.spec_from_file_location("fetcher", p)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def variants(name: str):
    n = re.sub(r"\s+", " ", name).strip()
    seen = set()
    s2 = re.sub(r"\b(LTD\.?|LIMITED)\b\.?", "", n, flags=re.I).strip(" .,-")
    for v in (n, s2, s2.title(), re.sub(r"\b(INDIA|IND)\b", "", s2, flags=re.I).strip(" .,-").title(), re.sub(r"[.,]", "", s2).title()):
        if v and v not in seen:
            seen.add(v)
            yield v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=9900)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--symbols", default="")
    a = ap.parse_args()
    key = os.environ.get("INDIANAPI_KEY")
    if not key and not a.dry:
        sys.exit("INDIANAPI_KEY not set")
    f = _fetcher()
    raw = f.RAW / "historical_stats"
    wrong = f.RAW / "historical_stats_wrong"
    wrong.mkdir(parents=True, exist_ok=True)
    m = sqlite3.connect(f"file:{os.environ.get('DATA_DIR', '/app/data')}/market.db?mode=ro", uri=True)
    ia = sqlite3.connect(f"file:{os.environ.get('DATA_DIR', '/app/data')}/indianapi.db?mode=ro", uri=True)
    held = {r[0] for r in ia.execute("SELECT symbol FROM ia_stock WHERE status='held_out'")}
    asked = {p.stem for p in raw.glob("*.json")} | {p.stem for p in (f.RAW / "historical_stats_nodata").glob("*.json")}
    targets = []
    for sym, name, sector in m.execute("SELECT symbol, name, sector FROM stocks WHERE market='IN'"):
        t = sym.split(":", 1)[1]
        if sector == "Exchange traded fund" or re.search(r"-B\d*$|-[A-Z]$", t):
            continue
        n_q = m.execute("SELECT COUNT(*) FROM quarterly_data WHERE symbol=?", (sym,)).fetchone()[0]
        if not n_q:
            continue
        if (wrong / f"{t}.json").exists():
            continue  # answered already; the identity test said another company or a restated history
        if sym in held or t not in asked or t in ("M&MFIN", "J&KBANK", "S&SPOWER"):
            targets.append((sym, t, name))
    if a.symbols:
        want = {x.strip() for x in a.symbols.split(",")}
        targets = [x for x in targets if x[0] in want or x[1] in want]
    print(len(targets), "stocks to look up by name", flush=True)
    if a.dry:
        print([t for _, t, _ in targets][:60])
        return
    cl = f.Client(key, a.budget)
    tally = {"loaded": 0, "wrong_entity": 0, "unknown": 0}
    for sym, t, name in targets:
        got = None
        for v in variants(name):
            body, err = cl.get("historical_stats", {"stock_name": v, "stats": "all"})
            if isinstance(body, dict) and "quarter_results" in body:
                got = body
                break
            if isinstance(body, dict) and "Unknown stock" not in str(body.get("error", "")):
                break
        if got is None:
            tally["unknown"] += 1
            continue
        tmp = wrong / f"{t}.json"
        tmp.write_text(json.dumps(got))
        ok = f.identity_ok(t, m) if False else None
        # identity test against OUR filings: temporarily point the checker at this file
        keep = raw / f"{t}.json"
        old = keep.read_text() if keep.exists() else None
        keep.write_text(json.dumps(got))
        ident = f.identity_ok(t, m)
        if ident is False:
            if old is not None:
                keep.write_text(old)
            else:
                keep.unlink()
            tally["wrong_entity"] += 1
        else:
            tmp.unlink()
            tally["loaded"] += 1
        print(sym, "ok" if ident is not False else "WRONG ENTITY", flush=True)
    print("DONE", tally, f.json.loads((f.CALLS).read_text()) if hasattr(f, "json") else "", flush=True)


if __name__ == "__main__":
    sys.exit(main())
