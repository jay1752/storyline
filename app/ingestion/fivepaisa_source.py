"""Real 5paisa ingestion — India price/OHLC, via 5paisa's own official Vendor API
(Openapi.5paisa.com), not INDstocks' consumer API this file used previously (that
API's own instrument-list endpoint stopped returning data once its access token
expired, and INDstocks itself turned out to be 5paisa's own retail/API brand —
see STATUS.md).

Auth is SEBI-mandated 2FA: CLIENT_ID + a live TOTP code + MPIN -> RequestToken ->
AccessToken, via the official `py5paisa` SDK. Verified end-to-end against a real
account this session — login and real OHLC data for RELIANCE/INFY both confirmed
before this was trusted. A hand-rolled version of the same two HTTP calls
(TOTPLogin, GetAccessToken) failed with "Invalid Input Parameters" for reasons
that stayed unclear even after checking the SDK source and public issue trackers —
the official SDK works, so this uses it rather than re-deriving the exact payload
shape that made the difference.

Credentials needed: FIVEPAISA_CLIENT_ID, FIVEPAISA_MPIN, FIVEPAISA_TOTP_SECRET,
FIVEPAISA_USER_KEY, FIVEPAISA_USER_ID, FIVEPAISA_ENCRYPTION_KEY — from 5paisa's
API dashboard (xstream.5paisa.com/dashboard). PASSWORD/APP_NAME/APP_SOURCE are
NOT needed — confirmed by reading the SDK source directly: PASSWORD is never
referenced after init, and APP_NAME/APP_SOURCE only matter for a different login
method and for order-placement payloads, neither of which this module touches.

Scope, deliberately: this module never calls an order/trading endpoint — only
get_totp_session (login) and historical_data (read-only OHLC). No code path here
can place a trade, and it should stay that way.

Symbol mapping: 5paisa's live ScripMaster API (`/ScripMaster/segment/nse_eq`)
returned a server-side "cache not available" error when tested (unrelated to
auth — a real account, freshly logged in, still got it). Falls back to their
static CSV mirror (images.5paisa.com/website/scripmaster-csv-format.csv), same
data, confirmed identical scrip codes to what historical_data itself expects
(RELIANCE=2885, INFY=1594, matched directly).

TOTP codes are single-use (server-enforced replay protection, hit directly while
testing) — login happens once per run, never once per symbol, and the resulting
access token is reused for every historical_data call in that run.
"""

import csv
import io
import os
import time
from datetime import date

import pyotp
import requests

from app.ingestion.base import DataSource, NotConfiguredError, apply_symbol_filter

SCRIP_MASTER_URL = "https://images.5paisa.com/website/scripmaster-csv-format.csv"
RATE_LIMIT_SECONDS = 0.3  # conservative default — no documented per-second limit found for historical_data

REQUIRED_ENV = [
    "FIVEPAISA_CLIENT_ID", "FIVEPAISA_MPIN", "FIVEPAISA_TOTP_SECRET",
    "FIVEPAISA_USER_KEY", "FIVEPAISA_USER_ID", "FIVEPAISA_ENCRYPTION_KEY",
]


class FivePaisaSource(DataSource):
    name = "5paisa"

    def _client(self):
        """Logs in fresh — TOTP is single-use, so this can't be cached across runs —
        and returns an authenticated py5paisa client. Import is local: py5paisa pulls
        in a dependency tree we don't want loaded for sources that never use it."""
        from py5paisa import FivePaisaClient

        missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
        if missing:
            raise NotConfiguredError(f"Missing: {', '.join(missing)} — get these from xstream.5paisa.com/dashboard.")

        cred = {
            "APP_NAME": "", "APP_SOURCE": "", "PASSWORD": "",  # unused by TOTP login/historical data — confirmed against SDK source
            "USER_ID": os.environ["FIVEPAISA_USER_ID"],
            "USER_KEY": os.environ["FIVEPAISA_USER_KEY"],
            "ENCRYPTION_KEY": os.environ["FIVEPAISA_ENCRYPTION_KEY"],
        }
        client = FivePaisaClient(cred=cred)
        totp = pyotp.TOTP(os.environ["FIVEPAISA_TOTP_SECRET"]).now()
        client.get_totp_session(os.environ["FIVEPAISA_CLIENT_ID"], totp, os.environ["FIVEPAISA_MPIN"])
        if not client.access_token:
            raise NotConfiguredError("5paisa login failed — check credentials in .env are current.")
        return client

    def _scrip_master_rows(self) -> list[dict]:
        resp = requests.get(SCRIP_MASTER_URL, timeout=60)
        resp.raise_for_status()
        return list(csv.DictReader(io.StringIO(resp.text)))

    def _nse_equity_rows(self) -> list[dict]:
        return [r for r in self._scrip_master_rows() if r.get("Exch") == "N" and r.get("ExchType") == "C" and r.get("Series") == "EQ"]

    def _bse_equity_rows(self) -> list[dict]:
        """BSE-segment equities — confirmed live this session that 5paisa's
        historical_data API genuinely serves this exchange (MCX, BSE-only/no NSE
        listing, returned real OHLC when queried with exch='B'). Used only as a
        fallback for symbols with no NSE listing — sync_universe() still treats NSE
        as primary (matches bse_source.py's NSE:{ticker} symbol-space convention,
        and NSE is generally the more liquid/canonical quote for dual-listed stocks)."""
        return [r for r in self._scrip_master_rows() if r.get("Exch") == "B" and r.get("ExchType") == "C" and r.get("Series") == "EQ"]

    def sync_universe(self, conn) -> int:
        rows = 0
        for row in self._nse_equity_rows():
            symbol = f"NSE:{row['Name']}"  # matches the symbol convention bse_source.py already uses for IN stocks
            name = row.get("FullName") or row["Name"]
            conn.execute(
                """INSERT INTO stocks (symbol, exchange, name, sector, market) VALUES (?, 'NSE', ?, 'Unclassified', 'IN')
                ON CONFLICT(symbol) DO UPDATE SET exchange=excluded.exchange, name=excluded.name, market=excluded.market""",
                # sector excluded from the update clause on purpose — this source shares the NSE:{ticker} symbol
                # space with bse_source.py, and re-running this sync used to silently wipe out its real sector
                # enrichment back to 'Unclassified' (confirmed happening live before this fix).
                (symbol, name),
            )
            rows += 1
        conn.commit()
        return rows

    def ingest_quarterly(self, conn, limit=None, symbol=None, progress=None) -> int:
        raise NotImplementedError("5paisa doesn't provide fundamentals — quarterly data comes from BSE (bse_source.py).")

    def ingest_prices(self, conn, limit=None, symbol=None, progress=None) -> int:
        client = self._client()
        all_rows = self._scrip_master_rows()  # one fetch, reused for both segments — avoids re-pulling the 26MB CSV twice
        nse_rows = [r for r in all_rows if r.get("Exch") == "N" and r.get("ExchType") == "C" and r.get("Series") == "EQ"]
        bse_rows = [r for r in all_rows if r.get("Exch") == "B" and r.get("ExchType") == "C" and r.get("Series") == "EQ"]

        # NSE preferred (more liquid/canonical for dual-listed stocks); BSE only as a
        # fallback for symbols with no NSE listing at all — confirmed live this session
        # that 5paisa's historical_data API genuinely serves the BSE segment too (MCX,
        # BSE-only, returned 177 real OHLC rows when queried with exch='B').
        exch_map = {f"NSE:{r['Name']}": ("B", int(r["Scripcode"])) for r in bse_rows}
        exch_map.update({f"NSE:{r['Name']}": ("N", int(r["Scripcode"])) for r in nse_rows})

        symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM stocks WHERE market = 'IN'").fetchall()]
        symbols = apply_symbol_filter(symbols, limit, symbol)

        if not symbol:
            # Resume, don't restart: this source had NO skip-already-covered logic —
            # unlike every other source here (tiingo/bse/moneycontrol/finnhub, all
            # fixed earlier this session) — so every re-trigger re-fetched each
            # symbol's FULL price history (2000-01-01 to today) from scratch and
            # INSERT OR REPLACEd it, even though the data hadn't changed. Confirmed
            # live: distinct symbol coverage sat at exactly 2413 across many
            # retriggers over several hours while rows_written climbed past 7
            # million on a single run — pure wasted work (and the likely cause of a
            # real "database is locked" failure earlier, from the sheer write
            # volume colliding with other concurrent jobs). An explicit `symbol`
            # re-ingestion still always runs regardless, same as every other source.
            already_covered = {r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM price_history WHERE source = '5paisa'"
            ).fetchall()}
            symbols = [s for s in symbols if s not in already_covered]

        end = date.today()
        start = date(2000, 1, 1)  # historical_data clamps to whatever's actually available per scrip
        rows_written = 0

        for sym in symbols:
            exch_scrip = exch_map.get(sym)
            if not exch_scrip:
                continue
            exch, scrip_code = exch_scrip
            time.sleep(RATE_LIMIT_SECONDS)
            try:
                df = client.historical_data(exch, "C", scrip_code, "1d", start.isoformat(), end.isoformat())
            except Exception:  # noqa: BLE001 — one symbol's quirk must never kill a multi-thousand-symbol run
                continue
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                d = str(row["Datetime"])[:10]
                conn.execute(
                    "INSERT OR REPLACE INTO price_history (symbol, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,'5paisa')",
                    (sym, d, row.get("Open"), row.get("High"), row.get("Low"), row.get("Close"), row.get("Volume")),
                )
                rows_written += 1
            conn.execute("DELETE FROM price_history WHERE symbol = ? AND source = 'mock'", (sym,))
            conn.commit()
            if progress:
                progress(rows_written)

        return rows_written
