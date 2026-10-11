"""Add the India ETFs we hold in funds.db but not in the stocks universe, so they can be searched, tracked and matched from broker
statements, and seed each one's price history from funds.db (Yahoo .NS history). Existing stocks rows are never touched.

    docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/add_india_etfs.py [--apply]
Without --apply it only counts."""
import re
import sqlite3
import sys

from app.db import DATA_DIR

AMC = {"GROWWAMC": "Groww", "MOTILALAMC": "Motilal Oswal", "MIRAEAMC": "Mirae Asset", "DSPAMC": "DSP", "UTIAMC": "UTI", "AXISAMC": "Axis",
       "KOTAKMAMC": "Kotak", "EDELAMC": "Edelweiss", "AONEAMC": "Angel One", "SBIAMC": "SBI", "ICICIPRAMC": "ICICI Prudential",
       "ZERODHAAMC": "Zerodha", "360ONEAMC": "360 ONE", "NIPPONAMC": "Nippon India", "BANDHANAMC": "Bandhan", "BIRLASLAMC": "Aditya Birla Sun Life",
       "RELIGARAMC": "Invesco", "UNIONAMC": "Union", "CHOICEAMC": "Choice", "HSBCAMC": "HSBC", "BFAM": "Bajaj Finserv", "HDFCAMC": "HDFC",
       "JIOBLACKROCKAMC": "Jio BlackRock", "QUANT_AMC": "Quant", "WEALTH": "Wealth Company"}


def display_name(ticker: str, raw: str, index: str | None, category: str | None) -> str:
    pre = re.split(r"\s*-\s*", raw, 1)[0].replace(" ", "") if "-" in raw else ""
    amc = AMC.get(pre.upper(), AMC.get(pre, pre.replace("AMC", "").title()))
    what = (index or category or "").strip()
    what = {"GOLD": "Gold", "SILVER": "Silver"}.get(what.upper(), what)
    base = " ".join(x for x in (amc, what) if x) or raw
    return f"{base} ETF ({ticker})"


def main() -> None:
    apply = "--apply" in sys.argv
    f = sqlite3.connect(f"{DATA_DIR}/funds.db")
    f.row_factory = sqlite3.Row
    m = sqlite3.connect(f"{DATA_DIR}/market.db", timeout=120)
    todo = [r for r in f.execute("SELECT ticker, name, isin, category, underlying_index FROM funds WHERE market='IN' AND kind='ETF'")
            if not m.execute("SELECT 1 FROM stocks WHERE symbol = ?", ("NSE:" + r["ticker"],)).fetchone()]
    print(f"{len(todo)} India ETFs are in funds.db but not in stocks")
    added = rows = 0
    for r in todo:
        sym = "NSE:" + r["ticker"]
        name = display_name(r["ticker"], r["name"], r["underlying_index"], r["category"])
        if not apply:
            if added < 6:
                print("  e.g.", sym, "->", name)
            added += 1
            continue
        m.execute("INSERT OR IGNORE INTO stocks (symbol, exchange, name, sector, market, isin) VALUES (?, 'NSE', ?, 'Exchange traded fund', 'IN', ?)", (sym, name, r["isin"]))
        for p in f.execute("SELECT date, close FROM fund_prices WHERE symbol = ?", ("FUND:NSE:" + r["ticker"],)):
            m.execute("INSERT OR IGNORE INTO price_history (symbol, date, close, source) VALUES (?, ?, ?, 'yahoo_etf')", (sym, p["date"], p["close"]))
            rows += 1
        added += 1
        if added % 25 == 0:
            m.commit()
    if apply:
        m.commit()
    print(("ADDED" if apply else "DRY RUN"), added, "ETFs" + (f", {rows} price rows" if apply else ""))


if __name__ == "__main__":
    sys.exit(main())
