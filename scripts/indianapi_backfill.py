"""Fetch IndianAPI data into raw JSON files (no market.db writes), under a hard call budget.

Run inside the app container so it shares the data volume:
    docker compose exec -d -w /app -e PYTHONPATH=/app -e INDIANAPI_KEY=... app python scripts/indianapi_backfill.py priority-core

Phases (one stock = the calls listed):
  priority-core    Nifty 500 + tracked positions:   historical_stats(all)                          1 call
  priority-addons  same list:                       /stock, industry_search (id), stock_target_price,
                                                    stock_forecasts EPS + SAL (Annual estimates)    5 calls
  rest-core        every other India stock with BSE data: historical_stats(all)                   1 call

Raw responses land in data/indianapi/raw/<endpoint>/<TICKER>.json; a stock already fetched is never fetched again, so
a re-run resumes. data/indianapi/calls.json counts every HTTP call (including failures) and the run stops at --budget.
Plan limits: 10,000 requests/month, 1 request/second."""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests

BASE = os.environ.get("INDIANAPI_HOST", "https://dev.indianapi.in")
ROOT = Path(os.environ.get("DATA_DIR", "/app/data")) / "indianapi"
RAW = ROOT / "raw"
CALLS = ROOT / "calls.json"
MIN_GAP = 1.1  # seconds between calls (plan: 1 request/second)


def _calls() -> dict:
    return json.loads(CALLS.read_text()) if CALLS.exists() else {"total": 0, "failed": 0, "by_endpoint": {}}


def _bump(ep: str, ok: bool) -> None:
    c = _calls()
    c["total"] += 1
    c["failed"] += 0 if ok else 1
    c["by_endpoint"][ep] = c["by_endpoint"].get(ep, 0) + 1
    CALLS.write_text(json.dumps(c))


class Budget(Exception):
    pass


class Client:
    def __init__(self, key: str, budget: int):
        self.key, self.budget, self.last, self.streak = key, budget, 0.0, 0

    def get(self, ep: str, params: dict):
        if _calls()["total"] >= self.budget:
            raise Budget(f"call budget {self.budget} reached")
        wait = MIN_GAP - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        try:
            r = requests.get(f"{BASE}/{ep}", params=params, headers={"X-Api-Key": self.key}, timeout=60)
        except requests.RequestException as e:
            _bump(ep, False)
            return None, f"net:{type(e).__name__}"
        ok = r.status_code == 200
        _bump(ep, ok)
        if r.status_code in (401, 402, 403, 429):
            raise Budget(f"HTTP {r.status_code}: {r.text[:120]}")
        try:
            body, err = (r.json() if ok else None), (None if ok else f"HTTP {r.status_code}")
        except ValueError:
            body, err = None, "bad-json"
        # An outage (seen 2026-10-09: every stats call returned {"error": "can only concatenate..."} with HTTP 200) must not
        # burn the monthly quota: 8 failures in a row stop the run.
        msg = str(body.get("error") or body.get("info") or "") if isinstance(body, dict) else ""
        definitive_no = "Data not available" in msg or "Unknown stock" in msg or "valid script_code" in msg  # coverage answer, not a fault
        broken = not definitive_no and (body is None or (isinstance(body, dict) and "error" in body and len(body) <= 2))
        self.streak = self.streak + 1 if broken else 0
        if self.streak >= 8:
            raise Budget("circuit breaker: 8 consecutive failures, provider looks down")
        return body, err


def _path(ep: str, ticker: str) -> Path:
    p = RAW / ep / f"{ticker}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def fetch(cl: Client, ep: str, ticker: str, params: dict, fails: dict, store: str | None = None) -> dict | list | None:
    """`ep` is the URL path; `store` (default: same) is the raw-folder name, so one endpoint can keep several variants."""
    p = _path(store or ep, ticker)
    if p.exists():
        return json.loads(p.read_text())
    if _path(f"{store or ep}_nodata", ticker).exists():
        return None
    data, err = cl.get(ep, params)
    if data is None or (isinstance(data, dict) and ("error" in data and len(data) <= 2 or data.get("info"))):
        why = err or str(data)[:80]
        fails[f"{ep}:{ticker}"] = why
        if "Data not available" in why or "Unknown stock" in why:
            _path(f"{store or ep}_nodata", ticker).write_text(json.dumps({"why": why}))
        return None
    p.write_text(json.dumps(data))
    return data


def priority_tickers() -> list[str]:
    from app.ingestion.funds_source import constituents
    out: list[str] = []
    u = sqlite3.connect(f"file:{os.environ.get('DATA_DIR', '/app/data')}/users.db?mode=ro", uri=True)
    out += [r[0].split(":", 1)[1] for r in u.execute("SELECT DISTINCT symbol FROM tracked_positions WHERE symbol LIKE 'NSE:%'")]
    out += [s for s, _ in (constituents("NIFTY 500") or [])]
    return list(dict.fromkeys(out))


def rest_tickers(exclude: set[str]) -> list[str]:
    m = sqlite3.connect(f"file:{os.environ.get('DATA_DIR', '/app/data')}/market.db?mode=ro", uri=True)
    rows = m.execute("SELECT DISTINCT s.symbol FROM stocks s JOIN quarterly_data q USING(symbol) WHERE s.market='IN' AND s.symbol LIKE 'NSE:%'").fetchall()
    # '&' and '-' tickers are skipped: IndianAPI resolved M&MFIN to M&M and J&KBANK to another bank, and errored on
    # BAJAJ-AUTO / NAM-INDIA. They need a lookup by company name plus an identity check, done separately.
    return [x for x in (r[0].split(":", 1)[1] for r in rows) if x not in exclude and "&" not in x and "-" not in x]


def identity_ok(t: str, m: sqlite3.Connection):
    """True/False when IndianAPI's latest quarters agree/disagree with our BSE rows (|sales|, >15% median gap = wrong company);
    None when fewer than 2 quarters can be compared. Absolute values, so our not-yet-repaired loss signs can't cause a false alarm."""
    import calendar
    import statistics
    from datetime import date
    p = RAW / "historical_stats" / f"{t}.json"
    if not p.exists():
        return None
    q = json.loads(p.read_text()).get("quarter_results") or {}
    sales = q.get("Sales") or q.get("Revenue") or {}
    abbr = {mn: i for i, mn in enumerate(calendar.month_abbr) if mn}
    ours = {r[0]: r[1] for r in m.execute("SELECT period_end_date, revenue_cr FROM quarterly_data WHERE symbol=? AND source='bse'", (f"NSE:{t}",))}
    diffs = []
    for lab, v in sales.items():
        try:
            mon, yr = lab.split()
            d = date(int(yr), abbr[mon], calendar.monthrange(int(yr), abbr[mon])[1]).isoformat()
        except (ValueError, KeyError):
            continue
        o = ours.get(d)
        if o and v:
            diffs.append(abs(abs(v) - abs(o)) / abs(o))
    return None if len(diffs) < 2 else statistics.median(diffs) <= 0.15


def run_batches(cl: Client, tickers: list[str], schedule: str, fails: dict) -> None:
    """Fetch not-yet-fetched tickers in growing batches; after each batch, stop if the fetch failure rate exceeds 10% or
    more than 10% of identity-checkable stocks disagree with BSE. `schedule` = 'SIZExCOUNT,...' (COUNT 0 = until done)."""
    m = sqlite3.connect(f"file:{os.environ.get('DATA_DIR', '/app/data')}/market.db?mode=ro", uri=True)
    todo = [t for t in tickers if not _path("historical_stats", t).exists() and not _path("historical_stats_nodata", t).exists()]
    print(f"rest-core batches: {len(todo)} tickers still to fetch", flush=True)
    steps = []
    for part in schedule.split(","):
        size, count = part.split("x")
        steps.append((int(size), int(count)))
    n = 0
    for size, count in steps:
        done = 0
        while todo and (count == 0 or done < count):
            batch, todo = todo[:size], todo[size:]
            before = _calls()["total"]
            f0 = len(fails)
            for t in batch:
                fetch(cl, "historical_stats", t, {"stock_name": t, "stats": "all"}, fails)
            n += 1
            done += 1
            got = [t for t in batch if _path("historical_stats", t).exists()]
            idc = [identity_ok(t, m) for t in got]
            chk = [x for x in idc if x is not None]
            bad = [t for t, x in zip(got, idc) if x is False]
            new = list(fails.values())[f0:]
            faults = [w for w in new if "Data not available" not in w and "Unknown stock" not in w]
            fail_rate = len(faults) / len(batch)
            bad_rate = len(bad) / len(chk) if chk else 0
            print(f"batch {n} size {len(batch)}: fetched {len(got)}, no-data {len(new) - len(faults)}, faults {len(faults)} ({fail_rate:.0%}), identity-checked {len(chk)}, "
                  f"mismatch {len(bad)} ({bad_rate:.0%}) {bad[:6]}, calls {_calls()['total'] - before} (total {_calls()['total']})", flush=True)
            if fail_rate > 0.10 or bad_rate > 0.10:
                print("STOPPED: batch failed the health gate; investigate before continuing", flush=True)
                (ROOT / "failures_rest-core.json").write_text(json.dumps(fails, indent=1))
                return
    (ROOT / "failures_rest-core.json").write_text(json.dumps(fails, indent=1))
    print("DONE rest-core", _calls(), "fails", len(fails), flush=True)


def run_extras(cl: Client, fails: dict) -> None:
    """1) '&'/'-' tickers and earlier failures, looked up by company name with an identity check (replacing wrong-entity files);
    2) add-on gaps for priority stocks; 3) next-quarter (Interim) EPS and annual net-profit estimates for priority stocks."""
    m = sqlite3.connect(f"file:{os.environ.get('DATA_DIR', '/app/data')}/market.db?mode=ro", uri=True)
    names = dict(m.execute("SELECT substr(symbol,5), name FROM stocks WHERE market='IN' AND symbol LIKE 'NSE:%'").fetchall())
    special = [t for t in names if ("&" in t or "-" in t)]
    print(f"extras: {len(special)} special tickers", flush=True)
    for t in special:
        p = _path("historical_stats", t)
        if p.exists() and identity_ok(t, m) is not False:
            continue  # already have a verified-or-uncheckable copy
        if p.exists():
            p.unlink()  # wrong entity (e.g. M&MFIN -> M&M)
        if _path("historical_stats_nodata", t).exists():
            continue
        hit = None
        for q in (names[t], t):
            hits, err = cl.get("industry_search", {"query": q})
            hit = next((h for h in (hits or []) if (h.get("exchangeCodeNsi") or "").upper() == t.upper()), None)
            if hit:
                break
        if not hit:
            fails[f"name-lookup:{t}"] = "no exact NSE-code match"
            continue
        _path("stock_id", t).write_text(json.dumps({"id": hit["id"]}))
        data, err = cl.get("historical_stats", {"stock_name": hit["commonName"], "stats": "all"})
        if not data or "quarter_results" not in data:
            fails[f"historical_stats:{t}"] = err or str(data)[:80]
            continue
        p.write_text(json.dumps(data))
        if identity_ok(t, m) is False:
            p.unlink()
            fails[f"identity:{t}"] = f"{hit['commonName']} disagrees with BSE revenue"
        else:
            print("  ok by name:", t, "->", hit["commonName"], flush=True)
    prio = priority_tickers()
    for t in prio:  # add-on gaps
        if "&" in t or "-" in t:
            continue
        if not _path("stock", t).exists():
            fetch(cl, "stock", t, {"name": t}, fails)
        if not _path("stock_target_price", t).exists() and _path("stock_id", t).exists():
            fetch(cl, "stock_target_price", t, {"stock_id": json.loads(_path("stock_id", t).read_text())["id"]}, fails)
    for label, params in (("stock_forecasts_EPSq", {"measure_code": "EPS", "period_type": "Interim"}), ("stock_forecasts_NET", {"measure_code": "NET", "period_type": "Annual"})):
        n = 0
        for t in prio:
            sp = _path("stock_id", t)
            if not sp.exists() or _path("stock_forecasts_EPS", t).exists() is False:
                continue  # no id, or the stock had no estimates in the first pass
            fetch(cl, "stock_forecasts", t, {"stock_id": json.loads(sp.read_text())["id"], "data_type": "Estimates", "age": "Current", **params}, fails, store=label)
            n += 1
            if n % 100 == 0:
                print(f"{label}: {n} calls={_calls()['total']}", flush=True)
        print(f"{label} done: {n} stocks", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["priority-core", "priority-addons", "rest-core", "extras"])
    ap.add_argument("--budget", type=int, default=9500)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--schedule", default="", help="rest-core only, e.g. 50x5,100x2,200x0 (size x count; count 0 = until done)")
    a = ap.parse_args()
    key = os.environ.get("INDIANAPI_KEY")
    if not key:
        sys.exit("INDIANAPI_KEY not set")
    ROOT.mkdir(parents=True, exist_ok=True)
    cl, fails = Client(key, a.budget), {}
    if a.phase == "extras":
        try:
            run_extras(cl, fails)
        except Budget as e:
            print("STOPPED:", e, flush=True)
        (ROOT / "failures_extras.json").write_text(json.dumps(fails, indent=1))
        print("DONE extras", _calls(), "fails", len(fails), flush=True)
        return
    prio = priority_tickers()
    tickers = prio if a.phase.startswith("priority") else rest_tickers(set(prio))
    if a.limit:
        tickers = tickers[:a.limit]
    print(f"{a.phase}: {len(tickers)} tickers, budget {a.budget}, used so far {_calls()['total']}", flush=True)
    t0 = time.time()
    if a.phase == "rest-core" and a.schedule:
        try:
            run_batches(cl, tickers, a.schedule, fails)
        except Budget as e:
            print("STOPPED:", e, flush=True)
            (ROOT / "failures_rest-core.json").write_text(json.dumps(fails, indent=1))
        return
    try:
        for i, t in enumerate(tickers, 1):
            if a.phase.endswith("core"):
                fetch(cl, "historical_stats", t, {"stock_name": t, "stats": "all"}, fails)
            else:
                fetch(cl, "stock", t, {"name": t}, fails)
                sid_path = _path("stock_id", t)
                if sid_path.exists():
                    sid = json.loads(sid_path.read_text()).get("id")
                else:
                    hits, err = cl.get("industry_search", {"query": t})
                    sid = next((h["id"] for h in (hits or []) if (h.get("exchangeCodeNsi") or "").upper() == t.upper()), None)
                    if sid:
                        sid_path.write_text(json.dumps({"id": sid}))
                    else:
                        fails[f"industry_search:{t}"] = err or "no exact NSE match"
                if sid:
                    fetch(cl, "stock_target_price", t, {"stock_id": sid}, fails)
                    for m in ("EPS", "SAL"):
                        fetch(cl, "stock_forecasts", t, {"stock_id": sid, "measure_code": m, "period_type": "Annual", "data_type": "Estimates", "age": "Current"}, fails, store=f"stock_forecasts_{m}")
            if i % 50 == 0:
                print(f"{i}/{len(tickers)} calls={_calls()['total']} fails={len(fails)} {time.time()-t0:.0f}s", flush=True)
    except Budget as e:
        print("STOPPED:", e, flush=True)
    (ROOT / f"failures_{a.phase}.json").write_text(json.dumps(fails, indent=1))
    print("DONE", a.phase, "calls", _calls(), "fails", len(fails), flush=True)


if __name__ == "__main__":
    main()
