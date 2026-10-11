"""Yahoo Finance (via the `yfinance` library) — daily OHLC price history, both
markets. Found this session specifically to attack the two biggest remaining gaps:
Tiingo's real ~50 requests/hour free-tier cap left US price coverage at 7.2%
(9,619 of 10,363 symbols untouched), and 5paisa's scrip-master name-matching
ceiling leaves India capped around 47% of the universe regardless of how many
times it's retriggered — neither is a quota that more triggers can out-wait.

No API key, no signup — `yfinance` scrapes Yahoo's own internal chart API. Live-
tested before building this: 30 rapid US requests in 8.5s, zero errors; a random
sample of India small-cap-tier symbols 5paisa can't resolve at all (recent IPOs
like SMARTWORKS, SHADOWFAX, COHANCE) came back 10-for-10. AAPL's full history goes
back to its 1980 IPO — deeper than Tiingo's own coverage. Coverage isn't total —
a sample of genuinely obscure/illiquid India micro-caps mostly came back empty —
but it's real coverage neither existing source can reach, not a replacement for
either: this is additive, keeps its own `source` tag, and never overwrites a row
Tiingo/5paisa already wrote.

No official rate limit is published (unlike Tiingo's documented hourly cap), so
RATE_LIMIT_SECONDS is a conservative guess, and MAX_RUN_SECONDS is a self-
terminating safety net — same lesson as moneycontrol_source.py's own hang
incidents this session, never trust an unfamiliar library to fail cleanly on its
own. A symbol yfinance can't resolve raises its own internal exceptions (not a
clean 404), so each symbol is wrapped individually and skipped on any failure —
one bad ticker must never take down a multi-thousand-symbol run."""

import time

from app.ingestion.base import DataSource, apply_symbol_filter

RATE_LIMIT_SECONDS = 0.3
MAX_RUN_SECONDS = 2700  # 45 min — a run that's still going gets picked up again by the next trigger via resume


class YFinanceSource(DataSource):
    name = "yfinance"

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("yfinance is used here for prices only — fundamentals come from SEC EDGAR/BSE/MoneyControl.")

    def sync_universe(self, conn, limit=None, symbol=None, progress=None) -> int:
        """Not a real symbol-list sync (that's SEC EDGAR/5paisa's job) — reuses this
        target to fill `stocks.sector` from yfinance's own `.info` for symbols SEC
        EDGAR can never classify. Real gap found live: ~1,340 US symbols stuck
        "Unclassified" turned out to be almost entirely foreign ADRs (Contemporary
        Amperex/CATL, BAE Systems, London Stock Exchange Group, etc.) whose SEC
        submissions record has `entityType: "other"` and a genuinely EMPTY SIC
        field — not a parsing gap, SEC itself has no industry classification for
        this filer category. Confirmed live that yfinance's `.info` carries real
        sector/industry for these (e.g. CYATY -> "Industrials") since Yahoo sources
        it independently of SEC's own filing metadata. Same
        never-clobber-real-data rule as every other sector writer here (see
        sec_edgar_source.py's own comment on this): only ever touches a symbol
        still sitting at NULL/'Unclassified'."""
        import yfinance as yf

        symbols = [r["symbol"] for r in conn.execute(
            "SELECT symbol FROM stocks WHERE (sector IS NULL OR sector = 'Unclassified')"
        ).fetchall()]
        symbols = apply_symbol_filter(symbols, limit, symbol)

        rows_written = 0
        run_deadline = time.monotonic() + MAX_RUN_SECONDS
        for sym in symbols:
            if time.monotonic() > run_deadline:
                break
            time.sleep(RATE_LIMIT_SECONDS)
            ticker = self._yf_ticker(sym)
            try:
                sector = yf.Ticker(ticker).info.get("sector")
            except Exception:  # noqa: BLE001 — same reasoning as ingest_prices: an unresolvable ticker must never kill the run
                continue
            if not sector:
                continue
            conn.execute(
                "UPDATE stocks SET sector = ? WHERE symbol = ? AND (sector IS NULL OR sector = 'Unclassified')",
                (sector, sym),
            )
            rows_written += 1
            conn.commit()
            if progress:
                progress(rows_written)

        return rows_written

    def _yf_tickers(self, sym: str) -> list[str]:
        """NSE:RELIANCE -> ["RELIANCE.NS", "RELIANCE.BO"] (Yahoo's India suffixes —
        .NS for NSE, .BO for BSE); NASDAQ:AAPL -> ["AAPL"] (US tickers are bare).

        Real gap found live: a symbol carrying our "NSE:" prefix convention (applied
        uniformly to every India stock in this DB, not a guarantee of an actual NSE
        listing) can be a company that's genuinely only listed on BSE, never NSE —
        .NS alone then always comes back empty no matter how deep yfinance's data
        goes, which looked identical to "yfinance doesn't have this company" but
        wasn't. Confirmed live on 6 symbols already marked unresolvable by the .NS-
        only lookup (ANDHRAPET, UTIQUE, ARUNAHTEL, ATVPR, BHAGGAS, BIHSPONG): every
        one of them has real, deep (4,600-6,900 rows) history under .BO. .NS is
        tried first since it's the common case; .BO is a fallback only reached when
        .NS comes back empty, not a double fetch for the (very common) symbols .NS
        already resolves."""
        if not sym.startswith("NSE:"):
            return [sym.split(":", 1)[1]]
        ticker = sym.split(":", 1)[1]
        return [f"{ticker}.NS", f"{ticker}.BO"]

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None, market=None) -> int:
        import yfinance as yf  # local import — same reasoning as fivepaisa_source.py's py5paisa: skip the dependency for sources that never touch it

        # market=None keeps the original combined US+IN behavior; passing 'US' or 'IN'
        # scopes a run to one market — lets a dedicated India run be triggered
        # alongside an already-running US-heavy job instead of waiting behind it in
        # the same symbol list.
        market_filter = "market IN ('US', 'IN')" if not market else "market = ?"
        params = () if not market else (market,)
        symbols = [r["symbol"] for r in conn.execute(
            f"SELECT symbol FROM stocks WHERE {market_filter}", params
        ).fetchall()]
        symbols = apply_symbol_filter(symbols, limit, symbol)

        if not symbol:
            # Resume, don't restart — same principle as every other price source
            # here. yfinance has no documented quota to wait out, but a 10,000+
            # symbol universe still takes real wall-clock time, so this still
            # matters for picking up where the last run left off.
            already_covered = {r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM price_history WHERE source = 'yfinance'"
            ).fetchall()}
            symbols = [s for s in symbols if s not in already_covered]

        rows_written = 0
        run_deadline = time.monotonic() + MAX_RUN_SECONDS

        for sym in symbols:
            if time.monotonic() > run_deadline:
                break
            df = None
            for ticker in self._yf_tickers(sym):
                time.sleep(RATE_LIMIT_SECONDS)
                try:
                    candidate = yf.Ticker(ticker).history(period="max", auto_adjust=False)
                except Exception:  # noqa: BLE001 — yfinance's own failure modes for a bad/delisted ticker aren't a clean, catchable type
                    continue
                if candidate is not None and not candidate.empty:
                    df = candidate
                    break
            if df is None:
                continue
            for idx, row in df.iterrows():
                conn.execute(
                    "INSERT OR REPLACE INTO price_history (symbol, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,'yfinance')",
                    (sym, idx.date().isoformat(), row.get("Open"), row.get("High"), row.get("Low"), row.get("Close"), row.get("Volume")),
                )
                rows_written += 1
            conn.execute("DELETE FROM price_history WHERE symbol = ? AND source = 'mock'", (sym,))
            conn.commit()
            if progress:
                progress(rows_written)

        return rows_written
