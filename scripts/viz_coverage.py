"""How many stocks get each Snapshot panel. Runs the real panel builders (app/viz.py) over the India stocks above the screening floor and a US sample.
    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/viz_coverage.py"""
import collections
import sqlite3

from app import viz
from app.db import get_market_conn
from app.routes.analytics import _lab

PANELS = ("rel", "growth", "cash", "own", "holders", "capital", "cycle", "value", "analysts", "peers", "deals")


def main() -> None:
    m = sqlite3.connect("/app/data/market.db")
    m.row_factory = sqlite3.Row
    inn = m.execute("SELECT s.symbol, s.sector, sm.bank FROM stock_metrics sm JOIN stocks s USING(symbol) WHERE sm.market='IN' AND sm.symbol LIKE 'NSE:%' AND sm.mcap >= 300 ORDER BY sm.mcap DESC").fetchall()
    us = m.execute("SELECT s.symbol, s.sector, sm.bank FROM stock_metrics sm JOIN stocks s USING(symbol) WHERE sm.market='US' AND sm.mcap IS NOT NULL ORDER BY sm.mcap DESC LIMIT 600").fetchall()
    for name, rows, mk in (("India above the Rs300cr floor", inn, "IN"), ("US, 600 largest", us, "US")):
        cnt = collections.Counter()
        with get_market_conn() as c:
            for r in rows:
                sym, bank = r["symbol"], bool(r["bank"])
                px = c.execute("SELECT close FROM price_history WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,)).fetchone()
                last = px[0] if px else None
                got = {"rel": viz._relative(c, sym, mk, r["sector"]), "growth": viz._growth(c, sym, _lab, "x"), "value": viz._value(c, sym, last)}
                got["cash"] = None if bank else viz._cash(c, sym, mk, "x")
                if mk == "IN":
                    own = viz._ownership(c, sym, _lab)
                    got["own"] = own and own["area"]
                    got["holders"] = own and own["holders"]
                    got["capital"] = None if bank else viz._capital(c, sym)
                    got["cycle"] = None if bank else viz._cycle(c, sym)
                    got["peers"] = viz._peers(c, sym, last)
                    got["deals"] = viz._deals(c, sym)
                    v = got["value"]
                    got["analysts"] = v and v["analysts"]
                else:
                    v = got["value"]
                    got["analysts"] = v and v["analysts"]
                for k in PANELS:
                    if got.get(k):
                        cnt[k] += 1
        n = len(rows)
        print(f"\n{name}: {n} stocks")
        for k in PANELS:
            if mk == "US" and k in ("own", "holders", "capital", "cycle", "peers", "deals"):
                continue
            print(f"  {k:9} {cnt[k]:5}  {100 * cnt[k] / n:3.0f}%")


if __name__ == "__main__":
    main()
