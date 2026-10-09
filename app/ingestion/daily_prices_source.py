"""Daily end-of-day price top-up, run by the in-app scheduler after each market closes.

India  — NSE's bhavcopy: ONE file per trading day covers every listed stock, so the whole market costs a handful of
         requests. It also carries NSE's own previous-close, which tells us when a split/bonus has re-based a stock.
US     — Yahoo Finance in batches of 100 tickers (no per-symbol quota), raw (split-adjusted, not dividend-adjusted) close.

Rules that keep the stored history trustworthy:
  * Only NEW dates are written (INSERT OR IGNORE for US; bhavcopy days replace only themselves). An existing price is
    never rewritten by a top-up.
  * If a new close differs from the last stored close by more than 40% (split, bonus, consolidation), the symbol's whole
    history is re-pulled from Yahoo so old and new prices sit on one basis, instead of a fake -90% cliff in the chart.
  * Anything a user tracks is also pushed through ensure_prices (covers stocks outside the bulk sources)."""

import csv
import io
import logging
import time
from datetime import date, timedelta

import requests

from app.ingestion.base import DataSource
from app.ingestion.nse_bhavcopy_source import EQUITY_SERIES, _fetch_day, _num
from app.price_fetch import ensure_prices, refresh_history

logger = logging.getLogger("app.daily_prices")

BSE_URL = "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{ymd}_F_0000.CSV"
BSE_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
               "Referer": "https://www.bseindia.com/"}
RATE_LIMIT_SECONDS = 0.3
MAX_RUN_SECONDS = 2400      # India: one file per day, takes seconds
US_MAX_RUN_SECONDS = 6000   # US: ~6.7k tickers in batches; a small CPU needs well over an hour
MAX_DAYS_BACK = 14        # catch-up window after an outage
JUMP = (0.6, 1.0 / 0.6)   # new/last close outside this band => corporate action
MAX_REFRESH = 80          # full-history refreshes per run
BATCH = 100


def _fetch_bse_day(d: date) -> list[dict] | None:
    """BSE's own daily bhavcopy (all BSE-listed stocks, ~5,000 rows). None on a holiday / not yet published."""
    try:
        r = requests.get(BSE_URL.format(ymd=d.strftime("%Y%m%d")), headers=BSE_HEADERS, timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200 or len(r.content) < 2000:
        return None
    return list(csv.DictReader(io.StringIO(r.content.decode("utf-8", "ignore"))))


class DailyPriceSource(DataSource):
    def __init__(self, market: str):
        self.market = market
        self.name = f"daily_prices_{market.lower()}"

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("daily_prices is prices-only.")

    # ------------------------------------------------------------------ shared
    def _last_close(self, conn, symbol: str, before: str | None = None):
        q = "SELECT date, close FROM price_history WHERE symbol = ?" + (" AND date < ?" if before else "") + " ORDER BY date DESC LIMIT 1"
        return conn.execute(q, (symbol, before) if before else (symbol,)).fetchone()

    def _refresh(self, conn, jumped: list[str], deadline: float) -> int:
        n = 0
        for sym in jumped[:MAX_REFRESH]:
            if time.monotonic() > deadline:
                break
            time.sleep(RATE_LIMIT_SECONDS)
            n += 1 if refresh_history(conn, sym) else 0
        return n

    def _tracked(self, conn) -> list[str]:
        try:
            rows = conn.execute("SELECT DISTINCT symbol FROM tracked_positions").fetchall() if _has_table(conn, "tracked_positions") else []
        except Exception:  # noqa: BLE001 — tracked_positions lives in users.db; absent on this connection is fine
            rows = []
        return [r[0] for r in rows]

    # ------------------------------------------------------------------ India
    def _india(self, conn, progress) -> int:
        deadline = time.monotonic() + MAX_RUN_SECONDS
        known = {r[0] for r in conn.execute("SELECT symbol FROM stocks WHERE market = 'IN'")}
        # Always re-walk the last two weeks: one small file per day, idempotent (a day just replaces itself), and it
        # heals any gap or half-filled day without needing to know what is "latest" (a single on-demand fetch can make
        # one stock look current while the rest of the market is days behind).
        today = date.today()
        start = today - timedelta(days=MAX_DAYS_BACK)
        days = [start + timedelta(days=i) for i in range((today - start).days + 1) if (start + timedelta(days=i)).weekday() < 5]
        # Our universe is every BSE/NSE-listed stock, but NSE's file only covers NSE-listed ones (~2,700 of ~5,200). BSE's file
        # covers the BSE-only rest, matched by ISIN first, then by ticker. NSE wins where a stock trades on both.
        by_isin = {r[0].upper(): r[1] for r in conn.execute("SELECT isin, symbol FROM stocks WHERE market = 'IN' AND isin IS NOT NULL")}
        written, jumped = 0, []
        checked: set[str] = set()

        def put(full, ts, o, h, l, c, v, pc, source):
            nonlocal written
            close = _num(c)
            if not close:
                return False
            if full not in checked:
                checked.add(full)
                prev = self._last_close(conn, full, before=ts)
                pcn = _num(pc)
                # the exchange's previous close is already corporate-action adjusted; if our stored close for that day
                # disagrees by >40% the stock was re-based since we last stored it.
                if prev and pcn and not (JUMP[0] <= prev["close"] / pcn <= JUMP[1]) and full not in jumped:
                    jumped.append(full)
            conn.execute("INSERT OR REPLACE INTO price_history (symbol, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
                         (full, ts, _num(o), _num(h), _num(l), close, _num(v), source))
            written += 1
            return True

        for d in days:
            if time.monotonic() > deadline:
                break
            time.sleep(RATE_LIMIT_SECONDS)
            covered: set[str] = set()
            try:
                res = _fetch_day(d)
            except Exception as e:  # noqa: BLE001
                logger.warning("NSE bhavcopy %s failed: %s", d, e)
                res = None
            if res is not None:
                schema, rows = res
                for row in rows:
                    if schema == "udiff":
                        sym, series, ts = row.get("TckrSymb"), row.get("SctySrs"), row.get("TradDt")
                        o, h, l, c, v, pc = (row.get(k) for k in ("OpnPric", "HghPric", "LwPric", "ClsPric", "TtlTradgVol", "PrvsClsgPric"))
                    else:
                        sym, series, ts = row.get("SYMBOL"), row.get("SERIES"), d.isoformat()
                        o, h, l, c, v, pc = (row.get(k) for k in ("OPEN", "HIGH", "LOW", "CLOSE", "TOTTRDQTY", "PREVCLOSE"))
                    full = f"NSE:{sym}"
                    if series in EQUITY_SERIES and full in known and put(full, ts, o, h, l, c, v, pc, "nse_bhavcopy"):
                        covered.add(full)
            time.sleep(RATE_LIMIT_SECONDS)
            bse = _fetch_bse_day(d)
            for row in bse or []:
                full = by_isin.get((row.get("ISIN") or "").upper()) or f"NSE:{row.get('TckrSymb')}"
                if full in known and full not in covered:
                    if put(full, row.get("TradDt") or d.isoformat(), row.get("OpnPric"), row.get("HghPric"), row.get("LwPric"),
                           row.get("ClsPric"), row.get("TtlTradgVol"), row.get("PrvsClsgPric"), "bse_bhavcopy"):
                        covered.add(full)
            conn.commit()
            if progress:
                progress(written)
        written += self._refresh(conn, jumped, deadline)
        return written

    # ------------------------------------------------------------------ US
    def _us(self, conn, progress, limit=None) -> int:
        import yfinance as yf

        deadline = time.monotonic() + US_MAX_RUN_SECONDS
        symbols = [r[0] for r in conn.execute(
            "SELECT symbol FROM stocks s WHERE market = 'US' AND EXISTS (SELECT 1 FROM quarterly_data q WHERE q.symbol = s.symbol)")]
        # Stalest first: if a run is cut short (slow host, provider hiccup) the next run starts with whatever it missed,
        # so every stock gets refreshed within a couple of days instead of the same alphabetical slice each time.
        newest = {s: (conn.execute("SELECT MAX(date) FROM price_history WHERE symbol = ?", (s,)).fetchone()[0] or "") for s in symbols}
        symbols.sort(key=lambda s: newest[s])
        if limit:
            symbols = symbols[:limit]
        written, jumped = 0, []
        for i in range(0, len(symbols), BATCH):
            if time.monotonic() > deadline:
                break
            chunk = symbols[i:i + BATCH]
            tickers = [s.split(":", 1)[1] for s in chunk]
            try:
                df = yf.download(tickers, period="14d", interval="1d", group_by="ticker", auto_adjust=False,
                                 progress=False, threads=True, timeout=30)
            except Exception as e:  # noqa: BLE001
                logger.warning("US batch %s failed: %s", i, e)
                continue
            if df is None or df.empty:
                continue
            for sym, tk in zip(chunk, tickers):
                try:
                    sub = df[tk] if len(tickers) > 1 else df
                except KeyError:
                    continue
                sub = sub.dropna(subset=["Close"])
                if sub.empty:
                    continue
                last = self._last_close(conn, sym)
                first_new = None
                for idx, r in sub.iterrows():
                    ds = idx.date().isoformat()
                    if last and ds <= last["date"]:
                        continue  # never rewrite what we already have
                    conn.execute("INSERT OR IGNORE INTO price_history (symbol, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,'yfinance')",
                                 (sym, ds, _num(r.get("Open")), _num(r.get("High")), _num(r.get("Low")), _num(r.get("Close")), _num(r.get("Volume"))))
                    written += 1
                    first_new = first_new if first_new is not None else float(r["Close"])
                if last and first_new and last["close"] and not (JUMP[0] <= first_new / last["close"] <= JUMP[1]):
                    jumped.append(sym)
            conn.commit()
            if progress:
                progress(written)
        written += self._refresh(conn, jumped, deadline)
        return written

    # ------------------------------------------------------------------ entry
    def ingest_prices(self, conn, limit=None, symbol=None, progress=None, market=None) -> int:
        # Stocks a user tracks come FIRST and are never left stale, whatever the bulk source covers or how long it takes.
        from app.db import get_users_conn
        with get_users_conn() as u:
            tracked = [r[0] for r in u.execute("SELECT DISTINCT symbol FROM tracked_positions WHERE status = 'active'")
                       if r[0].startswith("NSE:") == (self.market == "IN")]
        written, deadline = 0, time.monotonic() + 120
        for sym in tracked:
            written += ensure_prices(sym, deadline).get("fetched", 0)
        return written + (self._india(conn, progress) if self.market == "IN" else self._us(conn, progress, limit))


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None
