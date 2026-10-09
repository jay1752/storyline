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


def build_metrics(progress=None, only: str | None = None) -> int:
    from app.annual import annual_rows
    from app.ai.checklist import evaluate_framework, is_bank_or_financial
    from app.routes.analytics import _compute_pe, _yoy

    th = fw.builtin()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    written = 0
    with get_market_conn() as conn:
        stocks = conn.execute(
            """SELECT symbol, name, market, sector, cap_tier FROM stocks s
               WHERE EXISTS (SELECT 1 FROM quarterly_data q WHERE q.symbol = s.symbol AND q.quarter_label NOT LIKE '%annual%')"""
            + (" AND symbol = ?" if only else ""), ((only,) if only else ())).fetchall()
        for i, s in enumerate(stocks, 1):
            sym = s["symbol"]
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
            latest = rows[-1]
            conn.execute(
                """INSERT OR REPLACE INTO stock_metrics (symbol, market, name, sector, cap_tier, bank, raws, roce, de, growth_min4, opm, cfo_pat,
                   sales_cagr_3y, roce_3y, div_payout, pe, pe_low, pe_high, price, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sym, s["market"], s["name"], s["sector"], s["cap_tier"], 1 if is_bank_or_financial(s["sector"]) else 0, json.dumps(raws),
                 raws.get("roe" if is_bank_or_financial(s["sector"]) else "roce"), raws.get("de"), raws.get("sales_growth"), raws.get("opm"), raws.get("cfo_pat"),
                 ex["sales_cagr_3y"], ex["roce_3y"], ex["div_payout"], pe, lo, hi, px["close"] if px else None, now))
            written += 1
            if i % 250 == 0:
                conn.commit()
                if progress:
                    progress(written)
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
    {"key": "debtfree", "label": "Debt-free", "desc": "Almost no borrowings and a return on capital above 12%. Banks and lenders are left out."},
    {"key": "cash", "label": "Cash-rich", "desc": "Profit that turns into real cash: operating cash flow at least 90% of net profit, with a return on capital above 12%."},
    {"key": "dividend", "label": "Dividend payers", "desc": "Pay out at least 20% of profit as dividends and earn more than 12% on capital. India only for now."},
    {"key": "big", "label": "Big and stable", "desc": "Large companies with moderate debt and a return on capital above 12%."},
    {"key": "cheap", "label": "Cheap vs own history", "desc": "Trading in the cheapest third of their own three-year P/E range, with a return on capital above 15%."},
]
SCREEN_BY_KEY = {s["key"]: s for s in SCREENS}


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


def run_screen(key: str, th: dict, market: str = "IN", limit: int = 60) -> dict:
    rows = [r for r in _load() if market == "ALL" or r["market"] == market]
    hits: list[tuple] = []
    for r in rows:
        roce, de, bank = r["roce"], r["de"], bool(r["bank"])
        if key == "rules":
            j = judge(r["raws"], bank, th)
            if j["total"] < 7:  # too few rules could be judged (missing data) for "passes" to mean much
                continue
            miss = len(j["failing"])
            if miss <= 1:
                hits.append((miss, -j["total"], -(roce or 0), r, j))
            continue
        if key == "compounders":
            ok = r["roce_3y"] is not None and r["roce_3y"] >= 20 and r["sales_cagr_3y"] is not None and r["sales_cagr_3y"] >= 10
            sort = -(r["roce_3y"] or 0)
        elif key == "growers":
            ok = r["growth_min4"] is not None and r["growth_min4"] > 20 and r["opm"] is not None and r["opm"] > 10
            sort = -(r["growth_min4"] or 0)
        elif key == "debtfree":
            ok = not bank and de is not None and de < 0.05 and roce is not None and roce >= 12
            sort = -(roce or 0)
        elif key == "cash":
            ok = r["cfo_pat"] is not None and r["cfo_pat"] >= 0.9 and roce is not None and roce >= 12 and not bank
            sort = -(r["cfo_pat"] or 0)
        elif key == "dividend":
            ok = r["div_payout"] is not None and r["div_payout"] >= 20 and roce is not None and roce >= 12
            sort = -(r["div_payout"] or 0)
        elif key == "big":
            ok = r["cap_tier"] == "large" and roce is not None and roce >= 12 and (bank or (de is not None and de < 1))
            sort = -(roce or 0)
        elif key == "cheap":
            pos = None
            if r["pe"] and r["pe_low"] is not None and r["pe_high"] and r["pe_high"] > r["pe_low"]:
                pos = (r["pe"] - r["pe_low"]) / (r["pe_high"] - r["pe_low"])
            ok = pos is not None and pos <= 0.34 and roce is not None and roce >= 15
            sort = pos if pos is not None else 9
        else:
            continue
        if ok:
            hits.append((sort, 0, 0, r, None))
    hits.sort(key=lambda h: (h[0], h[1], h[2]))
    out = []
    for _, _, _, r, j in hits[:limit]:
        out.append({**{k: r[k] for k in ("symbol", "name", "market", "sector", "cap_tier", "roce", "de", "pe", "sales_cagr_3y", "growth_min4", "cfo_pat", "div_payout", "roce_3y", "bank")},
                    "judge": judge(r["raws"], bool(r["bank"]), th) if key == "rules" else None})
    return {"count": len(hits), "rows": out}
