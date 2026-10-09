"""One-off: re-ingest EVERY US symbol from SEC EDGAR, ignoring the "already covered" skip, so parser
improvements (fallback revenue/debt/op-income/EPS tags, pre-revenue rows, 70-125d quarters) reach existing rows.
Records itself in ingestion_runs like any job. Run inside the app container:
    docker compose exec -d -e PYTHONPATH=/app app python /app/scripts/run_sec_force.py"""
from datetime import datetime, timezone

from app.db import get_market_conn
from app.ingestion.sec_edgar_source import SecEdgarSource

now = lambda: datetime.now(timezone.utc).isoformat()
with get_market_conn() as m:
    run_id = m.execute("INSERT INTO ingestion_runs (source_name, target, started_at, status) VALUES ('sec_edgar','quarterly',?,'running')", (now(),)).lastrowid
    m.commit()
    status, err, rows = "success", None, 0
    try:
        def progress(n):
            m.execute("UPDATE ingestion_runs SET rows_written=? WHERE id=?", (n, run_id))
            m.commit()
        rows = SecEdgarSource().ingest_quarterly(m, force=True, progress=progress)
    except Exception as e:  # noqa: BLE001
        status, err = "failed", str(e)
    m.execute("UPDATE ingestion_runs SET finished_at=?, status=?, rows_written=?, error_message=? WHERE id=?", (now(), status, rows, err, run_id))
    m.commit()
