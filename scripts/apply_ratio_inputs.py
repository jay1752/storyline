"""Apply a file written by export_ratio_inputs.py: UPDATE the five ratio-input columns on rows that already exist here.
Never inserts and never touches any other column.  Run where the target database lives:
    docker exec -e PYTHONPATH=/app tenetfolio python /tmp/apply_ratio_inputs.py"""
import gzip
import json

from app.db import get_market_conn

COLS = ("finance_cost_cr", "current_assets_cr", "current_liabilities_cr", "inventory_cr", "cfo_cr")
sql = "UPDATE quarterly_data SET " + ", ".join(f"{c} = COALESCE(?, {c})" for c in COLS) + " WHERE symbol = ? AND period_end_date = ?"
done = seen = 0
with get_market_conn() as m, gzip.open("/app/data/ratio_inputs_delta.jsonl.gz", "rt") as f:
    batch = []
    for line in f:
        sym, end, *vals = json.loads(line)
        batch.append((*vals, sym, end)); seen += 1
        if len(batch) >= 5000:
            done += sum(m.execute(sql, b).rowcount for b in batch); m.commit(); batch = []
    done += sum(m.execute(sql, b).rowcount for b in batch); m.commit()
    print("rows in file:", seen, "| rows updated here:", done)
    print("rows here with finance cost / current assets / cfo:", [m.execute(f"SELECT COUNT(*) FROM quarterly_data WHERE {c} IS NOT NULL").fetchone()[0] for c in ("finance_cost_cr", "current_assets_cr", "cfo_cr")])
