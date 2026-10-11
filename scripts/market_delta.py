"""Ship repaired India fundamentals between machines as a small delta file instead of a 6 GB database.

  export (Mac):  python scripts/market_delta.py export            -> data/market_delta.jsonl.gz
  import (VM):   python scripts/market_delta.py import [file]     -> replaces India quarterly_data, annual_data, ia_basis_status,
                                                                     quarterly_data_original; first writes a rollback copy of the
                                                                     VM's India quarterly_data to data/quarterly_pre_delta.jsonl.gz
Also shipped (2026-10-12 data-quality release): the whole of india_mcap, us_annual and us_mcap_override (replaced); the Yahoo-sourced US quarters
(source 'yahoo_adr', inserted) and every US SEC quarter MERGED - a missing row is inserted, an existing row only has its EMPTY columns filled, so
newer filings the VM ingested itself are never overwritten; and sector / cap_tier of every stock present on both sides.
Prices, users and every other table are untouched. Columns are matched by name (a column one side lacks is skipped)."""
import gzip
import json
import sqlite3
import sys

from app.db import DATA_DIR

OUT = DATA_DIR / "market_delta.jsonl.gz"
TABLES = ["quarterly_data", "quarterly_data_original", "annual_data", "ia_basis_status"]
REPLACE_ALL = ["india_mcap", "us_annual", "us_mcap_override"]   # whole tables, replaced
US_MERGE = "us_quarterly_merge"                                   # pseudo-table name in the file
STOCK_META = "stock_meta"


def cols(c, t):
    return [r[1] for r in c.execute(f"PRAGMA table_info({t})")]


def export() -> None:
    c = sqlite3.connect(f"file:{DATA_DIR}/market.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    n = {}
    with gzip.open(OUT, "wt") as f:
        for t in TABLES:
            where = "WHERE symbol IN (SELECT symbol FROM stocks WHERE market='IN')"
            k = 0
            for r in c.execute(f"SELECT * FROM {t} {where}"):
                f.write(json.dumps({"t": t, "r": {x: r[x] for x in r.keys() if x != "id"}}) + "\n")
                k += 1
            n[t] = k
        for t in REPLACE_ALL:
            k = 0
            for r in c.execute(f"SELECT * FROM {t}"):
                f.write(json.dumps({"t": t, "r": {x: r[x] for x in r.keys() if x != "id"}}) + "\n")
                k += 1
            n[t] = k
        k = 0
        for r in c.execute("SELECT * FROM quarterly_data WHERE symbol IN (SELECT symbol FROM stocks WHERE market='US')"):
            f.write(json.dumps({"t": US_MERGE, "r": {x: r[x] for x in r.keys() if x != "id"}}) + "\n")
            k += 1
        n[US_MERGE] = k
        k = 0
        for r in c.execute("SELECT symbol, sector, cap_tier FROM stocks"):
            f.write(json.dumps({"t": STOCK_META, "r": dict(r)}) + "\n")
            k += 1
        n[STOCK_META] = k
    print("exported", n, "->", OUT)


def import_(path) -> None:
    from app.db import init_db
    init_db()
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=300)
    m.row_factory = sqlite3.Row
    india = "WHERE symbol IN (SELECT symbol FROM stocks WHERE market='IN')"
    with gzip.open(DATA_DIR / "quarterly_pre_delta.jsonl.gz", "wt") as f:  # rollback copy
        for r in m.execute(f"SELECT * FROM quarterly_data {india}"):
            f.write(json.dumps({x: r[x] for x in r.keys() if x != "id"}) + "\n")
    m.execute("CREATE TABLE IF NOT EXISTS quarterly_data_original AS SELECT *, CURRENT_TIMESTAMP AS archived_at FROM quarterly_data WHERE 0")
    m.execute("CREATE TABLE IF NOT EXISTS ia_basis_status (symbol TEXT PRIMARY KEY, status TEXT NOT NULL, np_agree INTEGER, n_overlap INTEGER, checked_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    m.execute("BEGIN")
    cur, have, counts = None, set(), {}
    with gzip.open(path, "rt") as f:  # streamed: the VM has little memory to spare
        for line in f:
            d = json.loads(line)
            t, r = d["t"], d["r"]
            if t == STOCK_META:
                counts[t] = counts.get(t, 0) + m.execute("UPDATE stocks SET sector = COALESCE(?, sector), cap_tier = COALESCE(?, cap_tier) WHERE symbol = ?",
                                                         (r["sector"], r["cap_tier"], r["symbol"])).rowcount
                continue
            if t == US_MERGE:
                if t != cur:
                    cur, have = t, set(cols(m, "quarterly_data"))
                    counts[t] = 0
                ks = [k for k in r if k in have]
                m.execute(f"INSERT OR IGNORE INTO quarterly_data ({', '.join(ks)}) VALUES ({', '.join('?' * len(ks))})", [r[k] for k in ks])
                fill = [k for k in ks if k not in ("symbol", "period_end_date") and r[k] is not None]
                if fill:  # an existing row keeps what it has; only its empty columns are filled
                    m.execute(f"UPDATE quarterly_data SET {', '.join(f'{k} = COALESCE({k}, ?)' for k in fill)} WHERE symbol = ? AND period_end_date = ?",
                              [r[k] for k in fill] + [r["symbol"], r["period_end_date"]])
                counts[t] += 1
                continue
            if t != cur:
                cur, have = t, set(cols(m, t))
                m.execute(f"DELETE FROM {t}" if t in REPLACE_ALL else f"DELETE FROM {t} {india}")
                counts[t] = 0
            ks = [k for k in r if k in have]
            m.execute(f"INSERT OR REPLACE INTO {t} ({', '.join(ks)}) VALUES ({', '.join('?' * len(ks))})", [r[k] for k in ks])
            counts[t] += 1
    print("replaced:", counts, flush=True)
    m.commit()
    print("quick_check:", m.execute("PRAGMA quick_check").fetchone()[0])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "export":
        export()
    elif len(sys.argv) > 1 and sys.argv[1] == "import":
        import_(sys.argv[2] if len(sys.argv) > 2 else OUT)
    else:
        sys.exit(__doc__)
