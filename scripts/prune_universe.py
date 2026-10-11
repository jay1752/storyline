"""Take stocks off the platform that cannot be analysed: the platform only keeps a stock it can show honestly.

Removed (market.db), each with its reason recorded in excluded_stocks:
  derivative   US warrants / units / rights / preferred lines (they copy their parent's figures)
  spac         blank-check shells with no operations
  no_price     no price in the last 8 days (India) / 14 days (US): delisted, suspended or stale
  stale        quarterly results stopped more than 400 days ago
  no_data      no quarterly results at all and not a fund/ETF (foreign filers Yahoo could not fill, dormant or non-filing companies)
Kept: anything a user tracks or has in a pending import; funds and ETFs (price-only pages); everything with analysable data.

Safety: a gzip backup of the stocks and their fundamentals is written first (prices are re-fetchable from Yahoo and are not backed up);
the stocks are added to excluded_stocks so universe syncs do not re-add them (delete the row there to bring one back).
    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/prune_universe.py [--apply]"""
import argparse
import collections
import datetime as dt
import gzip
import json
import re
import sqlite3
import sys

from app.db import DATA_DIR
from app.us_securities import is_derivative

IN_PRICE_DAYS, US_PRICE_DAYS, STALE_DAYS = 8, 14, 400
FUND = re.compile(r"MUTUAL FUND|MAMC|BEES|\bETF\b|\bETN\b|TRUST|FUND|PORTFOLIO|ISHARES|SPDR|VANGUARD|PROSHARES|INVESCO|WISDOMTREE|\bAMC\b")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    today = dt.date.today()
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=600)
    m.row_factory = sqlite3.Row
    u = sqlite3.connect(f"file:{DATA_DIR}/users.db?mode=ro", uri=True)
    held = {r[0] for r in u.execute("SELECT symbol FROM tracked_positions")}
    pend = " ".join(r[0] for r in u.execute("SELECT payload_json FROM pending_imports WHERE status='pending'"))
    stocks = {r["symbol"]: dict(r) for r in m.execute("SELECT symbol, name, sector, market, cap_tier, exchange FROM stocks")}
    last_px = {r[0]: r[1] for r in m.execute("SELECT symbol, MAX(date) FROM price_history GROUP BY symbol")}
    q = {r[0]: r[1] for r in m.execute("SELECT symbol, MAX(period_end_date) FROM quarterly_data WHERE quarter_label NOT LIKE '%annual%' GROUP BY symbol")}
    us_t = {s.split(":", 1)[1]: v["name"] for s, v in stocks.items() if v["market"] == "US"}

    def is_fund(s, v):
        n = (v["name"] or "").upper()
        return v["sector"] == "Exchange traded fund" or bool(FUND.search(n)) and q.get(s) is None

    # Commodity pools and crypto trusts file 10-Qs like companies but are funds: their "fundamentals" are not a business's. They stay as
    # price-only fund lines (trackable) and lose the SEC rows, so no screen or story treats them as a company.
    fund_filers = [s for s, v in stocks.items() if v["market"] == "US" and v["sector"] == "Commodity Contracts Brokers & Dealers"
                   and re.search(r"ETF|ETN|FUND|TRUST|BITCOIN|ETHER|CURRENCY|COMMODITY|INDEX|GOLD|SILVER|\bOIL\b|GASOLINE|PORTFOLIO|FUTURES|TREASURY|SOLANA|XRP|CRYPTO|COIN", (v["name"] or "").upper())
                   and q.get(s) is not None]
    print("fund filers (commodity pools / crypto trusts) to strip of fundamentals:", len(fund_filers), [x.split(":", 1)[1] for x in fund_filers[:12]])
    reason: dict[str, str] = {}
    for s, v in stocks.items():
        t = s.split(":", 1)[1]
        age = (today - dt.date.fromisoformat(last_px[s])).days if s in last_px else None
        fund = is_fund(s, v)
        if v["market"] == "US" and is_derivative(t, us_t):
            reason[s] = "derivative"
        elif v["sector"] and "BLANK CHECK" in v["sector"].upper():
            reason[s] = "spac"
        elif age is None or age > (IN_PRICE_DAYS if v["market"] == "IN" else US_PRICE_DAYS) and not (v["market"] == "US" and q.get(s) is None and age <= 30):
            reason[s] = "no_price"
        elif fund:
            continue
        elif q.get(s) is None:
            reason[s] = "no_data"
        elif (today - dt.date.fromisoformat(q[s])).days > STALE_DAYS:
            reason[s] = "stale"
    protected = {s for s in reason if s in held or s in pend}
    for s in protected:
        del reason[s]
    c = collections.Counter((stocks[s]["market"], r) for s, r in reason.items())
    print("to remove:", len(reason), dict(c), "| protected (tracked/pending import):", sorted(protected))
    for r in ("derivative", "spac", "no_price", "stale", "no_data"):
        print(" ", r, [x.split(":", 1)[1] for x, y in reason.items() if y == r][:14])
    if not a.apply:
        return
    for i in range(0, len(fund_filers), 500):
        chunk = fund_filers[i:i + 500]
        p = ",".join("?" * len(chunk))
        for t in ("quarterly_data", "us_annual", "stock_metrics"):
            m.execute(f"DELETE FROM {t} WHERE symbol IN ({p})", chunk)
        m.execute(f"UPDATE stocks SET sector = 'Exchange traded fund' WHERE symbol IN ({p})", chunk)
    m.commit()
    syms = sorted(reason)
    ph = ",".join("?" * 500)
    tables = [r[0] for r in m.execute("SELECT name FROM sqlite_master WHERE type='table'")
              if r[0] != "excluded_stocks" and "symbol" in {c[1] for c in m.execute(f"PRAGMA table_info({r[0]})")}]
    back = {}
    for t in ("stocks", "quarterly_data", "annual_data", "us_annual", "india_mcap", "institutional_holdings"):
        if t not in tables:
            continue
        rows, cols = [], None
        for i in range(0, len(syms), 500):
            chunk = syms[i:i + 500]
            cur = m.execute(f"SELECT * FROM {t} WHERE symbol IN ({','.join('?' * len(chunk))})", chunk)
            cols = [d[0] for d in cur.description]
            rows += [list(x) for x in cur.fetchall()]
        back[t] = {"columns": cols, "rows": rows}
    path = f"{DATA_DIR}/pruned_backup-{today.isoformat()}.json.gz"
    with gzip.open(path, "wt") as f:
        json.dump({"pruned_on": today.isoformat(), "reasons": reason, "data": back}, f)
    print("backup:", path, {t: len(v["rows"]) for t, v in back.items()})
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for i in range(0, len(syms), 500):
        chunk = syms[i:i + 500]
        p = ",".join("?" * len(chunk))
        m.execute(f"INSERT OR IGNORE INTO excluded_stocks (symbol, exchange, name, sector, market, cap_tier, excluded_at) "
                  f"SELECT symbol, exchange, name, sector, market, cap_tier, ? FROM stocks WHERE symbol IN ({p})", [now] + chunk)
        for t in tables:
            if t != "stocks":
                m.execute(f"DELETE FROM {t} WHERE symbol IN ({p})", chunk)
        m.execute(f"DELETE FROM stocks WHERE symbol IN ({p})", chunk)
        m.commit()
    p2 = f"/app/app/data/cap_tiers.json"
    try:
        tiers = json.load(open(p2))
        for s in syms:
            tiers.pop(s, None)
        json.dump(tiers, open(p2, "w"), indent=0)
    except FileNotFoundError:
        pass
    print("removed", len(syms), "stocks from", len(tables), "tables")


if __name__ == "__main__":
    sys.exit(main())
