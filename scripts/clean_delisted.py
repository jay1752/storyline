"""Remove delisted/defunct stocks everywhere. Input: /tmp/delist_cands.json from the finder (symbol, name, market, alive, live_last).
Safety: skips anything a user tracks or has in a pending import; keeps stocks Yahoo shows trading in the last 45 days (thin OTC);
exports everything it deletes to data/delisted_backup-<date>.json.gz; adds the stocks to excluded_stocks so universe syncs
don't re-add them.   docker compose exec -T -e PYTHONPATH=/app app python /tmp/clean_delisted.py"""
import datetime as dt
import gzip
import json
import re

from app.db import get_market_conn, get_users_conn

today = dt.date(2026, 10, 8)
recent = (today - dt.timedelta(days=45)).isoformat()
cands = [c for c in json.load(open("/tmp/delist_cands.json")) if not c["alive"] and not (c["live_last"] and c["live_last"] >= recent)]
syms = [c["symbol"] for c in cands]
print("delisted candidates after thin-trading safeguard:", len(syms))

with get_users_conn() as u:
    held = {r[0] for r in u.execute("SELECT symbol FROM tracked_positions")}
    pend = " ".join(r[0] for r in u.execute("SELECT payload_json FROM pending_imports WHERE status='pending'"))
protected = {s for s in syms if s in held or s in pend}
if protected:
    print("PROTECTED (a user tracks it / has it in a pending import) — left alone:", sorted(protected))
syms = [s for s in syms if s not in protected]
ph = ",".join("?" * len(syms))

with get_market_conn() as m:
    tables = {"stocks": "symbol", "quarterly_data": "symbol", "price_history": "symbol", "institutional_holdings": "symbol",
              "nse_results_attempts": "symbol", "bse_artifact_rows_backup": "symbol"}
    backup, counts = {}, {}
    for t, col in tables.items():
        cur = m.execute(f"SELECT * FROM {t} WHERE {col} IN ({ph})", syms)
        names = [d[0] for d in cur.description]
        rows = cur.fetchall()
        backup[t] = {"columns": names, "rows": [list(r) for r in rows]}
        counts[t] = len(rows)
    path = f"/app/data/delisted_backup-{today.isoformat()}.json.gz"
    with gzip.open(path, "wt") as f:
        json.dump({"deleted_on": today.isoformat(), "reason": "delisted/defunct (not on current exchange lists, no trade for weeks, Yahoo confirms)", "stocks": cands, "data": backup}, f)
    print("backup written:", path, counts)

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    m.execute(f"INSERT OR IGNORE INTO excluded_stocks (symbol, exchange, name, sector, market, cap_tier, excluded_at) "
              f"SELECT symbol, exchange, name, sector, market, cap_tier, ? FROM stocks WHERE symbol IN ({ph})", [now] + syms)
    for t, col in tables.items():
        if t == "stocks":
            continue
        m.execute(f"DELETE FROM {t} WHERE {col} IN ({ph})", syms)
    m.execute(f"DELETE FROM stocks WHERE symbol IN ({ph})", syms)
    m.commit()
    left = {t: m.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} IN ({ph})", syms).fetchone()[0] for t, col in tables.items()}
    print("rows remaining for deleted symbols:", left)

# cap-tier file used by the startup backfill
p = "/app/app/data/cap_tiers.json"
tiers = json.load(open(p))
for s in syms:
    tiers.pop(s, None)
json.dump(tiers, open(p, "w"), indent=0)
print("done; deleted", len(syms))
