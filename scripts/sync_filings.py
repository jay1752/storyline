"""Keep the exchange filings and read the key documents for the stocks people track (or for named / the largest India stocks), so a Deep research report
does not have to wait for them.

    docker compose exec -d -w /app -e PYTHONPATH=/app app sh -c "python scripts/sync_filings.py --tracked > /tmp/filings.log 2>&1"
    ... --symbols NSE:TITAN,NSE:MARICO     ... --top 200 (largest India stocks by market cap)     ... --list-only (keep the lists, read no documents)
One request a second to NSE; a stock takes a few seconds (a minute the first time if the documents are large). Resumable: stored text is never fetched twice."""
import argparse
import sqlite3
import sys
import time

from app import filings
from app.db import DATA_DIR


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracked", action="store_true")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--list-only", action="store_true")
    a = ap.parse_args()
    syms: list[str] = [s.strip() for s in a.symbols.split(",") if s.strip()]
    if a.tracked:
        u = sqlite3.connect(f"file:{DATA_DIR}/users.db?mode=ro", uri=True)
        syms += [r[0] for r in u.execute("SELECT DISTINCT symbol FROM tracked_positions WHERE status='active' AND symbol LIKE 'NSE:%'")]
    if a.top:
        m = sqlite3.connect(f"file:{DATA_DIR}/market.db?mode=ro", uri=True)
        syms += [r[0] for r in m.execute("SELECT symbol FROM stock_metrics WHERE market='IN' AND symbol LIKE 'NSE:%' AND mcap IS NOT NULL ORDER BY mcap DESC LIMIT ?", (a.top,))]
    syms = list(dict.fromkeys(syms))
    print(len(syms), "stocks", flush=True)
    ok = bad = docs = 0
    for i, sym in enumerate(syms, 1):
        try:
            if a.list_only:
                res = {"sync": filings.sync(sym), "read": []}
            else:
                res = filings.refresh_key_documents(sym)
            err = res["sync"].get("error")
            ok += 0 if err else 1
            bad += 1 if err else 0
            docs += sum(1 for d in res["read"] if d["text_status"] == "ok")
            if err:
                print(sym, "list failed:", err, flush=True)
        except Exception as e:  # noqa: BLE001 - one stock must not stop the run
            bad += 1
            print(sym, "failed:", type(e).__name__, str(e)[:80], flush=True)
        if i % 10 == 0:
            print(f"{i}/{len(syms)} ok={ok} failed={bad} documents_read={docs}", flush=True)
    print("DONE", f"ok={ok} failed={bad} documents_read={docs}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
