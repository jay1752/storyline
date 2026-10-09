"""A real job queue — now a bounded thread pool, not a single serial worker.

History: two full-universe bulk jobs (SEC EDGAR + Tiingo) running truly
concurrently once starved each other for SQLite's single write lock — with WAL
mode off (required for the Docker bind-mount fix), only one writer can hold the
file at a time. That incident's actual root cause, found and reproduced at the
time, was narrower than "concurrency is unsafe": a job's `progress()` callback
opened a SECOND connection while the first still held an open transaction — two
connections each wanting the one write lock, safe as a deadlock. The fix then was
serializing everything through one worker thread, which sidestepped the bug
without fixing it.

The actual fix — `progress()` reusing the same connection as the rest of the job,
never opening a second one mid-transaction — has been in place since. With that
discipline (one connection per job, held for that job's lifetime only), genuinely
concurrent writers are safe: SQLite's own file lock serializes their commits via
its busy-timeout (each write blocks and retries, up to `_connect()`'s 30s), not a
deadlock — a deadlock needs a circular wait, which a single-exclusive-lock file
can't produce between independent connections each holding at most one request.

Why parallelize at all: most ingestion time is network-bound (rate-limited calls
to BSE, Tiingo, Finnhub, OpenFIGI, 5paisa), not write-bound — and every source
hits a *different* external API with its own independent rate limit, so running
them side by side doesn't make any single source's own throttle riskier. A full
BSE run and a full Tiingo run no longer have to happen one after the other; they
can happen at the same time, bounded by whichever is actually slower instead of
by their sum.

MAX_CONCURRENT_JOBS caps the pool — matches the number of distinct real sources,
so in practice every triggered source gets its own thread immediately rather than
queuing behind unrelated work. Two jobs against the SAME source still run
one-after-another in submission order (Python's GIL plus the shared `SOURCES[name]`
instance make that the natural outcome, not a special case coded for here).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app.db import get_market_conn
from app.ingestion.base import NotConfiguredError
from app.ingestion.bse_shareholding_source import BseShareholdingSource
from app.ingestion.bse_source import BseSource
from app.ingestion.finnhub_source import FinnhubSource
from app.ingestion.fred_source import FredSource
from app.ingestion.fivepaisa_source import FivePaisaSource
from app.ingestion.kite_source import KiteSource
from app.ingestion.mock_source import MockSource
from app.ingestion.moneycontrol_source import MoneyControlSource
from app.ingestion.nse_bhavcopy_source import NseBhavcopySource
from app.ingestion.nse_results_source import NseResultsSource
from app.ingestion.sec13f_source import Sec13FSource
from app.ingestion.bse_ratio_inputs_source import BseRatioInputsSource
from app.ingestion.daily_prices_source import DailyPriceSource
from app.ingestion.nse_integrated_source import NseIntegratedSource
from app.ingestion.sec_edgar_source import SecEdgarSource
from app.ingestion.tiingo_source import TiingoSource
from app.ingestion.worldbank_source import WorldBankSource
from app.ingestion.yfinance_source import YFinanceSource

SOURCES = {
    "mock": MockSource(),
    "5paisa": FivePaisaSource(),
    "kite": KiteSource(),
    "sec_edgar": SecEdgarSource(),
    "nse_integrated": NseIntegratedSource(),
    "bse_ratios": BseRatioInputsSource(),
    "daily_prices_in": DailyPriceSource("IN"),
    "daily_prices_us": DailyPriceSource("US"),
    "tiingo": TiingoSource(),
    "yfinance": YFinanceSource(),
    "nse_bhavcopy": NseBhavcopySource(),
    "nse_results": NseResultsSource(),
    "finnhub": FinnhubSource(),
    "bse": BseSource(),
    "bse_shareholding": BseShareholdingSource(),
    "moneycontrol": MoneyControlSource(),
    "sec_13f": Sec13FSource(),
    "worldbank": WorldBankSource(),
    "fred": FredSource(),
}

TARGETS = ["universe", "quarterly", "prices", "institutional", "macro"]

MAX_CONCURRENT_JOBS = 4  # walked back from 7 after real failures: running all 8 sources at once (bse_shareholding
# and moneycontrol added this session) produced repeated "database is locked" errors on 6 of 8 jobs — confirmed
# live, not theoretical. 7 concurrent writers, some (5paisa, bse) committing very frequently, exceeded the 30s
# busy-timeout under real contention. 3-4 concurrent was the level actually validated earlier this session
# (BSE + SEC EDGAR + World Bank running together, no failures) — going back to that proven number rather than
# re-guessing a higher one. See also the raised busy-timeout in app/db.py for extra headroom.
_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="ingestion-worker")


def _now():
    return datetime.now(timezone.utc).isoformat()


def start_worker():
    """Call once at app startup (see main.py). Cleans up any run left mid-flight by
    a previous process — the pool is in-memory and doesn't survive a restart, so an
    orphaned 'running'/'queued' row would otherwise sit there forever, looking like
    a live job that isn't."""
    _cleanup_stale_runs()


def _cleanup_stale_runs():
    with get_market_conn() as m:
        m.execute(
            "UPDATE ingestion_runs SET status='failed', finished_at=?, error_message='Interrupted by app restart' "
            "WHERE status IN ('queued', 'running')",
            (_now(),),
        )
        m.commit()


def enqueue_ingestion(
    source_name: str, target: str, limit: int | None = None, symbol: str | None = None, market: str | None = None
) -> int:
    """Returns immediately — the actual work happens on the pool. Safe to call while
    other jobs are already running: up to MAX_CONCURRENT_JOBS execute at once, any
    beyond that queue inside the executor until a slot frees up."""
    with get_market_conn() as m:
        cur = m.execute(
            "INSERT INTO ingestion_runs (source_name, target, started_at, status) VALUES (?,?,?, 'queued')",
            (source_name, target, _now()),
        )
        run_id = cur.lastrowid
        m.commit()

    _executor.submit(_execute, run_id, source_name, target, limit, symbol, market)
    return run_id


def _execute(run_id: int, source_name: str, target: str, limit, symbol, market=None) -> None:
    source = SOURCES[source_name]

    with get_market_conn() as m:
        m.execute("UPDATE ingestion_runs SET status='running' WHERE id=?", (run_id,))
        m.commit()

    rows_written, status, error = 0, "success", None
    try:
        with get_market_conn() as m:
            # progress() reuses THIS SAME connection — not a fresh one. See module
            # docstring: a second connection opened here, concurrently with this job's
            # own open transaction, is exactly the pattern that caused the original
            # deadlock. One connection per job, for the job's whole lifetime, is what
            # makes running multiple jobs in parallel safe against SQLite's single
            # write lock — each job's writes serialize via busy-timeout, never deadlock.
            def progress(rows_so_far: int):
                m.execute("UPDATE ingestion_runs SET rows_written=? WHERE id=?", (rows_so_far, run_id))
                m.commit()

            if target == "universe":
                rows_written = source.sync_universe(m)
            elif target == "quarterly":
                rows_written = source.ingest_quarterly(m, limit=limit, symbol=symbol, progress=progress)
            elif target == "prices":
                # market is yfinance-specific (lets a dedicated India run be triggered
                # alongside an already-running US one); every other source's
                # ingest_prices doesn't accept it, so only pass it when set.
                kwargs = {"market": market} if market else {}
                rows_written = source.ingest_prices(m, limit=limit, symbol=symbol, progress=progress, **kwargs)
            elif target == "institutional":
                rows_written = source.ingest_institutional(m, limit=limit, symbol=symbol, progress=progress)
            elif target == "macro":
                rows_written = source.ingest_macro(m, limit=limit, symbol=symbol, progress=progress)
            else:
                raise ValueError(f"unknown target: {target}")
    except NotConfiguredError as e:
        status, error = "not_configured", str(e)
    except NotImplementedError as e:
        status, error = "not_implemented", str(e)
    except Exception as e:  # noqa: BLE001 — deliberately broad, this is a job runner
        status, error = "failed", str(e)

    with get_market_conn() as m:
        m.execute(
            "UPDATE ingestion_runs SET finished_at=?, status=?, rows_written=?, error_message=? WHERE id=?",
            (_now(), status, rows_written, error, run_id),
        )
        m.commit()


def enqueue_position_analysis(run_id: int) -> None:
    """Run one queued per-stock analysis (see app/ai/insights_engine.py) on the worker pool. The run row itself
    (insight_runs) carries the status the UI polls — no ingestion_runs entry, these are user actions."""
    _executor.submit(_execute_position_analysis, run_id)


def _execute_position_analysis(run_id: int) -> None:
    from app.ai.insights_engine import execute_run

    execute_run(run_id)


def enqueue_insights(user_id: int, trigger: str = "daily", skip_fresh_hours: int = 18) -> int:
    """The daily Insights job's entry point — same enqueue/tracking shape as
    enqueue_ingestion above (an ingestion_runs row, submitted to the same thread pool),
    but it doesn't fit DataSource's symbol/market-scoped contract (it takes a user_id and
    writes users.db, not market.db), so it's a sibling function here rather than a forced
    DataSource subclass. Reuses the SAME ingestion_runs table/Admin UI rather than
    inventing a parallel job-tracking mechanism."""
    with get_market_conn() as m:
        cur = m.execute(
            "INSERT INTO ingestion_runs (source_name, target, started_at, status) VALUES ('insights', 'positions', ?, 'queued')",
            (_now(),),
        )
        run_id = cur.lastrowid
        m.commit()

    _executor.submit(_execute_insights, run_id, user_id, trigger, skip_fresh_hours)
    return run_id


def _execute_insights(run_id: int, user_id: int, trigger: str = "daily", skip_fresh_hours: int = 18) -> None:
    from app.ai.insights_engine import run_insights_for_user

    with get_market_conn() as m:
        m.execute("UPDATE ingestion_runs SET status='running' WHERE id=?", (run_id,))
        m.commit()

    rows_written, status, error = 0, "success", None
    try:
        rows_written = run_insights_for_user(user_id, trigger=trigger, skip_fresh_hours=skip_fresh_hours)
    except NotConfiguredError as e:
        status, error = "not_configured", str(e)
    except Exception as e:  # noqa: BLE001 — same deliberately-broad job-runner catch as _execute
        status, error = "failed", str(e)

    with get_market_conn() as m:
        m.execute(
            "UPDATE ingestion_runs SET finished_at=?, status=?, rows_written=?, error_message=? WHERE id=?",
            (_now(), status, rows_written, error, run_id),
        )
        m.commit()


def enqueue_community_briefing(force: bool = False) -> int:
    """The daily Community briefing (app/ai/community_briefing.py), tracked in ingestion_runs like every other job
    so it shows in Admin and a failed day is visible. Writes users.db only."""
    with get_market_conn() as m:
        cur = m.execute(
            "INSERT INTO ingestion_runs (source_name, target, started_at, status) VALUES ('community_briefing', 'daily', ?, 'queued')",
            (_now(),),
        )
        run_id = cur.lastrowid
        m.commit()
    _executor.submit(_execute_community_briefing, run_id, force)
    return run_id


def _execute_community_briefing(run_id: int, force: bool) -> None:
    from app.ai.community_briefing import run_daily_briefing

    with get_market_conn() as m:
        m.execute("UPDATE ingestion_runs SET status='running' WHERE id=?", (run_id,))
        m.commit()

    rows_written, status, error = 0, "success", None
    try:
        rows_written = run_daily_briefing(force=force)
    except Exception as e:  # noqa: BLE001 — same deliberately-broad job-runner catch as _execute
        status, error = "failed", str(e)[:500]

    with get_market_conn() as m:
        m.execute(
            "UPDATE ingestion_runs SET finished_at=?, status=?, rows_written=?, error_message=? WHERE id=?",
            (_now(), status, rows_written, error, run_id),
        )
        m.commit()
