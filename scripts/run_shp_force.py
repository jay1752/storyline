"""One-off: re-read the latest BSE shareholding filings for every Indian stock to fill FII/DII (0 where a category is
absent), promoter pledge flag and pledge %. max_filings=2 -> the two most recent quarters.
    docker compose exec -d -e PYTHONPATH=/app app python /tmp/run_shp_force.py [max_filings]"""
import sys
from datetime import datetime, timezone

from app.db import get_market_conn
from app.ingestion.bse_shareholding_source import BseShareholdingSource

now = lambda: datetime.now(timezone.utc).isoformat()
mf = int(sys.argv[1]) if len(sys.argv) > 1 else 2
with get_market_conn() as m:
    run_id = m.execute("INSERT INTO ingestion_runs (source_name, target, started_at, status) VALUES ('bse_shareholding','quarterly',?,'running')", (now(),)).lastrowid
    m.commit()
    status, err, rows = "success", None, 0
    try:
        def progress(n):
            m.execute("UPDATE ingestion_runs SET rows_written=? WHERE id=?", (n, run_id)); m.commit()
        rows = BseShareholdingSource().ingest_quarterly(m, force=True, max_filings=mf, progress=progress)
    except Exception as e:  # noqa: BLE001
        status, err = "failed", str(e)
    m.execute("UPDATE ingestion_runs SET finished_at=?, status=?, rows_written=?, error_message=? WHERE id=?", (now(), status, rows, err, run_id)); m.commit()
