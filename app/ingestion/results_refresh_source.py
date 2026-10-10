"""After a company's results date, pull its new quarter.

Candidates are results_calendar rows still 'upcoming' whose date is today or up to 6 days back (filings can lag the board meeting by a day or
more). Per stock: (1) BSE filings, re-ingested for that one symbol (free, always); (2) if INDIANAPI_KEY is set, the stock is covered by
IndianAPI and the monthly call cap allows, one /historical_stats call, then the same loader/apply/gate scripts as the bulk backfill, restricted
to that symbol (IndianAPI stays the source of record wherever it has the quarter; the basis gate still decides). The row becomes 'refreshed' once
the quarter that ended just before the result date is present in quarterly_data, else it is retried next run (max 5 tries over the window).
Finally the stock's analytics metrics (stock_metrics) are rebuilt so screens and the stock page show the new quarter."""
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
from app.ingestion.bse_source import BseSource

LOOKBACK_DAYS = 6
MAX_TRIES = 5
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


def _run_scripts(sym: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(SCRIPTS.parent)}
    for script, extra in (("load_indianapi.py", []), ("apply_indianapi_quarterly.py", ["--apply"]), ("apply_indianapi_annual.py", []),
                          ("apply_indianapi_holdings.py", ["--apply"]), ("apply_indianapi_gate.py", ["--apply"])):
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
            """SELECT symbol, event_date, tries FROM results_calendar
               WHERE status = 'upcoming' AND event_date <= ? AND event_date >= ? AND tries < ? ORDER BY event_date""",
            (today.isoformat(), (today - timedelta(days=LOOKBACK_DAYS)).isoformat(), MAX_TRIES)).fetchall()
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol]
        if limit:
            rows = rows[:limit]
        key = os.environ.get("INDIANAPI_KEY", "").strip()
        fetcher, client = (None, None)
        if key:
            fetcher = _fetcher()
            client = fetcher.Client(key, 10 ** 9)  # our own monthly cap (ia_usage) governs; the client adds pacing and the outage breaker
        bse, done, notes = BseSource(), 0, []
        ia_stopped = None
        for r in rows:
            sym, ev = r["symbol"], r["event_date"]
            qend, note = expected_quarter_end(ev), []
            try:
                bse.ingest_quarterly(conn, symbol=sym)
                conn.commit()
            except Exception as e:  # noqa: BLE001 - one stock failing must not stop the rest
                note.append(f"bse: {type(e).__name__}")
            wants_ia = bool(client) and _ia_eligible(conn, sym)
            if wants_ia and not ia_stopped:
                if _used(conn) >= MONTHLY_CAP:
                    ia_stopped = f"IndianAPI monthly cap {MONTHLY_CAP} reached"
                else:
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
                            _run_scripts(sym)
                        else:
                            note.append(f"indianapi: {err or 'no data'}")
                    except Exception as e:  # noqa: BLE001 - Budget (401/402/403/429, breaker) stops IA for this run, not BSE
                        ia_stopped = f"IndianAPI stopped: {str(e)[:80]}"
            src = _quarter_source(conn, sym, qend)
            got = src is not None
            # IndianAPI usually lags the filing by a day or two: while it is wanted and has not got the quarter, keep the row open
            # (BSE's figures are already in), and accept BSE-only after a 3rd attempt.
            final = got and (not wants_ia or src == "indianapi" or r["tries"] + 1 >= 3)
            if got and not final:
                note.append("filed; waiting for IndianAPI")
            conn.execute("UPDATE results_calendar SET status = ?, tries = tries + 1, last_try = ?, note = ? WHERE symbol = ? AND event_date = ?",
                         ("refreshed" if final else "upcoming", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "; ".join(note) or None, sym, ev))
            conn.commit()
            if got:
                done += 1 if final else 0
                try:
                    from app import discovery
                    discovery.build_metrics(only=sym)
                except Exception as e:  # noqa: BLE001
                    notes.append(f"{sym} metrics: {type(e).__name__}")
            if progress:
                progress(done)
        if ia_stopped:
            notes.append(ia_stopped)
        if not key:
            notes.append("IndianAPI key not set: BSE filings only")
        if notes:
            print("results_refresh:", "; ".join(notes))
        return done
