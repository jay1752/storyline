"""Fetch IndianAPI analyst price targets (current consensus plus its 1-week / 30 / 60 / 90-day history, one call) for India stocks that have /stock data
but no target yet, largest first.

A target call needs IndianAPI's own stock id. It is taken, in order, from a stock_id file we already have, from another stock's peer list (the peer id
equals the target-endpoint id: 189 of 190 checked; only a name that matches exactly one peer is used), or looked up with industry_search (1 extra call;
the hit must carry our NSE code). Resumable; hard cap --calls N for this run; stops if more than 10% of a batch of 100 fails.
    docker compose exec -d -w /app -e PYTHONPATH=/app app sh -c "python scripts/indianapi_targets.py --calls 2350 > /tmp/ia_targets.log 2>&1"
then load:  python scripts/load_indianapi.py --symbols NSE:X,NSE:Y ..."""
import argparse
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indianapi_backfill as bf  # noqa: E402


def norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, required=True)
    ap.add_argument("--batch", type=int, default=100)
    a = ap.parse_args()
    key = os.environ.get("INDIANAPI_KEY")
    if not key:
        sys.exit("INDIANAPI_KEY not set")
    data = os.environ.get("DATA_DIR", "/app/data")
    m = sqlite3.connect(f"file:{data}/market.db?mode=ro", uri=True)
    ia = sqlite3.connect(f"file:{data}/indianapi.db?mode=ro", uri=True)
    peers: dict[str, set] = {}
    for name, pid in ia.execute("SELECT peer_name, peer_ticker_id FROM ia_peer WHERE peer_ticker_id IS NOT NULL"):
        peers.setdefault(norm(name), set()).add(str(pid))
    order = [r[0].split(":", 1)[1] for r in m.execute("SELECT symbol FROM stock_metrics WHERE market='IN' AND symbol LIKE 'NSE:%' AND mcap IS NOT NULL ORDER BY mcap DESC")]
    todo = [t for t in order if "&" not in t and "-" not in t and bf._path("stock", t).exists() and not bf._path("stock_target_price", t).exists()
            and not bf._path("stock_target_price_nodata", t).exists()]
    start = bf._calls()["total"]
    cl, fails = bf.Client(key, start + a.calls), {}
    print(f"{len(todo)} stocks without a target; this run may spend {a.calls} calls (counter at {start})", flush=True)
    got = nodata = noid = 0
    bn = bbad = 0
    try:
        for i, t in enumerate(todo, 1):
            sid_path = bf._path("stock_id", t)
            sid = json.loads(sid_path.read_text()).get("id") if sid_path.exists() else None
            if not sid:
                try:
                    nm = json.loads(bf._path("stock", t).read_text()).get("companyName")
                except (OSError, ValueError):
                    nm = None
                ids = peers.get(norm(nm), set())
                if len(ids) == 1:
                    sid = next(iter(ids))
                    sid_path.write_text(json.dumps({"id": sid, "via": "peer list"}))
            if not sid:
                hits, err = cl.get("industry_search", {"query": t})
                hit = next((h for h in (hits or []) if (h.get("exchangeCodeNsi") or "").upper() == t.upper()), None)
                if hit:
                    sid = hit["id"]
                    sid_path.write_text(json.dumps({"id": sid}))
            bn += 1
            if not sid:
                noid += 1
                bbad += 1
                fails[f"id:{t}"] = "no exact NSE match"
            else:
                f0 = len(fails)
                d = bf.fetch(cl, "stock_target_price", t, {"stock_id": sid}, fails)
                if d is not None:
                    got += 1
                elif bf._path("stock_target_price_nodata", t).exists():
                    nodata += 1
                else:
                    bbad += 1
            if bn >= a.batch:
                print(f"{i}/{len(todo)} targets={got} no-data={nodata} no-id={noid} counter={bf._calls()['total']}", flush=True)
                if bbad / bn > 0.10:
                    print("STOPPED: more than 10% of the last batch failed", flush=True)
                    break
                bn = bbad = 0
    except bf.Budget as e:
        print("STOPPED:", e, flush=True)
    print(f"DONE targets={got} no-data={nodata} no-id={noid} counter={bf._calls()['total']}", flush=True)
    (bf.ROOT / "failures_targets.json").write_text(json.dumps(fails, indent=1))


if __name__ == "__main__":
    sys.exit(main())
