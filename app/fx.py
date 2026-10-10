"""The platform's USD -> INR rate. One place that answers "how many rupees is a dollar right now", used wherever a US holding is
shown in rupees (Portfolio's combined view, position weight in the daily analysis). The rate is written hourly by the fx_usd_inr job
(app/ingestion/fx_source.py); if nothing has ever been stored, FALLBACK is used and the answer is marked stale."""
import time

from app.db import get_market_conn

FALLBACK = 90.0
_cache: dict = {"at": 0.0, "val": None}


def usd_inr() -> dict:
    """{"rate": float, "as_of": str | None, "source": str | None, "fetched_at": str | None, "stale": bool}. Cached for a minute."""
    if _cache["val"] and time.time() - _cache["at"] < 60:
        return _cache["val"]
    row = None
    try:
        with get_market_conn() as m:
            row = m.execute("SELECT rate, as_of, source, fetched_at FROM fx_rates WHERE pair = 'USDINR' ORDER BY fetched_at DESC LIMIT 1").fetchone()
    except Exception:  # noqa: BLE001 - table not created yet
        row = None
    val = ({"rate": float(row["rate"]), "as_of": row["as_of"], "source": row["source"], "fetched_at": row["fetched_at"], "stale": False}
           if row else {"rate": FALLBACK, "as_of": None, "source": None, "fetched_at": None, "stale": True})
    _cache.update(at=time.time(), val=val)
    return val


def rate() -> float:
    return usd_inr()["rate"]


def reset_cache() -> None:
    _cache["at"] = 0.0
