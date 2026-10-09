"""One-off: price history for stocks that have fundamentals but no prices, via yfinance (additive, source='yfinance').
    docker compose exec -d -e PYTHONPATH=/app app python /tmp/fill_prices.py"""
from datetime import datetime, timezone

from app.db import get_market_conn
from app.ingestion.yfinance_source import YFinanceSource

now = lambda: datetime.now(timezone.utc).isoformat()
with get_market_conn() as m:
    syms = [r[0] for r in m.execute(
        "SELECT symbol FROM stocks s WHERE EXISTS (SELECT 1 FROM quarterly_data q WHERE q.symbol=s.symbol) "
        "AND NOT EXISTS (SELECT 1 FROM price_history p WHERE p.symbol=s.symbol)")]
    run_id = m.execute("INSERT INTO ingestion_runs (source_name, target, started_at, status) VALUES ('yfinance','prices',?,'running')", (now(),)).lastrowid
    m.commit()
    src, rows, got = YFinanceSource(), 0, 0
    for i, s in enumerate(syms):
        n = src.ingest_prices(m, symbol=s)
        rows += n; got += 1 if n else 0
        m.execute("UPDATE ingestion_runs SET rows_written=? WHERE id=?", (rows, run_id)); m.commit()
    m.execute("UPDATE ingestion_runs SET finished_at=?, status='success', rows_written=?, error_message=? WHERE id=?",
              (now(), rows, f"{got}/{len(syms)} symbols found a price history", run_id))
    m.commit()
