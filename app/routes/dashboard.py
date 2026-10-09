"""The in-app Home (/home): where a signed-in user lands.

One page for two kinds of people - someone still researching and building a portfolio, and someone running one. It adapts to what the
user has tracked: nothing -> search plus "build your portfolio"; a watchlist -> how each stock stands against THEIR rules; positions ->
what needs attention today, holdings by urgency, what's coming up. Search is always at the top, so research is never pushed down.
Everything shown comes from the user's own rows, live prices and the latest stored analysis; nothing here calls an AI model."""
import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import framework as fw
from app.ai.insights_engine import FX_INR_PER_USD, live_scan
from app.db import get_market_conn, get_users_conn
from app.templates_config import templates

router = APIRouter()

SEVERITY = {"EXIT": 0, "alert": 1, "TRIM": 2, "warning": 3, "BUY": 4, "watch": 5}


def _inr(v: float) -> str:
    if v >= 1e7:
        return f"₹{v / 1e7:,.2f} Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:,.2f} L"
    return f"₹{v:,.0f}"


def _scan_counts(u, user_id: int, symbol: str):
    try:
        s = live_scan(u, user_id, symbol)
    except Exception:  # noqa: BLE001 - one stock's scan must never break Home
        return None
    if not s:
        return None
    checked = [f for f in s["filters"] if f["status"] in ("pass", "fail")]
    return {"passed": sum(1 for f in checked if f["status"] == "pass"), "total": len(checked),
            "failing": [f["label"] for f in checked if f["status"] == "fail"]} if checked else None


def _action_text(a: dict, weight, size) -> str:
    v = a.get("verdict")
    if v == "EXIT":
        return "Exit the position"
    if v == "TRIM":
        return f"Trim from {weight:.1f}% toward {size['target']:g}% of your book" if weight else "Trim part of the position"
    if v == "BUY":
        return "Consider adding, within your sizing limits"
    return ""


@router.get("/home", response_class=HTMLResponse)
def app_home(request: Request):
    user_id = request.state.user_id
    user = request.state.user
    name = (user["display_name"] if user and "display_name" in user.keys() and user["display_name"] else "") or ""  # never the email: it reads badly as a greeting
    with get_users_conn() as u:
        rows = u.execute(
            """SELECT tp.*, (SELECT analysis_json FROM position_snapshots s WHERE s.position_id = tp.id AND s.analysis_json IS NOT NULL ORDER BY s.id DESC LIMIT 1) AS aj,
                      (SELECT MAX(checked_at) FROM position_snapshots s WHERE s.position_id = tp.id AND s.analysis_json IS NOT NULL) AS analysed_at
               FROM tracked_positions tp WHERE tp.user_id = ? AND tp.status = 'active' ORDER BY tp.anchor_type, tp.symbol""", (user_id,)).fetchall()
        sizing = fw.sizing_plan(u, user_id)
        size = {"max": sizing["size_max"], "target": sizing["size_target"], "watch": sizing["size_watch"]}
        scans = {r["symbol"]: _scan_counts(u, user_id, r["symbol"]) for r in rows[:40]}

    with get_market_conn() as m:
        last = {}
        for r in rows:
            p = m.execute("SELECT close FROM price_history WHERE symbol = ? ORDER BY date DESC LIMIT 1", (r["symbol"],)).fetchone()
            last[r["symbol"]] = p["close"] if p else None
        available = m.execute(
            """SELECT s.symbol, s.name FROM stocks s WHERE s.symbol IN ('NSE:RELIANCE','NSE:TCS','NSE:HDFCBANK','NSE:INFY','NSE:ITC','NSE:TITAN','NASDAQ:AAPL','NASDAQ:MSFT')
               ORDER BY (s.market = 'IN') DESC, s.symbol""").fetchall()

    fx = lambda sym: 1.0 if sym.startswith("NSE:") else FX_INR_PER_USD
    positions = [r for r in rows if r["anchor_type"] == "position"]
    watch = [r for r in rows if r["anchor_type"] == "watchlist"]
    value = cost = 0.0
    for r in positions:
        px = last.get(r["symbol"])
        if px and r["quantity"]:
            value += r["quantity"] * px * fx(r["symbol"])
            if r["buy_price"]:
                cost += r["quantity"] * r["buy_price"] * fx(r["symbol"])

    def build(r):
        a = json.loads(r["aj"]) if r["aj"] else None
        px = last.get(r["symbol"])
        weight = round(r["quantity"] * px * fx(r["symbol"]) / value * 100, 1) if (r["anchor_type"] == "position" and r["quantity"] and px and value) else None
        ret = round((px / r["buy_price"] - 1) * 100, 1) if (px and r["buy_price"]) else None
        # position-size flags are recomputed below from the live weight; the stored one reflects the day the analysis ran
        flags = [f for f in ((a or {}).get("story_flags") or []) if f["level"] in ("watch", "warning", "alert") and f.get("kind") != "size"]
        scan = scans.get(r["symbol"])
        return {"id": r["id"], "symbol": r["symbol"], "name": r["company_name"], "type": r["anchor_type"], "weight": weight, "ret": ret,
                "scan": scan, "verdict": (a or {}).get("verdict"), "flags": flags, "analysed_at": r["analysed_at"], "analysis": a}

    items = [build(r) for r in rows]
    # What needs attention: one row per stock, ranked by its most urgent reason
    attention = []
    for it in items:
        a, reasons = it["analysis"], []
        if a and a.get("verdict") in ("EXIT", "TRIM", "BUY"):
            txt = _action_text(a, it["weight"], size)
            if txt:
                reasons.append((SEVERITY[a["verdict"]], txt))
        for f in it["flags"]:
            reasons.append((SEVERITY[f["level"]], f["text"]))
        if it["weight"] and it["weight"] > size["max"]:
            reasons.append((SEVERITY["alert"], f"{it['weight']:.1f}% of your book, above your {size['max']:g}% limit"))
        elif it["weight"] and it["weight"] > size["watch"]:
            reasons.append((SEVERITY["watch"], f"{it['weight']:.1f}% of your book, over your {size['watch']:g}% yellow-flag line"))
        if reasons:
            reasons.sort(key=lambda x: x[0])
            attention.append({**it, "top": reasons[0][1], "rank": reasons[0][0], "more": len(reasons) - 1})
    attention.sort(key=lambda x: (x["rank"], -(x["weight"] or 0), x["symbol"]))

    # Coming up: next board meeting / results for tracked stocks (priority stocks only - the data comes from the company reference set)
    from app.ia_panels import company_panel
    upcoming = []
    for it in items[:40]:
        c = company_panel(it["symbol"]) if it["symbol"].startswith("NSE:") else None
        ne = c and c.get("next_event")
        if ne and 0 <= ne["days"] <= 45:
            upcoming.append({"symbol": it["symbol"], "id": it["id"], "name": it["name"], "date": ne["date"][:10], "days": ne["days"],
                             "what": "Results" if "esult" in (ne["text"] or "") else "Board meeting"})
    upcoming.sort(key=lambda x: x["days"])

    held = sorted([i for i in items if i["type"] == "position"], key=lambda x: (x.get("weight") is None, -(x.get("weight") or 0)))
    watching = sorted([i for i in items if i["type"] == "watchlist"], key=lambda x: -((x["scan"] or {}).get("passed", -1)))
    state = "holdings" if positions else ("watch" if watch else "new")
    strip = None
    if positions:
        strip = {"value": _inr(value), "ret": round((value / cost - 1) * 100, 1) if cost else None, "n": len(positions), "need": len(attention)}
    return templates.TemplateResponse("app_home.html", {
        "request": request, "active_nav": "home", "state": state, "name": name.split(" ")[0].title() if name else "", "strip": strip,
        "attention": attention[:6], "attention_total": len(attention), "held": held, "watching": watching, "upcoming": upcoming[:5], "size": size,
        "available": available,
    })
