"""Analyst consensus + company panels for the Analytics page, read from the IndianAPI staging DB (data/indianapi.db).

Everything here is optional: no staging file (e.g. on a machine that never loaded it), no row for the stock -> the panel is simply None
and the page renders without it. Nothing is written. Values keep IndianAPI's meaning: analyst-basis EPS excludes one-offs, so it is
labelled 'consensus' and never mixed into the filed EPS rows. Sales / net-profit estimates arrive in INR millions and are shown in INR Cr."""
import json
import sqlite3
from datetime import date

from app.db import DATA_DIR

_PATH = DATA_DIR / "indianapi.db"
_AGE_ORDER = [("NinetyDaysAgo", "90d ago"), ("SixtyDaysAgo", "60d ago"), ("ThirtyDaysAgo", "30d ago"), ("OneWeekAgo", "1w ago")]
_BANDS = [(1.8, "Strong Buy", "pos"), (2.6, "Buy", "pos"), (3.4, "Hold", "neutral"), (4.2, "Sell", "neg"), (5.1, "Strong Sell", "neg")]
_MEASURES = [("EPS", "EPS", "₹", 1.0), ("SAL", "Revenue", "₹ Cr", 0.1), ("NET", "Net profit", "₹ Cr", 0.1)]  # IndianAPI sends INR millions
_QTR = {"QTR1": "Q1", "QTR2": "Q2", "QTR3": "Q3", "QTR4": "Q4"}


def _db():
    if not _PATH.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{_PATH}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("SELECT 1 FROM ia_stock LIMIT 1")
        return c
    except sqlite3.Error:
        return None


def _json(s):
    try:
        return json.loads(s) if s else None
    except (TypeError, ValueError):
        return None


def _money(v, scale=1.0, places=0):
    if v is None:
        return None
    x = v * scale
    return f"{x:,.{places}f}"


def _pos(v, lo, hi):
    """Position of v on the low-high track, 0-100, clamped."""
    if v is None or lo is None or hi is None or hi <= lo:
        return None
    return round(min(100.0, max(0.0, (v - lo) / (hi - lo) * 100)), 1)


def _fy(y):
    return f"FY{str(y)[-2:]}"


def analyst_panel(symbol: str, price: float | None):
    c = _db()
    if c is None:
        return None
    try:
        tg = c.execute("SELECT * FROM ia_target WHERE symbol=?", (symbol,)).fetchone()
        prof = c.execute("SELECT recos_json, risk_category, risk_std FROM ia_profile WHERE symbol=?", (symbol,)).fetchone()
        target = None
        if tg and tg["mean"]:
            snaps = {r["age"]: r["mean"] for r in c.execute("SELECT age, mean FROM ia_target_snapshot WHERE symbol=?", (symbol,))}
            trend = [{"label": lbl, "mean": snaps[k]} for k, lbl in _AGE_ORDER if snaps.get(k)]
            old = trend[0]["mean"] if trend else None
            target = {
                "mean": tg["mean"], "high": tg["high"], "low": tg["low"], "median": tg["median"], "n": tg["n_estimates"],
                "upside": round((tg["mean"] / price - 1) * 100, 1) if price else None,
                "trend": trend, "drift": round((tg["mean"] / old - 1) * 100, 1) if old else None,
                "mean_pos": _pos(tg["mean"], tg["low"], tg["high"]), "price_pos": _pos(price, tg["low"], tg["high"]) if price else None,
                "price_out": bool(price and tg["high"] and (price < tg["low"] or price > tg["high"])),
            }
        recos = None
        if prof:
            raw = _json(prof["recos_json"]) or []
            n = sum(int(x.get("numberOfAnalysts") or 0) for x in raw)
            if n:
                avg = sum(float(x.get("ratingValue") or 0) * int(x.get("numberOfAnalysts") or 0) for x in raw) / n
                label, tone = next((lb, tn) for lim, lb, tn in _BANDS if avg <= lim)
                recos = {"n": n, "avg": round(avg, 2), "label": label, "tone": tone,
                         "bars": [{"name": x["ratingName"], "n": int(x.get("numberOfAnalysts") or 0), "pct": round(int(x.get("numberOfAnalysts") or 0) / n * 100, 1), "color": x.get("colorCode")} for x in raw]}
        est_rows, years = [], set()
        for m, label, unit, scale in _MEASURES:
            rows = c.execute("SELECT fiscal_year, mean, high, low, n_estimates FROM ia_estimate WHERE symbol=? AND measure=? AND freq='Annual' AND kind='estimate' ORDER BY fiscal_year", (symbol, m)).fetchall()
            if rows:
                years |= {r["fiscal_year"] for r in rows}
                est_rows.append({"label": label, "unit": unit, "by": {r["fiscal_year"]: r for r in rows}, "scale": scale, "dp": 2 if m == "EPS" else 0})
        years = sorted(years)
        estimates = None
        if est_rows:
            estimates = {"columns": [_fy(y) for y in years], "rows": [
                {"label": r["label"], "unit": r["unit"], "cells": [
                    ({"v": _money(r["by"][y]["mean"], r["scale"], r["dp"]), "sub": f"{r['by'][y]['n_estimates'] or 0} analysts · {_money(r['by'][y]['low'], r['scale'], r['dp'])}–{_money(r['by'][y]['high'], r['scale'], r['dp'])}"} if y in r["by"] else None)
                    for y in years]} for r in est_rows]}
        nextq = [{"label": f"{_QTR.get(r['fiscal_type'], r['fiscal_type'])} {_fy(r['fiscal_year'])}", "v": f"{r['mean']:.2f}", "n": r["n_estimates"], "lo": r["low"], "hi": r["high"]}
                 for r in c.execute("SELECT fiscal_type, fiscal_year, mean, high, low, n_estimates FROM ia_estimate WHERE symbol=? AND measure='EPS' AND freq='Interim' AND kind='estimate' ORDER BY fiscal_year, fiscal_type LIMIT 4", (symbol,))]
        surprises = [{"label": f"{_QTR.get(r['fiscal_type'], r['fiscal_type'])} {_fy(r['fiscal_year'])}", "actual": r["reported"], "surprise": r["surprise_pct"], "n": r["n_estimates"]}
                     for r in c.execute("SELECT fiscal_type, fiscal_year, reported, surprise_pct, n_estimates FROM ia_estimate WHERE symbol=? AND measure='EPS' AND freq='Interim' AND kind='actual' AND surprise_pct IS NOT NULL ORDER BY fiscal_year DESC, fiscal_type DESC LIMIT 6", (symbol,))][::-1]
        if not (target or recos or estimates or nextq):
            return None
        return {"target": target, "recos": recos, "estimates": estimates, "nextq": nextq, "surprises": surprises,
                "risk": {"label": prof["risk_category"], "std": prof["risk_std"]} if prof and prof["risk_category"] else None}
    finally:
        c.close()


def company_panel(symbol: str):
    c = _db()
    if c is None:
        return None
    try:
        p = c.execute("SELECT * FROM ia_profile WHERE symbol=?", (symbol,)).fetchone()
        if p is None:
            return None
        officers = []
        for o in (_json(p["officers_json"]) or [])[:6]:
            name = " ".join(x for x in (o.get("firstName"), o.get("lastName")) if x)
            title = (o.get("title") or {}).get("Value") or ""
            officers.append({"name": name, "title": title, "age": o.get("age"), "since": o.get("since")})
        names = {r["ia_name"]: r["symbol"] for r in c.execute("SELECT symbol, ia_name FROM ia_stock WHERE ia_name IS NOT NULL")}
        peers = [{"name": r["peer_name"], "symbol": names.get(r["peer_name"]), "pe": r["pe"], "pb": r["pb"], "mcap": r["market_cap"], "chg": r["pct_change"]}
                 for r in c.execute("SELECT * FROM ia_peer WHERE symbol=? ORDER BY market_cap DESC", (symbol,))]
        groups = []
        titles = {"valuation": "Valuation", "margins": "Margins", "growth": "Growth", "mgmtEffectiveness": "Management effectiveness", "financialstrength": "Financial strength",
                  "incomeStatement": "Income statement", "persharedata": "Per-share data", "priceandVolume": "Price & volume"}
        for k, items in (_json(p["key_metrics_json"]) or {}).items():
            rows = [{"name": i.get("displayName") or i.get("key"), "value": i.get("value")} for i in items if i.get("value") not in (None, "")]
            if rows:
                groups.append({"title": titles.get(k, k), "rows": rows})
        ca = _json(p["corporate_actions_json"]) or {}
        today = date.today().isoformat()
        meetings = sorted(ca.get("boardMeetings") or [], key=lambda x: x.get("boardMeetDate") or "")
        upcoming = [m for m in meetings if (m.get("boardMeetDate") or "") >= today][:3] or meetings[-3:]
        actions = {
            "dividends": [{"date": d.get("xdDate") or d.get("recordDate"), "text": d.get("remarks")} for d in sorted(ca.get("dividend") or [], key=lambda x: x.get("xdDate") or "", reverse=True)[:5]],
            "other": [{"kind": k.title(), "date": d.get("xbDate") or d.get("recordDate") or d.get("dateOfAnnouncement"), "text": d.get("remarks")} for k in ("splits", "bonus", "rights") for d in (ca.get(k) or [])[:3]],
            "meetings": [{"date": m.get("boardMeetDate"), "text": m.get("purpose")} for m in upcoming],
        }
        news = [{"headline": n.get("headline"), "date": (n.get("date") or "")[:10]} for n in (_json(p["news_json"]) or [])[:6]]
        growth = {}
        for r in c.execute("SELECT metric, horizon, pct FROM ia_growth WHERE symbol=?", (symbol,)):
            growth.setdefault(r["metric"], {})[r["horizon"]] = r["pct"]
        ma = [{"days": r["days"], "price": r["nse_price"] or r["bse_price"]} for r in c.execute("SELECT days, nse_price, bse_price FROM ia_technical WHERE symbol=? ORDER BY days", (symbol,))]
        return {"description": p["description"], "industry": p["industry"], "isin": p["isin"], "officers": officers, "peers": peers, "metrics": groups,
                "actions": actions, "news": news, "growth": growth, "ma": ma}
    finally:
        c.close()
