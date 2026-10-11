"""Yahoo quarterly statements -> USD-millions quarter dicts (shared by scripts/fill_adr_yahoo.py and scripts/fill_us_gaps_yahoo.py)."""
import math

import yfinance as yf

FX: dict[str, float | None] = {}


def fx_to_usd(cur: str | None) -> float | None:
    if not cur or cur == "USD":
        return 1.0
    if cur in FX:
        return FX[cur]
    scale = 1.0
    c = cur
    if cur in ("GBp", "GBX"):
        c, scale = "GBP", 0.01
    rate = None
    try:
        h = yf.Ticker(f"{c}USD=X").history(period="5d")
        if h is not None and not h.empty:
            rate = float(h["Close"].dropna().iloc[-1]) * scale
    except Exception:  # noqa: BLE001
        rate = None
    FX[cur] = rate
    return rate


def _v(df, names, col):
    """First available row among `names` for column `col`; None when absent / NaN."""
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.index:
            x = df.at[n, col] if col in df.columns else None
            if x is not None and not (isinstance(x, float) and math.isnan(x)):
                return float(x)
    return None


def build_rows(sym: str, price: float | None):
    t = yf.Ticker(sym.split(":", 1)[1])
    inc, bal, cf = t.quarterly_income_stmt, t.quarterly_balance_sheet, t.quarterly_cashflow
    if inc is None or inc.empty:
        return None, "no income statement"
    info = {}
    try:
        info = t.info or {}
    except Exception:  # noqa: BLE001
        pass
    cur = info.get("financialCurrency") or "USD"
    fx = fx_to_usd(cur)
    if fx is None:
        return None, f"no FX rate for {cur}"
    mcap = info.get("marketCap")
    adr_shares = (mcap / price) if (mcap and price) else None
    cols = sorted((c for c in inc.columns), reverse=False)
    M = 1e6 / fx   # value in reporting currency -> USD millions: value * fx / 1e6
    rows = []
    for c in cols:
        end = c.date().isoformat()
        rev = _v(inc, ["Total Revenue", "Operating Revenue"], c)
        op = _v(inc, ["Operating Income", "Total Operating Income As Reported"], c)
        ni = _v(inc, ["Net Income Common Stockholders", "Net Income", "Net Income From Continuing Operation Net Minority Interest"], c)
        pbt = _v(inc, ["Pretax Income"], c)
        tax = _v(inc, ["Tax Provision"], c)
        intr = _v(inc, ["Interest Expense", "Interest Expense Non Operating"], c)
        if rev is None and ni is None:
            continue
        d = {"end": end, "rev": rev * fx / 1e6 if rev is not None else None, "op": op * fx / 1e6 if op is not None else None,
             "ni": ni * fx / 1e6 if ni is not None else None, "pbt": pbt * fx / 1e6 if pbt is not None else None,
             "tax_pct": round(tax / pbt * 100, 1) if tax is not None and pbt else None,
             "fin": abs(intr) * fx / 1e6 if intr is not None else None}
        for k, names in (("assets", ["Total Assets"]), ("ca", ["Current Assets"]), ("cl", ["Current Liabilities"]),
                         ("equity", ["Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"]),
                         ("debt", ["Total Debt"]), ("cash", ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]),
                         ("inv", ["Inventory"])):
            x = _v(bal, names, c)
            d[k] = x * fx / 1e6 if x is not None else None
        x = _v(cf, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"], c)
        d["cfo"] = x * fx / 1e6 if x is not None else None
        rows.append(d)
    if not rows:
        return None, "no usable quarters"
    # year-ago growth where Yahoo holds both quarters
    for r in rows:
        prev = next((p for p in rows if 350 <= (_days(r["end"]) - _days(p["end"])) <= 380), None)
        r["growth"] = round((r["rev"] / prev["rev"] - 1) * 100, 1) if prev and prev["rev"] and r["rev"] and prev["rev"] > 0 else None
    last = rows[-1]
    ttm = rows[-4:]
    if len(ttm) == 4:
        if all(x["op"] is not None for x in ttm) and last["assets"] is not None and last["cl"] is not None and last["assets"] - last["cl"] > 0:
            last["roce"] = round(sum(x["op"] for x in ttm) / (last["assets"] - last["cl"]) * 100, 1)
        if all(x["ni"] is not None for x in ttm) and last["equity"] and last["equity"] > 0:
            last["roe"] = round(sum(x["ni"] for x in ttm) / last["equity"] * 100, 1)
    if last["debt"] is not None and last["equity"] and last["equity"] > 0:
        last["de"] = round(last["debt"] / last["equity"], 2)
    return {"rows": rows, "adr_shares": adr_shares, "currency": cur, "fx": fx}, None


def _days(iso: str) -> int:
    from datetime import date
    return date.fromisoformat(iso).toordinal()


