"""After a company's results date, pull its new quarter from IndianAPI, and only that quarter.

IndianAPI is the one source for new quarters (user decision 2026-10-18: no mixing of sources inside a quarter; BSE is no longer called here).
It usually has a company's quarter some days after the filing, so each stock is checked on a spaced schedule (CHECK_AFTER_DAYS) instead of daily,
the biggest companies first, which also spends far fewer of the monthly calls. Candidates are results_calendar rows still 'upcoming' whose date
is within LOOKBACK_DAYS. Per stock: one /historical_stats call, then the loader and the apply scripts restricted to that stock AND that one
quarter (--period): the quarter's P&L, its shareholding and, for a March quarter, that fiscal year's annual row. Nothing older is touched.
The row becomes 'refreshed' once the quarter is stored with source indianapi; until then the stock page says the results are filed and the data
is awaited. A quarter whose net profit disagrees with the filing already stored (another basis) is left as filed and noted.
The stock's analytics metrics (stock_metrics) are rebuilt after each new quarter so screens and the stock page show it."""
import importlib.util
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from app.db import DATA_DIR
from app.ingestion.base import DataSource

LOOKBACK_DAYS = 25
CHECK_AFTER_DAYS = [1, 3, 6, 9, 13, 18, 24]  # the n-th check happens this many days after the results date or later
MAX_TRIES = len(CHECK_AFTER_DAYS)
MONTHLY_CAP = int(os.environ.get("INDIANAPI_MONTHLY_CAP", "450"))  # free plan = 500/month; keep a margin
IA_HOST = "https://stock.indianapi.in"
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def expected_quarter_end(event_date: str) -> str:
    """The quarter that ended before the result date (a board meeting on 10 Oct reports the quarter ended 30 Sep)."""
    d = date.fromisoformat(event_date)
    ends = [date(y, m, day) for y in (d.year, d.year - 1) for m, day in ((3, 31), (6, 30), (9, 30), (12, 31))]
    return max(q for q in ends if q < d).isoformat()


def _fetcher():
    spec = importlib.util.spec_from_file_location("ia_fetcher", SCRIPTS / "indianapi_backfill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BASE = IA_HOST  # the dev host is unreliable
    return mod


def _usage_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _used(conn) -> int:
    row = conn.execute("SELECT calls FROM ia_usage WHERE month = ?", (_usage_month(),)).fetchone()
    return row[0] if row else 0


def _bump(conn, n=1) -> None:
    conn.execute("INSERT INTO ia_usage (month, calls) VALUES (?, ?) ON CONFLICT(month) DO UPDATE SET calls = calls + ?", (_usage_month(), n, n))
    conn.commit()


def _ia_eligible(conn, sym: str) -> bool:
    ticker = sym.split(":")[-1]
    if "&" in ticker or "-" in ticker:  # these tickers resolve unreliably at IndianAPI
        return False
    bad = conn.execute("SELECT 1 FROM ia_basis_status WHERE symbol = ? AND status = 'reverted'", (sym,)).fetchone() if _has(conn, "ia_basis_status") else None
    if bad:
        return False
    try:
        from app.indianapi_db import get_indianapi_conn
        c = get_indianapi_conn()
        try:
            row = c.execute("SELECT status FROM ia_stock WHERE symbol = ?", (sym,)).fetchone()
        finally:
            c.close()
    except Exception:  # noqa: BLE001 - no staging file means no coverage knowledge: let the fetch decide
        return True
    return row is None or row[0] != "held_out"


def _has(conn, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _run_scripts(sym: str, qend: str) -> None:
    """Load the stock into the staging DB, then apply only the quarter that just reported (and its fiscal year when it is a March quarter).
    The basis gate is not re-run: it judges a stock's whole history and can delete it, which a one-quarter refresh must never do."""
    env = {**os.environ, "PYTHONPATH": str(SCRIPTS.parent)}
    steps = [("load_indianapi.py", []), ("apply_indianapi_quarterly.py", ["--apply", "--period", qend]), ("apply_indianapi_holdings.py", ["--apply", "--period", qend])]
    if qend.endswith("-03-31"):
        steps.append(("apply_indianapi_annual.py", ["--period", qend]))
    for script, extra in steps:
        r = subprocess.run([sys.executable, str(SCRIPTS / script), "--symbols", sym, *extra], cwd=str(SCRIPTS.parent), env=env, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"{script}: {(r.stderr or r.stdout)[-200:]}")


def _quarter_source(conn, sym: str, qend: str) -> str | None:
    """Source of the stored row for that quarter, or None when it is not there yet."""
    row = conn.execute("SELECT source FROM quarterly_data WHERE symbol = ? AND period_end_date = ? AND (revenue_cr IS NOT NULL OR net_profit_cr IS NOT NULL)", (sym, qend)).fetchone()
    return row[0] if row else None


class ResultsRefreshSource(DataSource):
    name = "results_refresh"

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("results_refresh runs as a daily job only.")

    def ingest_prices(self, conn, limit: Optional[int] = None, symbol: Optional[str] = None, progress: Optional[Callable[[int], None]] = None) -> int:
        today = date.today()
        rows = conn.execute(
            """SELECT c.symbol, c.event_date, c.tries FROM results_calendar c LEFT JOIN stock_metrics s ON s.symbol = c.symbol
               WHERE c.status = 'upcoming' AND c.event_date <= ? AND c.event_date >= ? AND c.tries < ? ORDER BY COALESCE(s.mcap, 0) DESC""",
            (today.isoformat(), (today - timedelta(days=LOOKBACK_DAYS)).isoformat(), MAX_TRIES)).fetchall()
        # spaced checks: the n-th check waits CHECK_AFTER_DAYS[n] days from the results date (IndianAPI lags the filing)
        rows = [r for r in rows if (today - date.fromisoformat(r["event_date"])).days >= CHECK_AFTER_DAYS[r["tries"]]]
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol]
        if limit:
            rows = rows[:limit]
        key = os.environ.get("INDIANAPI_KEY", "").strip()
        if not key:
            print("results_refresh: IndianAPI key not set, nothing to refresh")
            return 0
        fetcher = _fetcher()
        client = fetcher.Client(key, 10 ** 9)  # our own monthly cap (ia_usage) governs; the client adds pacing and the outage breaker
        done, notes, ia_stopped = 0, [], None
        for r in rows:
            sym, ev = r["symbol"], r["event_date"]
            qend, note = expected_quarter_end(ev), []
            if not _ia_eligible(conn, sym):
                conn.execute("UPDATE results_calendar SET status = 'not_covered', last_try = ?, note = ? WHERE symbol = ? AND event_date = ?",
                             (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "not covered by IndianAPI", sym, ev))
                conn.commit()
                continue
            if ia_stopped:
                break                                           # budget or outage: leave the rest for the next run, tries untouched
            if _used(conn) >= MONTHLY_CAP:
                ia_stopped = f"IndianAPI monthly cap {MONTHLY_CAP} reached"
                break
            try:
                p = fetcher._path("historical_stats", sym.split(":")[-1])
                if p.exists() and datetime.now().timestamp() - p.stat().st_mtime < 20 * 3600:
                    body, err = json.loads(p.read_text()), None  # fetched earlier today (a failed apply is retried without a new call)
                else:
                    _bump(conn)
                    body, err = client.get("historical_stats", {"stock_name": sym.split(":")[-1], "stats": "all"})
                    if isinstance(body, dict) and "error" not in body and body:
                        p.write_text(json.dumps(body))
                if isinstance(body, dict) and "error" not in body and body:
                    _run_scripts(sym, qend)
                else:
                    note.append(f"indianapi: {err or 'no data'}")
            except Exception as e:  # noqa: BLE001 - budget (401/402/403/429, breaker) stops IndianAPI for this run
                ia_stopped = f"IndianAPI stopped: {str(e)[:80]}"
                break
            got = _quarter_source(conn, sym, qend) == "indianapi"
            if not got:
                note.append("results filed; quarter not yet at IndianAPI")
            conn.execute("UPDATE results_calendar SET status = ?, tries = tries + 1, last_try = ?, note = ? WHERE symbol = ? AND event_date = ?",
                         ("refreshed" if got else "upcoming", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "; ".join(note) or None, sym, ev))
            conn.commit()
            if got:
                done += 1
                try:
                    from app import discovery
                    discovery.build_metrics(only=sym)
                except Exception as e:  # noqa: BLE001
                    notes.append(f"{sym} metrics: {type(e).__name__}")
            if progress:
                progress(done)
        if ia_stopped:
            notes.append(ia_stopped)
        if notes:
            print("results_refresh:", "; ".join(notes))
        return done
