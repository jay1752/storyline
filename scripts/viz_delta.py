"""Ship the Snapshot view's three small market tables between machines (the full market.db is 6 GB).

  export (Mac):  python scripts/viz_delta.py export                  -> data/viz_delta.jsonl.gz
  import (VM):   python - import < scripts/viz_delta.py              (reads /app/data/viz_delta.jsonl.gz; works before the new image is built)

  index_prices  every row, replaced           holders  every row, replaced (only this loader writes it)
  deals         merged (INSERT OR IGNORE: the VM may collect its own daily deals)
Rows for a symbol the target does not carry are skipped. Creates the tables if the target does not have them yet (same definitions as app/db.py).
Nothing else is touched. A rollback note is printed with the row counts that were there before."""
import gzip
import json
import sqlite3
import sys

try:
    from app.db import DATA_DIR
except Exception:  # noqa: BLE001
    from pathlib import Path
    DATA_DIR = Path("/app/data")

FILE = DATA_DIR / "viz_delta.jsonl.gz"
TABLES = ["index_prices", "holders", "deals"]
DDL = """
CREATE TABLE IF NOT EXISTS index_prices (symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL, PRIMARY KEY (symbol, date));
CREATE TABLE IF NOT EXISTS holders (symbol TEXT NOT NULL, period_end TEXT NOT NULL, holder_name TEXT NOT NULL, category TEXT NOT NULL,
    shares REAL, pct REAL, filed_on TEXT, PRIMARY KEY (symbol, period_end, holder_name, category));
CREATE TABLE IF NOT EXISTS deals (deal_date TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL, client TEXT NOT NULL, side TEXT NOT NULL,
    qty REAL, price REAL, remarks TEXT, PRIMARY KEY (deal_date, symbol, kind, client, side, qty, price));
"""


def export() -> None:
    c = sqlite3.connect(f"file:{DATA_DIR}/market.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    n = {}
    with gzip.open(FILE, "wt") as f:
        for t in TABLES:
            k = 0
            for r in c.execute(f"SELECT * FROM {t}"):
                f.write(json.dumps({"t": t, "r": dict(r)}) + "\n")
                k += 1
            n[t] = k
    print("exported", n, "->", FILE)


def import_() -> None:
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=300)
    m.executescript(DDL)
    ours = {r[0] for r in m.execute("SELECT symbol FROM stocks")}
    before = {t: m.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    print("rows before:", before, flush=True)
    rows = {t: [] for t in TABLES}
    skipped = 0
    with gzip.open(FILE, "rt") as f:
        for line in f:
            d = json.loads(line)
            sym = d["r"].get("symbol")
            if d["t"] != "index_prices" and sym not in ours:
                skipped += 1
                continue
            rows[d["t"]].append(d["r"])
    m.execute("BEGIN")
    m.execute("DELETE FROM holders")
    for t, verb in (("index_prices", "INSERT OR REPLACE"), ("holders", "INSERT OR REPLACE"), ("deals", "INSERT OR IGNORE")):
        rs = rows[t]
        if not rs:
            continue
        cols = list(rs[0].keys())
        m.executemany(f"{verb} INTO {t} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [tuple(r[c] for c in cols) for r in rs])
    m.commit()
    after = {t: m.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    print("rows after:", after, "| skipped (stock not on this machine):", skipped, flush=True)
    print("rollback: DROP TABLE index_prices; DROP TABLE holders; DROP TABLE deals; (nothing else was changed)")


if __name__ == "__main__":
    if "export" in sys.argv:
        export()
    elif "import" in sys.argv:
        import_()
    else:
        sys.exit(__doc__)
