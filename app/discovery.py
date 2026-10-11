"""Discovery for the Analytics landing: per-stock metrics (stock_metrics) and the screens built on them.

The metrics are computed with the SAME evaluate_framework the story page uses (it now exposes the number behind each rule), so a stock
"passes your rules" here exactly when its story page says so. The numbers are stored once; the user's own thresholds are applied at
request time, so "Passes your rules" always means THEIR rules. Banks/lenders follow the usual exemptions. Nothing here calls an AI model."""
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone

from app import framework as fw
from app.db import get_market_conn
from app.us_securities import is_derivative

_CACHE: dict = {"at": 0.0, "stamp": None, "rows": []}
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------- build

def _annual_extras(annual: list) -> dict:
    """Sales CAGR (3y), average ROCE (3y), latest dividend payout and cash conversion from annual statements (India)."""
    out = {"sales_cagr_3y": None, "roce_3y": None, "div_payout": None}
    rows = [a for a in annual if a.get("revenue_cr")]
    if len(rows) >= 4:
        last, ago = rows[-1], rows[-4]
        years = (date.fromisoformat(last["period_end_date"]) - date.fromisoformat(ago["period_end_date"])).days / 365.25
        if ago["revenue_cr"] > 0 and last["revenue_cr"] > 0 and 2.5 <= years <= 3.5:
            out["sales_cagr_3y"] = round(((last["revenue_cr"] / ago["revenue_cr"]) ** (1 / years) - 1) * 100, 1)
    roce = [a.get("roce_pct") for a in annual[-3:] if a.get("roce_pct") is not None]
    if len(roce) == 3:
        out["roce_3y"] = round(sum(roce) / 3, 1)
    dp = next((a.get("dividend_payout_pct") for a in reversed(annual) if a.get("dividend_payout_pct") is not None), None)
    out["div_payout"] = dp
    return out


def _ttm_cagr_3y(quarterly: list) -> float | None:
    """Sales growth per year over 3 years from quarterly results, for stocks with no annual statements (US, and India names IndianAPI has
    no years for): the last four quarters' sales against the same four quarters three years earlier. Only when both windows are four
    consecutive quarters (a missing quarter would understate a year), otherwise None - never a guess."""
    rows = [(date.fromisoformat(r["period_end_date"]), r["revenue_cr"]) for r in quarterly if r["revenue_cr"] is not None]
    rows.sort()

    def window(end_idx: int):
        w = rows[end_idx - 3:end_idx + 1] if end_idx >= 3 else None
        if not w or not 250 <= (w[-1][0] - w[0][0]).days <= 285 or any(not 75 <= (b[0] - a[0]).days <= 105 for a, b in zip(w, w[1:])):
            return None
        return w

    if len(rows) < 16:
        return None
    now = window(len(rows) - 1)
    if not now:
        return None
    target = date.fromordinal(now[-1][0].toordinal() - round(3 * 365.25))
    ago_idx = min(range(len(rows)), key=lambda i: abs((rows[i][0] - target).days))
    if abs((rows[ago_idx][0] - target).days) > 20:
        return None
    ago = window(ago_idx)
    if not ago:
        return None
    a, n = sum(x[1] for x in ago), sum(x[1] for x in now)
    return round(((n / a) ** (1 / 3) - 1) * 100, 1) if a > 0 and n > 0 else None


def _us_extras(rows: list, last_quarter_end: str) -> dict:
    """US 3-year average ROCE and latest dividend payout from the SEC 10-K years in us_annual (USD millions). ROCE is operating profit over
    (assets - current liabilities) and only where that capital is positive; payout is dividends paid over net profit and only where both
    are positive. Nothing is filled from a year the filing doesn't state."""
    out = {"roce_3y": None, "div_payout": None}
    rows = sorted((r for r in rows if r["period_end_date"] != "0000-00-00"), key=lambda r: r["period_end_date"])
    if not rows:
        return out
    last3 = rows[-3:]
    vals = []
    for r in last3:
        op, a, cl = r["operating_profit_m"], r["assets_m"], r["current_liabilities_m"]
        if op is None or a is None or cl is None or a - cl <= 0:
            vals = []
            break
        vals.append(op / (a - cl) * 100)
    if len(vals) == 3 and (date.fromisoformat(last3[-1]["period_end_date"]) - date.fromisoformat(last3[0]["period_end_date"])).days <= 2 * 366 + 20:
        out["roce_3y"] = round(sum(vals) / 3, 1)
    fy = rows[-1]
    stale = (date.fromisoformat(last_quarter_end) - date.fromisoformat(fy["period_end_date"])).days > 550
    if not stale and fy["dividends_paid_m"] and fy["dividends_paid_m"] > 0 and fy["net_profit_m"] and fy["net_profit_m"] > 0:
        out["div_payout"] = round(fy["dividends_paid_m"] / fy["net_profit_m"] * 100, 1)
    return out


def _drop_us_duplicates(conn) -> None:
    """The SEC lists some companies under two tickers (a bond or note line, an old and a new ticker) that carry identical figures. Keep the one
    that trades the most (then the shorter ticker); a dash share class (BRK-A / BRK-B) is a real second security and stays."""
    groups: dict[tuple, list] = {}
    for r in conn.execute("SELECT symbol, name, roce, opm, de, growth_min4, cfo_pat, last_q, mcap FROM stock_metrics WHERE market = 'US'"):
        if r["roce"] is None and r["opm"] is None and r["growth_min4"] is None:
            continue
        groups.setdefault((r["name"], r["roce"], r["opm"], r["de"], r["growth_min4"], r["cfo_pat"], r["last_q"]), []).append(r)
    drop = []
    for g in groups.values():
        if len(g) < 2:
            continue
        vol = {r["symbol"]: (conn.execute("SELECT COALESCE(SUM(volume), 0) FROM (SELECT volume FROM price_history WHERE symbol = ? ORDER BY date DESC LIMIT 20)", (r["symbol"],)).fetchone()[0]) for r in g}
        g.sort(key=lambda r: (-vol[r["symbol"]], len(r["symbol"])))  # the line that actually trades most is the company's own stock
        drop += [r["symbol"] for r in g[1:] if "-" not in r["symbol"]]
    conn.executemany("DELETE FROM stock_metrics WHERE symbol = ?", [(x,) for x in drop])


def build_metrics(progress=None, only: str | None = None) -> int:
    from app.annual import annual_rows
    from app.ai.checklist import evaluate_framework, is_bank_or_financial
    from app.routes.analytics import _compute_pe, _yoy

    th = fw.builtin()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    written = 0
    with get_market_conn() as conn:
        try:
            in_mcap = {r[0]: r[1] for r in conn.execute("SELECT symbol, mcap_cr FROM india_mcap")}
            us_over = {r[0]: r[1] for r in conn.execute("SELECT symbol, mcap_m FROM us_mcap_override")}
            us_ann: dict[str, list] = {}
            for r in conn.execute("SELECT * FROM us_annual"):
                us_ann.setdefault(r["symbol"], []).append(r)
        except Exception:  # noqa: BLE001 - tables not filled yet on this database
            in_mcap, us_ann, us_over = {}, {}, {}
        stocks = conn.execute(
            """SELECT symbol, name, market, sector, cap_tier FROM stocks s
               WHERE EXISTS (SELECT 1 FROM quarterly_data q WHERE q.symbol = s.symbol AND q.quarter_label NOT LIKE '%annual%')"""
            + (" AND symbol = ?" if only else ""), ((only,) if only else ())).fetchall()
        try:  # IndianAPI's 3-year compounded sales growth: used only where our own (annual or quarterly) figure is missing; it agrees with ours within 3 pp for 99% of stocks that have both
            from app.indianapi_db import INDIANAPI_DB
            import sqlite3 as _sq
            _ia = _sq.connect(f"file:{INDIANAPI_DB}?mode=ro", uri=True)
            ia_g3 = {r[0]: r[1] for r in _ia.execute("SELECT symbol, pct FROM ia_growth WHERE metric = 'Compounded Sales Growth' AND horizon = '3Y' AND pct IS NOT NULL")}
            _ia.close()
        except Exception:  # noqa: BLE001 - IndianAPI staging absent: the screens simply have fewer 3-year figures
            ia_g3 = {}
        us_tickers = {x["symbol"].split(":", 1)[1]: x["name"] for x in stocks if x["market"] == "US"}
        for i, s in enumerate(stocks, 1):
            sym = s["symbol"]
            if s["market"] == "US" and is_derivative(sym.split(":", 1)[1], us_tickers):
                continue  # a warrant / unit / right / preferred line: it copies its parent's numbers, so it is not screened
            allq = conn.execute("SELECT * FROM quarterly_data WHERE symbol = ? ORDER BY period_end_date", (sym,)).fetchall()
            quarterly = [r for r in allq if "annual" not in (r["quarter_label"] or "").lower()]
            if not quarterly:
                continue
            by_end = {r["period_end_date"]: r for r in quarterly}
            rows = []
            for r in quarterly[-8:]:
                d = dict(r)
                if d.get("revenue_growth_pct") is None:
                    d["revenue_growth_pct"] = _yoy(r, by_end, stored=False)
                rows.append(d)
            annual = annual_rows(conn, sym) if sym.startswith("NSE:") else []
            try:
                fwk = evaluate_framework(rows, s["sector"], th, annual=annual)
            except Exception:  # noqa: BLE001 - one odd stock must not stop the build
                continue
            raws = {f["key"]: f["raw"] for f in fwk["filters"] if f.get("raw") is not None}
            try:
                pe, lo, hi = _compute_pe(conn, sym, quarterly)
            except Exception:  # noqa: BLE001 - e.g. a stored price of NULL: no P/E for that stock, not a failed build
                pe = lo = hi = None
            px = conn.execute("SELECT close FROM price_history WHERE symbol = ? ORDER BY date DESC LIMIT 1", (sym,)).fetchone()
            ex = _annual_extras(annual)
            if ex["sales_cagr_3y"] is None:
                ex["sales_cagr_3y"] = _ttm_cagr_3y(quarterly)
            if ex["sales_cagr_3y"] is None and sym in ia_g3:
                ex["sales_cagr_3y"] = ia_g3[sym]
            mcap = None
            if s["market"] == "IN":
                mcap = in_mcap.get(sym)
            else:
                if sym in us_ann:
                    ex.update({k: v for k, v in _us_extras(us_ann[sym], quarterly[-1]["period_end_date"]).items() if v is not None})
                sh = conn.execute("SELECT shares_outstanding sh FROM quarterly_data WHERE symbol = ? AND shares_outstanding IS NOT NULL ORDER BY period_end_date DESC LIMIT 1", (sym,)).fetchone()
                if sh and sh["sh"] and px and px["close"]:
                    mcap = round(sh["sh"] * px["close"] / 1e6, 1)
                    floor = {"large": 10_000, "mid": 2_000, "small": 300}.get(s["cap_tier"])
                    if floor and mcap < floor * 0.5:  # the share count disagrees with Nasdaq's size band (multi-class shares, stale cover page)
                        mcap = us_over.get(sym)
                if mcap is None and sym in us_over:
                    mcap = us_over[sym]
            tier = s["cap_tier"]
            if tier is None and s["market"] == "US" and mcap:  # Nasdaq's screener list had no tier: same cut-offs on price x SEC share count
                tier = "large" if mcap >= 10_000 else "mid" if mcap >= 2_000 else "small" if mcap >= 300 else "micro"
            latest = rows[-1]
            conn.execute(
                """INSERT OR REPLACE INTO stock_metrics (symbol, market, name, sector, cap_tier, bank, raws, roce, de, growth_min4, opm, cfo_pat,
                   sales_cagr_3y, roce_3y, div_payout, pe, pe_low, pe_high, price, mcap, last_q, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sym, s["market"], s["name"], s["sector"], tier, 1 if is_bank_or_financial(s["sector"]) else 0, json.dumps(raws),
                 raws.get("roe" if is_bank_or_financial(s["sector"]) else "roce"), raws.get("de"), raws.get("sales_growth"), raws.get("opm"), raws.get("cfo_pat"),
                 ex["sales_cagr_3y"], ex["roce_3y"], ex["div_payout"], pe, lo, hi, px["close"] if px else None, mcap, quarterly[-1]["period_end_date"], now))
            written += 1
            if i % 250 == 0:
                conn.commit()
                if progress:
                    progress(written)
        if not only:  # a full build: anything not rebuilt (a line that is no longer screened) goes
            conn.execute("DELETE FROM stock_metrics WHERE updated_at != ?", (now,))
            _drop_us_duplicates(conn)
        conn.commit()
    with _LOCK:
        _CACHE["at"] = 0.0
    return written


# ---------------------------------------------------------------------------- judging against a user's thresholds

def judge(raws: dict, bank: bool, th: dict) -> dict:
    """{passed, total, failing: [labels]} for one stock against the user's rules (same comparisons as evaluate_framework)."""
    passed = total = 0
    failing: list[str] = []
    for key, r in th.items():
        if not r.get("enabled", True):
            continue
        if (key == "roce" and bank) or (key == "roe" and not bank):
            continue
        raw = raws.get(key)
        if raw is None:
            continue
        v, op = r["value"], r["op"]
        ok = raw > v if op == ">" else (raw < v if op == "<" else raw >= -v)  # "≤": promoter may fall by at most v pp
        total += 1
        passed += 1 if ok else 0
        if not ok:
            failing.append(r["label"])
    return {"passed": passed, "total": total, "failing": failing}


# ---------------------------------------------------------------------------- screens

SCREENS = [
    {"key": "rules", "label": "Passes your rules", "star": True,
     "desc": "Stocks that clear the thresholds you set on Investment rules. Change them there and this list changes."},
    {"key": "compounders", "label": "Steady compounders", "desc": "High returns on capital that stayed high for three years, with sales growing 10% a year or more. India only for now."},
    {"key": "growers", "label": "Fast growers", "desc": "Sales up more than 20% on the year in each of the last four quarters, and a margin above 10%."},
    {"key": "financials", "label": "Strong banks & insurers", "desc": "Banks, lenders and insurers that earn a return on equity of 12% or more, more than 1% on assets, keep at least 8% of the balance sheet as equity, and trade below 3 times book value."},
    {"key": "debtfree", "label": "Debt-free", "desc": "Almost no borrowings and a return on capital above 12%. Banks and lenders are left out."},
    {"key": "cash", "label": "Cash-rich", "desc": "Profit that turns into real cash: operating cash flow at least 90% of net profit, with a return on capital above 12%."},
    {"key": "dividend", "label": "Dividend payers", "desc": "Pay out at least 20% of profit as dividends and earn more than 12% on capital. India only for now."},
    {"key": "big", "label": "Big and stable", "desc": "Large companies with moderate debt and a return on capital above 12%."},
    {"key": "cheap", "label": "Cheap vs own history", "desc": "Trading in the cheapest third of their own three-year P/E range, with a return on capital above 15%."},
]
EXTRA_SCREENS = {
    "browse": {"key": "browse", "label": "Browse", "desc": "Every stock in this group, best match against your rules first."},
    "custom": {"key": "custom", "label": "Your own screen", "desc": "Stocks that match the filters you picked below."},
    "largest": {"key": "largest", "label": "Biggest companies", "desc": "The largest companies in this market by market cap, biggest first. India uses AMFI's 6-month average; US uses today's price times shares."},
}
SCREEN_BY_KEY = {**{s["key"]: s for s in SCREENS}, **EXTRA_SCREENS}


def _load() -> list[dict]:
    with _LOCK:
        if _CACHE["rows"] and time.time() - _CACHE["at"] < 600:
            return _CACHE["rows"]
    with get_market_conn() as m:
        try:
            rows = [dict(r) for r in m.execute("SELECT * FROM stock_metrics")]
        except Exception:  # noqa: BLE001 - table not there yet
            rows = []
    for r in rows:
        r["raws"] = json.loads(r["raws"] or "{}")
        # Screens and sorting only trust sane figures: a 900% ROCE is a near-zero capital base, and a negative debt/equity is negative
        # equity (not "no debt"). The rule judging keeps the raw numbers, exactly as the story page does.
        if r["roce"] is not None and abs(r["roce"]) > 150:
            r["roce"] = None
        if r["de"] is not None and r["de"] < 0:
            r["de"] = None
        if r["opm"] is not None and abs(r["opm"]) > 100:  # a margin beyond +-100% is a revenue line that isn't the whole top line (lenders, insurers)
            r["opm"] = None
        if r["pe"] is not None and r["pe"] < 2:  # under 2x almost always means a price and an EPS that don't belong together
            r["pe"] = None
    with _LOCK:
        _CACHE.update(at=time.time(), rows=rows)
    return rows


def metrics_age_days() -> float | None:
    with get_market_conn() as m:
        try:
            r = m.execute("SELECT MAX(updated_at) a FROM stock_metrics").fetchone()
        except Exception:  # noqa: BLE001
            return None
    if not r or not r["a"]:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(r["a"].replace(" ", "T") + "+00:00")).total_seconds() / 86400


CAPS = ("large", "mid", "small", "micro")
CUSTOM_FIELDS = {  # form field -> (stock_metrics key, comparison, label)
    "roce": ("roce", ">=", "ROCE"), "de": ("de", "<=", "Debt / equity"), "growth": ("sales_cagr_3y", ">=", "Sales growth (3y)"),
    "pe": ("pe", "<=", "P/E"), "opm": ("opm", ">=", "Operating margin"), "div": ("div_payout", ">=", "Dividend payout"),
}
CUSTOM_SORTS = {"roce": ("roce", True), "growth": ("sales_cagr_3y", True), "pe": ("pe", False), "div": ("div_payout", True), "rules": (None, True)}


STALE_DAYS = 400          # a stock whose latest quarterly result is older than this is not screened (delisted, or results stopped)
MIN_MCAP = {"IN": 300.0, "US": 150.0}   # Rs crore / USD millions: the named screens leave out the tiniest companies, whose ratios swing wildly
NOISE_SECTORS = {"Blank Checks"}        # shell companies with no business yet: browsable by search, not offered as an industry tile


def _visible(r: dict) -> bool:
    """Stocks the screens show at all: results not stale (no latest-quarter date yet = a pre-upgrade row, shown)."""
    lq = r.get("last_q")
    if not lq:
        return True
    return (date.today() - date.fromisoformat(lq)).days <= STALE_DAYS


def facets(market: str = "IN") -> dict:
    """Counts for the browse tiles: size tiers and the largest sectors in a market."""
    rows = [r for r in _load() if (market == "ALL" or r["market"] == market) and _visible(r)]
    caps, sectors = {}, {}
    for r in rows:
        if r["cap_tier"]:
            caps[r["cap_tier"]] = caps.get(r["cap_tier"], 0) + 1
        sec = r["sector"]
        if sec and sec != "Unclassified" and sec not in NOISE_SECTORS:
            sectors[sec] = sectors.get(sec, 0) + 1
    return {"caps": [(c, caps.get(c, 0)) for c in CAPS if caps.get(c)],
            "sectors": sorted(sectors.items(), key=lambda kv: -kv[1])[:14], "total": len(rows)}


def _pos_in_range(r: dict):
    if r["pe"] and r["pe_low"] is not None and r["pe_high"] and r["pe_high"] > r["pe_low"]:
        return (r["pe"] - r["pe_low"]) / (r["pe_high"] - r["pe_low"])
    return None


def run_screen(key: str, th: dict, market: str = "IN", limit: int = 60, cap: str | None = None, sector: str | None = None,
               custom: dict | None = None) -> dict:
    rows = [r for r in _load() if (market == "ALL" or r["market"] == market) and _visible(r)]
    if cap in CAPS:
        rows = [r for r in rows if r["cap_tier"] == cap]
    if sector:
        rows = [r for r in rows if r["sector"] == sector]
    named = key in ("rules", "compounders", "growers", "financials", "debtfree", "cash", "dividend", "big", "cheap")
    hits: list[tuple] = []
    for r in rows:
        roce, de, bank = r["roce"], r["de"], bool(r["bank"])
        if named and (r["mcap"] is None or r["mcap"] < MIN_MCAP.get(r["market"], 0)):
            continue
        if key == "rules":
            j = judge(r["raws"], bank, th)
            if j["total"] < 7:  # too few rules could be judged (missing data) for "passes" to mean much
                continue
            miss = len(j["failing"])
            if miss <= 1:
                hits.append((miss, -j["total"], -(roce or 0), r, j))
            continue
        if key == "browse":  # everything in the chosen group, best match against the user's rules first
            j = judge(r["raws"], bank, th)
            if j["total"] >= 1:  # banks and US finance names have only a couple of checkable rules: still listed, ranked by how many they pass
                hits.append((len(j["failing"]) / j["total"], -j["total"], -(roce or 0), r, j))
            continue
        if key == "largest":
            if r["mcap"]:
                hits.append((-r["mcap"], 0, 0, r, None))
            continue
        if key == "custom":
            c = custom or {}
            ok = True
            for f, (col, op, _) in CUSTOM_FIELDS.items():
                if c.get(f) is None:
                    continue
                v = r[col]
                if v is None or (op == ">=" and v < c[f]) or (op == "<=" and v > c[f]):
                    ok = False
                    break
            if ok and c.get("div") is not None and r["div_payout"] is None:
                ok = False
            if not ok:
                continue
            col, desc = CUSTOM_SORTS.get(c.get("sort") or "roce", CUSTOM_SORTS["roce"])
            if col is None:
                j = judge(r["raws"], bank, th)
                hits.append((len(j["failing"]), -j["total"], -(roce or 0), r, j))
            else:
                v = r[col]
                hits.append((0 if v is not None else 1, -(v or 0) if desc else (v or 0), 0, r, None))
            continue
        # The named screens rank by quality first, then size: an extreme figure (1,700% growth, 600x cash cover) is a tiny base, not a better stock.
        if key == "compounders":
            ok = r["roce_3y"] is not None and r["roce_3y"] >= 20 and r["sales_cagr_3y"] is not None and r["sales_cagr_3y"] >= 10
            sort = -min(r["roce_3y"] or 0, 100)
        elif key == "growers":
            ok = r["growth_min4"] is not None and r["growth_min4"] > 20 and r["opm"] is not None and r["opm"] > 10
            sort = -min(r["growth_min4"] or 0, 100)
        elif key == "financials":
            rw = r["raws"]
            ok = bank and all(rw.get(k) is not None for k in ("roe", "roa", "equity_assets", "pb")) and rw["roe"] >= 12 and rw["roa"] >= 1.0 and rw["equity_assets"] >= 8 and 0 < rw["pb"] <= 3
            sort = -min(rw.get("roe") or 0, 40)
        elif key == "debtfree":
            ok = not bank and de is not None and de < 0.05 and roce is not None and roce >= 12
            sort = -min(roce or 0, 100)
        elif key == "cash":
            ok = r["cfo_pat"] is not None and 0.9 <= r["cfo_pat"] and roce is not None and roce >= 12 and not bank
            sort = -min(r["cfo_pat"] or 0, 2.0)
        elif key == "dividend":
            ok = r["div_payout"] is not None and 20 <= r["div_payout"] <= 100 and roce is not None and roce >= 12  # over 100% pays out more than it earns
            sort = -(r["div_payout"] or 0)
        elif key == "big":
            ok = r["cap_tier"] == "large" and roce is not None and roce >= 12 and (bank or (de is not None and de < 1))
            sort = -min(roce or 0, 100)
        elif key == "cheap":
            pos = _pos_in_range(r)
            ok = pos is not None and pos <= 0.34 and roce is not None and roce >= 15
            sort = pos if pos is not None else 9
        else:
            continue
        if ok:
            hits.append((sort, -(r["mcap"] or 0), 0, r, None))  # same quality score: the bigger company first
    hits.sort(key=lambda h: (h[0], h[1], h[2]))
    out = []
    for _, _, _, r, j in hits[:limit]:
        out.append({**{k: r[k] for k in ("symbol", "name", "market", "sector", "cap_tier", "roce", "de", "pe", "sales_cagr_3y", "growth_min4", "cfo_pat", "div_payout", "roce_3y", "bank", "opm", "mcap")}, "raws": r["raws"],
                    "judge": judge(r["raws"], bool(r["bank"]), th) if key in ("rules", "browse") else None})
    return {"count": len(hits), "rows": out}
