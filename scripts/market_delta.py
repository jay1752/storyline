"""Ship repaired India fundamentals between machines as a small delta file instead of a 6 GB database.

  export (Mac):  python scripts/market_delta.py export            -> data/market_delta.jsonl.gz
  import (VM):   python scripts/market_delta.py import [file]     -> replaces India quarterly_data, annual_data, ia_basis_status,
                                                                     quarterly_data_original; first writes a rollback copy of the
                                                                     VM's India quarterly_data to data/quarterly_pre_delta.jsonl.gz
US rows, prices, users and every other table are untouched. Columns are matched by name (a column one side lacks is skipped)."""
import gzip
import json
import sqlite3
import sys

from app.db import DATA_DIR

OUT = DATA_DIR / "market_delta.jsonl.gz"
TABLES = ["quarterly_data", "quarterly_data_original", "annual_data", "ia_basis_status"]


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
            if t != cur:
                cur, have = t, set(cols(m, t))
                m.execute(f"DELETE FROM {t} {india}")
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
