"""Write the ratio-input columns (only rows that have any) to a compact gzip file, so they can be applied to another
database (the live VM) without copying the 6 GB file.  Run where the data was ingested:
    docker compose exec -T -e PYTHONPATH=/app app python /tmp/export_ratio_inputs.py"""
import gzip
import json

from app.db import get_market_conn

COLS = ("finance_cost_cr", "current_assets_cr", "current_liabilities_cr", "inventory_cr", "cfo_cr")
with get_market_conn() as m:
    q = ("SELECT symbol, period_end_date, " + ", ".join(COLS) + " FROM quarterly_data WHERE " + " OR ".join(f"{c} IS NOT NULL" for c in COLS))
    n = 0
    with gzip.open("/app/data/ratio_inputs_delta.jsonl.gz", "wt") as f:
        for r in m.execute(q):
            f.write(json.dumps(list(r)) + "\n"); n += 1
print("exported rows:", n)
