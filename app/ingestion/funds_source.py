"""ETF and index-fund ingestion into data/funds.db. Every source here is free and keyless.

  US ETFs          Nasdaq's ETF screener (list + last price)  ->  Yahoo (history, fee, AUM, category, top-10 holdings, sectors)
  India ETFs       NSE's ETF securities list (name, ISIN, underlying) + NSE's live ETF table (price, 52w)  ->  Yahoo .NS history
  India index MFs  AMFI's daily NAV file (index-fund category, Direct plan, Growth option)  ->  mfapi.in full NAV history
  India holdings   NSE's published constituent list of the tracked index (unweighted; NSE's CSV carries no weights)

Idempotent: every phase upserts, so it can be re-run or resumed."""

import csv
import io
import logging
import re
import time
from datetime import datetime, timezone

import requests

from app.db import get_market_conn
from app.funds_db import get_funds_conn

logger = logging.getLogger("app.funds")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept": "*/*"}
NASDAQ_H = dict(UA, Accept="application/json, text/plain, */*", Origin="https://www.nasdaq.com", Referer="https://www.nasdaq.com/")
_now = lambda: datetime.now(timezone.utc).isoformat()

# NSE constituent files: normalised index name -> file. Names are matched inside ETF / index-fund names (longest first).
NSE_INDEX_FILES = {
    "NIFTY 50": "ind_nifty50list", "NIFTY NEXT 50": "ind_niftynext50list", "NIFTY 100": "ind_nifty100list",
    "NIFTY 200": "ind_nifty200list", "NIFTY 500": "ind_nifty500list", "NIFTY BANK": "ind_niftybanklist",
    "NIFTY IT": "ind_niftyitlist", "NIFTY FINANCIAL SERVICES": "ind_niftyfinancelist", "NIFTY PSU BANK": "ind_niftypsubanklist",
    "NIFTY MIDCAP 150": "ind_niftymidcap150list", "NIFTY MIDCAP 50": "ind_niftymidcap50list", "NIFTY SMALLCAP 250": "ind_niftysmallcap250list",
    "NIFTY SMALLCAP 50": "ind_niftysmallcap50list", "NIFTY LARGEMIDCAP 250": "ind_niftylargemidcap250list",
    "NIFTY MIDSMALLCAP 400": "ind_niftymidsmallcap400list", "NIFTY AUTO": "ind_niftyautolist", "NIFTY PHARMA": "ind_niftypharmalist",
    "NIFTY FMCG": "ind_niftyfmcglist", "NIFTY METAL": "ind_niftymetallist", "NIFTY ENERGY": "ind_niftyenergylist",
    "NIFTY INFRASTRUCTURE": "ind_niftyinfralist", "NIFTY PRIVATE BANK": "ind_nifty_privatebanklist", "NIFTY REALTY": "ind_niftyrealtylist",
    "NIFTY MEDIA": "ind_niftymedialist", "NIFTY HEALTHCARE": "ind_niftyhealthcarelist", "NIFTY LOW VOLATILITY 30": "ind_niftylowvolatility30list",
    "NIFTY ALPHA 50": "ind_niftyalpha50list", "NIFTY QUALITY 30": "ind_niftyquality30list", "NIFTY MOMENTUM 30": "ind_niftymomentum30list",
    "NIFTY 50 EQUAL WEIGHT": "ind_nifty50equalweightlist", "NIFTY100 QUALITY 30": "ind_nifty100quality30list",
}
_INDEX_KEYS = sorted(NSE_INDEX_FILES, key=len, reverse=True)
_constituents_cache: dict[str, list[tuple[str, str]] | None] = {}


def _norm(s: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper())
    s = re.sub(r"NIFTY(\d)", r"NIFTY \1", s)          # NIFTY50 -> NIFTY 50
    s = s.replace("NIFTY50", "NIFTY 50").replace("MIDCAP150", "MIDCAP 150").replace("SMALLCAP250", "SMALLCAP 250")
    return re.sub(r"\s+", " ", s).strip()


def detect_index(text: str) -> str | None:
    """Which published index a fund tracks, read from its name (e.g. 'UTI Nifty 50 Index Fund' -> 'NIFTY 50')."""
    t = _norm(text)
    for k in _INDEX_KEYS:
        if re.search(rf"(?<![A-Z0-9]){re.escape(k)}(?![A-Z0-9])", t):
            return k
    return None


def constituents(index_key: str) -> list[tuple[str, str]] | None:
    if index_key not in _constituents_cache:
        try:
            r = requests.get(f"https://nsearchives.nseindia.com/content/indices/{NSE_INDEX_FILES[index_key]}.csv", headers=UA, timeout=30)
            rows = list(csv.DictReader(io.StringIO(r.text))) if r.status_code == 200 else []
            _constituents_cache[index_key] = [(x["Symbol"].strip(), x["Company Name"].strip()) for x in rows if x.get("Symbol")] or None
        except (requests.RequestException, KeyError):
            _constituents_cache[index_key] = None
    return _constituents_cache[index_key]


def _upsert(fc, row: dict) -> None:
    cols = list(row)
    fc.execute(f"INSERT INTO funds ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
               f"ON CONFLICT(symbol) DO UPDATE SET " + ", ".join(f"{c}=COALESCE(excluded.{c}, {c})" for c in cols if c != "symbol")
               + ", updated_at=CURRENT_TIMESTAMP", [row[c] for c in cols])


def _num(v):
    try:
        return float(str(v).replace("$", "").replace(",", "").replace("%", "")) if v not in (None, "", "-", "--") else None
    except ValueError:
        return None


# ------------------------------------------------------------------------------------------------ universes

def sync_us_etfs(fc) -> int:
    r = requests.get("https://api.nasdaq.com/api/screener/etf?download=true", headers=NASDAQ_H, timeout=60)
    rows = r.json()["data"]["data"]["rows"]
    n = 0
    for x in rows:
        t = (x.get("symbol") or "").strip().upper()
        if not t:
            continue
        _upsert(fc, {"symbol": f"FUND:US:{t}", "kind": "ETF", "market": "US", "ticker": t, "name": (x.get("companyName") or t).strip(),
                     "price": _num(x.get("lastSalePrice")), "source": "nasdaq etf screener"})
        n += 1
    fc.commit()
    return n


def sync_in_etfs(fc) -> int:
    meta = requests.get("https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv", headers=UA, timeout=40).text
    live = {x["symbol"]: x for x in requests.get("https://www.nseindia.com/api/etf", headers=UA, timeout=40).json().get("data", [])}
    n = 0
    for x in csv.DictReader(io.StringIO(meta)):
        sym = (x.get("Symbol") or "").strip()
        if not sym:
            continue
        under_key = (x.get("Underlying Key") or x.get("Underlying Asset") or "").strip()
        name = (x.get("Underlying Asset") if len((x.get("Underlying Asset") or "")) > len(sym) + 6 else x.get("SecurityName") or sym).strip()
        lv = live.get(sym, {})
        idx = detect_index(under_key) or detect_index(x.get("SecurityName", "")) or detect_index(x.get("Underlying Asset", ""))
        _upsert(fc, {"symbol": f"FUND:NSE:{sym}", "kind": "ETF", "market": "IN", "ticker": sym, "name": (x.get("SecurityName") or sym).strip(),
                     "isin": (x.get("ISINNumber") or "").strip() or None, "underlying_asset": (x.get("ETF Underlying") or "").strip() or None,
                     "underlying_index": (under_key or None), "category": (x.get("Underlying Asset") or "").strip() or None,
                     "price": _num(lv.get("ltP")), "nav": _num(lv.get("nav")), "price_date": datetime.now().strftime("%Y-%m-%d") if lv else None,
                     "source": "nse etf list"})
        if idx:
            fc.execute("UPDATE funds SET underlying_index = ? WHERE symbol = ?", (idx.title().replace("Nifty", "Nifty"), f"FUND:NSE:{sym}"))
        n += 1
    fc.commit()
    return n


def sync_in_index_funds(fc) -> int:
    txt = requests.get("https://www.amfiindia.com/spages/NAVAll.txt", headers=UA, timeout=90).text
    cat = amc = None
    n = 0
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        if ";" not in line:
            if line.startswith("Open Ended") or line.startswith("Close Ended") or line.startswith("Interval"):
                cat = line
            elif "Scheme Code" not in line:
                amc = line
            continue
        p = line.split(";")
        if not p[0].isdigit() or not cat or "Index Funds" not in cat:
            continue
        name = " ".join(x for x in p[3:-2] if x and x != "-").strip() if len(p) > 6 else p[3]
        low = name.lower()
        if "direct" not in low or "growth" not in low or "idcw" in low or "dividend" in low or "bonus" in low:
            continue
        isin = next((x for x in (p[1], p[2]) if x and x.startswith("INF")), None)
        sub = re.search(r"Index Funds - (.+?)\)", cat)
        _upsert(fc, {"symbol": f"FUND:MF:{p[0]}", "kind": "INDEX_FUND", "market": "IN", "ticker": p[0], "name": name, "isin": isin, "amc": amc,
                     "category": sub.group(1) if sub else "Index Funds", "scheme_code": p[0], "nav": _num(p[-2]), "nav_date": p[-1],
                     "underlying_index": detect_index(name), "source": "amfi nav"})
        n += 1
    fc.commit()
    return n


# ------------------------------------------------------------------------------------------------ history

def _yahoo_history(ticker: str):
    import yfinance as yf
    df = yf.Ticker(ticker).history(period="max", timeout=15, auto_adjust=False)
    return None if df is None or df.empty else [(i.date().isoformat(), float(r["Close"])) for i, r in df.iterrows() if r["Close"] == r["Close"]]


def history_in_etfs(fc, limit=None) -> tuple[int, int]:
    got = rows = 0
    syms = [r["ticker"] for r in fc.execute("SELECT ticker FROM funds WHERE kind='ETF' AND market='IN'")][:limit]
    for t in syms:
        time.sleep(0.25)
        try:
            h = _yahoo_history(f"{t}.NS") or _yahoo_history(f"{t}.BO")
        except Exception:  # noqa: BLE001
            h = None
        if h:
            fc.executemany("INSERT OR REPLACE INTO fund_prices (symbol, date, close) VALUES (?,?,?)", [(f"FUND:NSE:{t}", d, c) for d, c in h])
            fc.execute("UPDATE funds SET price=?, price_date=? WHERE symbol=?", (h[-1][1], h[-1][0], f"FUND:NSE:{t}"))
            got += 1; rows += len(h)
            fc.commit()
    return got, rows


def history_in_index_funds(fc, limit=None) -> tuple[int, int]:
    got = rows = 0
    for r in list(fc.execute("SELECT scheme_code FROM funds WHERE kind='INDEX_FUND'"))[:limit]:
        time.sleep(0.2)
        try:
            d = requests.get(f"https://api.mfapi.in/mf/{r['scheme_code']}", timeout=40).json().get("data") or []
        except (requests.RequestException, ValueError):
            continue
        pts = []
        for x in d:
            try:
                pts.append((datetime.strptime(x["date"], "%d-%m-%Y").date().isoformat(), float(x["nav"])))
            except (ValueError, KeyError):
                pass
        if pts:
            fc.executemany("INSERT OR REPLACE INTO fund_prices (symbol, date, close) VALUES (?,?,?)", [(f"FUND:MF:{r['scheme_code']}", dd, c) for dd, c in pts])
            got += 1; rows += len(pts)
            fc.commit()
    return got, rows


# ------------------------------------------------------------------------------------------------ facts + holdings

def facts_us(fc, tickers: list[str]) -> tuple[int, int]:
    """Fee, AUM, category, issuer, top-10 holdings, sector weights from Yahoo; plus price history. Returns (funds with facts, with holdings)."""
    import yfinance as yf
    ok = held = 0
    for t in tickers:
        time.sleep(0.3)
        try:
            sym = f"FUND:US:{t}"
            try:
                tk = yf.Ticker(t)
                info = tk.info or {}
                fd = tk.funds_data
                top, sect, ov = fd.top_holdings, fd.sector_weightings, fd.fund_overview or {}
            except Exception as e:  # noqa: BLE001
                logger.info("facts %s failed: %s", t, str(e)[:80])
                continue
            er = info.get("netExpenseRatio") or info.get("annualReportExpenseRatio")
            ac = {k: (v or 0) for k, v in (fd.asset_classes or {}).items()}   # Yahoo returns None for classes it doesn't report
            _upsert(fc, {"symbol": sym, "kind": "ETF", "market": "US", "name": info.get("longName") or info.get("shortName") or t,
                         "amc": ov.get("family"), "category": ov.get("categoryName") or info.get("category"),
                         "expense_ratio": None if er is None else (er * 100 if er < 0.2 else er) if er <= 5 else er,
                         "aum": info.get("totalAssets"), "price": info.get("regularMarketPrice") or info.get("navPrice"),
                         "nav": info.get("navPrice"), "underlying_asset": "EQUITY" if ac.get("stockPosition", 0) > 0.5 else
                         ("DEBT" if ac.get("bondPosition", 0) > 0.5 else "MIXED/OTHER"), "facts_at": _now(), "source": "yahoo fund data"})
            if top is not None and len(top):
                fc.execute("DELETE FROM fund_holdings WHERE fund = ?", (sym,))
                for rank, (hs, row) in enumerate(top.iterrows(), 1):
                    w = row.get("Holding Percent")
                    fc.execute("INSERT OR REPLACE INTO fund_holdings (fund, rank, holding_symbol, holding_name, weight_pct, source) VALUES (?,?,?,?,?,?)",
                               (sym, rank, str(hs), row.get("Name"), None if w != w or w is None else float(w) * 100, "yahoo top holdings"))
                held += 1
            if sect:
                fc.execute("DELETE FROM fund_sectors WHERE fund = ?", (sym,))
                for k, v in sect.items():
                    if v:
                        fc.execute("INSERT OR REPLACE INTO fund_sectors (fund, sector, weight_pct) VALUES (?,?,?)", (sym, k.replace("_", " ").title(), float(v) * 100))
            try:
                h = _yahoo_history(t)
                if h:
                    fc.executemany("INSERT OR REPLACE INTO fund_prices (symbol, date, close) VALUES (?,?,?)", [(sym, d, c) for d, c in h])
                    fc.execute("UPDATE funds SET price_date=? WHERE symbol=?", (h[-1][0], sym))
            except Exception:  # noqa: BLE001
                pass
            fc.commit()
        except Exception as e:  # noqa: BLE001 — one odd fund must never stop a 5,000-fund run
            logger.warning("facts %s skipped: %s: %s", t, type(e).__name__, str(e)[:100])
            fc.rollback()
            continue
        ok += 1
    return ok, held


def holdings_in(fc) -> tuple[int, int]:
    """India: holdings of every fund whose tracked index we can name = that index's constituents (NSE publishes no weights)."""
    with get_market_conn() as m:
        ours = {r[0] for r in m.execute("SELECT symbol FROM stocks WHERE market = 'IN'")}
    funds = list(fc.execute("SELECT symbol, underlying_index, name FROM funds WHERE market='IN' AND (underlying_index IS NOT NULL OR kind='INDEX_FUND')"))
    done = rows = 0
    for f in funds:
        key = detect_index(f["underlying_index"] or "") or detect_index(f["name"])
        cons = constituents(key) if key else None
        if not cons:
            continue
        fc.execute("DELETE FROM fund_holdings WHERE fund = ?", (f["symbol"],))
        for rank, (s, nm) in enumerate(cons, 1):
            fc.execute("INSERT OR REPLACE INTO fund_holdings (fund, rank, holding_symbol, holding_name, weight_pct, source) VALUES (?,?,?,?,?,?)",
                       (f["symbol"], rank, f"NSE:{s}" if f"NSE:{s}" in ours else s, nm, None, "index constituents (unweighted)"))
        fc.execute("UPDATE funds SET underlying_index = ?, facts_at = ? WHERE symbol = ?", (key.title(), _now(), f["symbol"]))
        done += 1; rows += len(cons)
    fc.commit()
    return done, rows


def quality_report(fc) -> dict:
    q = lambda sql, *a: fc.execute(sql, a).fetchone()[0]
    out = {}
    for kind, mk in (("ETF", "US"), ("ETF", "IN"), ("INDEX_FUND", "IN")):
        n = q("SELECT COUNT(*) FROM funds WHERE kind=? AND market=?", kind, mk)
        if not n:
            continue
        pct = lambda cond: f"{100 * q(f'SELECT COUNT(*) FROM funds WHERE kind=? AND market=? AND {cond}', kind, mk) // n}%"
        out[f"{kind} {mk}"] = {
            "funds": n, "price/NAV": pct("(price IS NOT NULL OR nav IS NOT NULL)"), "category": pct("category IS NOT NULL"),
            "issuer/AMC": pct("amc IS NOT NULL"), "ISIN": pct("isin IS NOT NULL"), "expense ratio": pct("expense_ratio IS NOT NULL"),
            "AUM": pct("aum IS NOT NULL"), "tracked index named": pct("underlying_index IS NOT NULL"),
            "has holdings": f"{100 * q('SELECT COUNT(DISTINCT h.fund) FROM fund_holdings h JOIN funds f ON f.symbol=h.fund WHERE f.kind=? AND f.market=?', kind, mk) // n}%",
            "has price history": f"{100 * q('SELECT COUNT(DISTINCT p.symbol) FROM fund_prices p JOIN funds f ON f.symbol=p.symbol WHERE f.kind=? AND f.market=?', kind, mk) // n}%",
        }
    return out
