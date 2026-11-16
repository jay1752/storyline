"""Returns and ledger: what the user's money actually earned, from the trades in the ledger (app/ledger.py, app/portfolio_returns.py).

The trades come from statement imports and from the journal. This page also lets the user record a past trade of a stock they no longer hold (a sale
before they started tracking, say), and delete a trade that was entered wrongly. A stock they still hold is changed through its journal, so the position
and the ledger cannot drift apart."""

import csv
import io
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import ledger as L
from app import portfolio_returns as PR
from app.db import get_users_conn
from app.routes.portfolio import _fmt_money, _fmt_pct
from app.templates_config import templates

router = APIRouter()


def _cell(v):
    """A text cell in a CSV that a spreadsheet will not run as a formula."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@router.get("/portfolio/returns", response_class=HTMLResponse)
def returns_page(request: Request, saved: str | None = None, error: str | None = None):
    user_id = request.state.user_id
    with get_users_conn() as u:
        a = PR.assemble(u, user_id)
        trades = list(reversed(L.load_trades(u, user_id)))[:200]
        total_trades = u.execute("SELECT COUNT(*) FROM ledger_trades WHERE user_id = ?", (user_id,)).fetchone()[0]
    return templates.TemplateResponse("returns.html", {
        "request": request, "active_nav": "portfolio", "a": a, "trades": trades, "total_trades": total_trades, "saved": saved, "error": error,
        "money": _fmt_money, "pct": _fmt_pct, "bench": PR.BENCH, "cur": PR.CUR, "today": date.today().isoformat()})


@router.post("/portfolio/returns/trade")
def add_past_trade(request: Request, symbol: str = Form(""), side: str = Form(""), when: str = Form(""), qty: str = Form(""), price: str = Form("")):
    def back(msg: str | None = None, ok: bool = False):
        import urllib.parse
        return RedirectResponse("/portfolio/returns?" + ("saved=1" if ok else "error=" + urllib.parse.quote(msg or "")), status_code=303)

    symbol = symbol.strip()
    try:
        d = date.fromisoformat(when.strip())
        q, p = float(qty), float(price)
    except ValueError:
        return back("Enter a valid date, quantity and price.")
    if side not in ("buy", "sell") or q <= 0 or p <= 0:
        return back("Choose buy or sell, and enter a quantity and price above zero.")
    if d > date.today():
        return back("That date is in the future.")
    with get_users_conn() as u:
        if not u.execute("SELECT 1 FROM market.stocks WHERE symbol = ?", (symbol,)).fetchone():
            return back("We do not know that symbol. Use the form NSE:TCS or NASDAQ:AAPL.")
        if u.execute("SELECT 1 FROM tracked_positions WHERE user_id = ? AND symbol = ? AND status = 'active' AND anchor_type = 'position'", (request.state.user_id, symbol)).fetchone():
            return back("You still hold that stock. Record buys and sales for it in its journal, so your position and your ledger stay together.")
        L.add_trade(u, request.state.user_id, symbol, side, q, p, d.isoformat(), "manual")
        u.commit()
    return back(ok=True)


@router.post("/portfolio/returns/trade/delete")
def delete_trade(request: Request, trade_id: int = Form(...)):
    import urllib.parse
    with get_users_conn() as u:
        row = u.execute("SELECT source FROM ledger_trades WHERE id = ? AND user_id = ?", (trade_id, request.state.user_id)).fetchone()
        if not row:
            return RedirectResponse("/portfolio/returns?error=" + urllib.parse.quote("That trade was not found."), status_code=303)
        if row["source"] == "journal":
            return RedirectResponse("/portfolio/returns?error=" + urllib.parse.quote("That trade comes from a journal entry. Undo the entry on the stock's page instead."), status_code=303)
        u.execute("DELETE FROM ledger_trades WHERE id = ? AND user_id = ?", (trade_id, request.state.user_id))
        u.commit()
    return RedirectResponse("/portfolio/returns?saved=1", status_code=303)


def _csv(rows: list[list], filename: str) -> Response:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/portfolio/returns/export/trades.csv")
def export_trades(request: Request):
    with get_users_conn() as u:
        rows = L.load_trades(u, request.state.user_id)
    return _csv([["date", "symbol", "side", "quantity", "price", "source", "estimated"]] +
                [[r["date"], _cell(r["symbol"]), r["side"], r["qty"], r["price"], _cell(r["source"]), "yes" if r["estimated"] else "no"] for r in rows], "tenetfolio-trades.csv")


@router.get("/portfolio/returns/export/realised.csv")
def export_realised(request: Request):
    with get_users_conn() as u:
        a = PR.assemble(u, request.state.user_id)
    out = [["financial_year", "symbol", "market", "bought", "sold", "quantity", "buy_price", "sell_price", "cost", "proceeds", "gain", "days_held", "term", "estimated_cost_date"]]
    for mkt, b in a["books"].items():
        for leg in sorted(b["legs"], key=lambda x: x["sell_date"]):
            out.append([L.financial_year(leg["sell_date"]), _cell(leg["symbol"]), mkt, leg["buy_date"], leg["sell_date"], round(leg["qty"], 6), round(leg["buy_price"], 4),
                        round(leg["sell_price"], 4), round(leg["cost"], 2), round(leg["proceeds"], 2), round(leg["gain"], 2), leg["days"], leg["term"], "yes" if leg["estimated"] else "no"])
    return _csv(out, "tenetfolio-realised-gains.csv")
