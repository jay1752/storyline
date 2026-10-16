"""Fetch IndianAPI /stock (peers, officers, analyst view, holding split, corporate actions, news) for India stocks that do not have it yet, largest first.

One call per stock. Identity is checked on what the response itself says: its ISIN must equal ours (or, where either is missing, its NSE code our ticker);
a response for a different company is moved to raw/stock_wrong/ and never loaded. Resumable (a stock already fetched is skipped). Hard cap: --calls N
for this run, on top of the shared counter in data/indianapi/calls.json. Stops if a batch of 100 has more than 10% mismatches or faults.
    docker compose exec -d -w /app -e PYTHONPATH=/app app sh -c "python scripts/indianapi_stock_details.py --calls 2600 > /tmp/ia_stock.log 2>&1"
then load:  python scripts/load_indianapi.py --symbols NSE:X,NSE:Y ..."""
import argparse
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indianapi_backfill as bf  # noqa: E402


def todo_tickers(limit_by_mcap: int) -> list[tuple[str, str | None]]:
    m = sqlite3.connect(f"file:{os.environ.get('DATA_DIR', '/app/data')}/market.db?mode=ro", uri=True)
    isin = dict(m.execute("SELECT symbol, isin FROM stocks WHERE market='IN'").fetchall())
    rows = m.execute("SELECT symbol FROM stock_metrics WHERE market='IN' AND symbol LIKE 'NSE:%' AND mcap IS NOT NULL ORDER BY mcap DESC LIMIT ?", (limit_by_mcap,)).fetchall()
    out = []
    for (sym,) in rows:
        t = sym.split(":", 1)[1]
        if "&" in t or "-" in t:  # IndianAPI resolves these to the wrong company by ticker (M&MFIN -> M&M); they need a name lookup
            continue
        if bf._path("stock", t).exists() or bf._path("stock_nodata", t).exists() or (bf.RAW / "stock_wrong" / f"{t}.json").exists():
            continue
        out.append((t, isin.get(sym)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, required=True, help="most calls this run may spend")
    ap.add_argument("--universe", type=int, default=4400, help="how many of the largest India stocks to consider")
    ap.add_argument("--batch", type=int, default=100)
    a = ap.parse_args()
    key = os.environ.get("INDIANAPI_KEY")
    if not key:
        sys.exit("INDIANAPI_KEY not set")
    start = bf._calls()["total"]
    cl, fails = bf.Client(key, start + a.calls), {}
    todo = todo_tickers(a.universe)
    print(f"{len(todo)} stocks without /stock data; this run may spend {a.calls} calls (counter at {start})", flush=True)
    ok = wrong = nodata = faults = 0
    batch_n = batch_bad = 0
    try:
        for i, (t, isin) in enumerate(todo, 1):
            f0 = len(fails)
            data = bf.fetch(cl, "stock", t, {"name": t}, fails)
            batch_n += 1
            if data is None:
                new = list(fails.values())[f0:]
                if bf._path("stock_nodata", t).exists():
                    nodata += 1
                else:
                    faults += 1
                    batch_bad += 1
            else:
                prof = (data.get("companyProfile") or {}) if isinstance(data, dict) else {}
                nse = (prof.get("exchangeCodeNse") or "").upper()
                got_isin = (prof.get("isInId") or "").upper()
                if isin and got_isin:      # the ISIN is the surest identity; a new listing may have no NSE code in the response yet
                    same = isin.upper() == got_isin
                else:
                    same = nse == t.upper()
                if same:
                    ok += 1
                else:
                    wrong += 1
                    batch_bad += 1
                    dst = bf.RAW / "stock_wrong"
                    dst.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(bf._path("stock", t)), str(dst / f"{t}.json"))
                    print(f"  WRONG ENTITY {t}: IndianAPI answered {nse or '?'} / {got_isin or '?'} (ours {isin})", flush=True)
            if batch_n >= a.batch:
                print(f"{i}/{len(todo)} ok={ok} wrong={wrong} no-data={nodata} faults={faults} counter={bf._calls()['total']}", flush=True)
                if batch_bad / batch_n > 0.10:
                    print("STOPPED: more than 10% of the last batch failed the health gate", flush=True)
                    break
                batch_n = batch_bad = 0
    except bf.Budget as e:
        print("STOPPED:", e, flush=True)
    print(f"DONE ok={ok} wrong={wrong} no-data={nodata} faults={faults} counter={bf._calls()['total']}", flush=True)
    (bf.ROOT / "failures_stock_details.json").write_text(json.dumps(fails, indent=1))


if __name__ == "__main__":
    sys.exit(main())
