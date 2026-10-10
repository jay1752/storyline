"""Announced results dates for Indian stocks, from the exchanges themselves (free, official).

Three feeds, merged: NSE's event calendar (next ~2 weeks, purpose 'Financial Results...'), NSE's board-meetings list (any date range; we ask for
the next 60 days) and BSE's 'forthcoming results' list (covers BSE-only names). Rows go to results_calendar (symbol, event_date); a refetch updates
purpose/source but keeps status and tries, and a stock whose date moved loses its old upcoming row. Used by the stock page ("Results in 4 days")
and by the post-results refresh (app/ingestion/results_refresh_source.py)."""
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

import requests

from app.ingestion import bse_source as bse
from app.ingestion.base import DataSource

NSE_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"}
WINDOW_DAYS = 60


def _d(text: str, fmts) -> str | None:
    for f in fmts:
        try:
            return datetime.strptime(text.strip(), f).date().isoformat()
        except (ValueError, AttributeError):
            continue
    return None


def _nse_event_calendar():
    for x in requests.get("https://www.nseindia.com/api/event-calendar", params={"index": "equities"}, headers=NSE_UA, timeout=30).json():
        d = _d(x.get("date", ""), ("%d-%b-%Y",))
        if d and "esult" in (x.get("purpose") or ""):
            yield f"NSE:{x['symbol']}", d, x["purpose"], "nse"


def _nse_board_meetings():
    today = date.today()
    params = {"index": "equities", "from_date": (today - timedelta(days=3)).strftime("%d-%m-%Y"), "to_date": (today + timedelta(days=WINDOW_DAYS)).strftime("%d-%m-%Y")}
    for x in requests.get("https://www.nseindia.com/api/corporate-board-meetings", params=params, headers=NSE_UA, timeout=45).json():
        d = _d(x.get("bm_date", ""), ("%d-%b-%Y",))
        if d and "esult" in (x.get("bm_purpose") or ""):
            yield f"NSE:{x['bm_symbol']}", d, x["bm_purpose"], "nse"


def _bse_results():
    scrip = {str(r["SCRIP_CD"]): f"NSE:{r['scrip_id']}" for r in bse.BseSource()._scrip_rows() if r.get("scrip_id") and r.get("SCRIP_CD")}
    for x in requests.get(bse.API_BASE + "/Corpforthresults/w", headers=bse.HEADERS, timeout=30).json():
        d, sym = _d(x.get("meeting_date", ""), ("%d %b %Y",)), scrip.get(str(x.get("scrip_Code")))
        if d and sym:
            yield sym, d, "Financial Results", "bse"


class ResultsCalendarSource(DataSource):
    name = "results_calendar"

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("results_calendar only provides dates.")

    def ingest_prices(self, conn, limit: Optional[int] = None, symbol: Optional[str] = None, progress: Optional[Callable[[int], None]] = None) -> int:
        known = {r[0] for r in conn.execute("SELECT symbol FROM stocks WHERE market = 'IN'")}
        found: dict[tuple, tuple] = {}
        errors = []
        for name, feed in (("nse event calendar", _nse_event_calendar), ("nse board meetings", _nse_board_meetings), ("bse forthcoming results", _bse_results)):
            try:
                for sym, d, purpose, src in feed():
                    if sym in known:
                        found.setdefault((sym, d), (purpose, src))  # first feed wins the purpose text
            except Exception as e:  # noqa: BLE001 - one feed down must not lose the others
                errors.append(f"{name}: {type(e).__name__}")
        if not found:
            raise RuntimeError("No results dates from any feed - " + "; ".join(errors))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        today = date.today().isoformat()
        by_symbol: dict[str, set] = {}
        for (sym, d), (purpose, src) in found.items():
            conn.execute(
                """INSERT INTO results_calendar (symbol, event_date, purpose, source, fetched_at) VALUES (?,?,?,?,?)
                   ON CONFLICT(symbol, event_date) DO UPDATE SET purpose = excluded.purpose, fetched_at = excluded.fetched_at""", (sym, d, purpose, src, now))
            by_symbol.setdefault(sym, set()).add(d)
        for sym, dates in by_symbol.items():  # a date that moved or was cancelled: drop the old, still-upcoming future row
            for (d,) in conn.execute("SELECT event_date FROM results_calendar WHERE symbol = ? AND status = 'upcoming' AND event_date >= ?", (sym, today)).fetchall():
                if d not in dates:
                    conn.execute("DELETE FROM results_calendar WHERE symbol = ? AND event_date = ?", (sym, d))
        conn.commit()
        return len(found)
