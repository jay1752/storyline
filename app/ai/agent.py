"""The chat agent: one module-level Agent with a fixed tool set, built once at import
time. The model is NOT fixed here — pydantic_ai's run_stream()/run_sync() both accept a
`model=` override per call (verified against the real installed pydantic-ai-slim==2.54.0
API), so app/routes/chat.py and app/ai/insights_engine.py each resolve the right
model via app/ai/providers.py and pass it in per-call rather than building a new Agent
per request.

Tools deliberately take plain validated arguments (a symbol string), never raw user_id.
get_portfolio_or_watchlist needs to know WHICH user is asking, but a tool is called by
pydantic_ai's own agent loop, not directly by FastAPI, so it has no Request to read
request.state.user_id from — it reads app.auth.middleware's current_user_id contextvar
instead, set at the top of every request by AuthMiddleware.

Each tool is read-only and opens its own short-lived connection; no tool ever writes."""

import contextvars
from typing import Literal, Optional

from pydantic import BaseModel
from pydantic_ai import Agent

from app import positions as positions_mod
from app.auth.middleware import current_user_id
from app.db import get_market_conn, get_users_conn

# propose_positions appends the proposal ids it creates here so app/routes/chat.py can
# surface a confirm card after the run — tool results are internal to the agent loop and
# never reach the SSE stream on their own. The chat route sets a fresh list per request.
proposals_created: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar("proposals_created", default=None)

# Every tool call appends {"name", "arg"} here so the chat UI can show what an answer was
# grounded in ("Fundamentals · NSE:MCX") — same per-request-list trick as proposals_created.
tools_used: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar("tools_used", default=None)


def _note_tool(name: str, arg: str | None = None) -> None:
    used = tools_used.get()
    if used is not None:
        used.append({"name": name, "arg": arg})

SYSTEM_PROMPT = """You are Storyline's in-app assistant, answering questions about stocks \
and the user's own tracked positions/watchlist using the app's own data via your tools — \
never from general knowledge or memory, since numbers you'd recall are very likely stale.

The user's personal framework (not yours to invent) classifies holdings into buckets — \
Core, Steady, Emerging, Reserve, Trim/Exit — and watches for exits on three speeds: \
Speed 1 (immediate: auditor change, promoter pledge spike, regulatory action, fraud), \
Speed 2 (confirmed over 2 quarters: ROCE/sales/margin/debt deterioration), Speed 3 (slow, \
12-18 months: promoter holding decline, moat erosion). When discussing a holding, frame \
your answer in those terms if relevant, but always base the actual numbers on a tool call \
— don't restate thresholds from memory, since the exact numeric filters live in the \
daily Insights job, not in you.

If a tool returns an error or "not found," say so plainly rather than guessing a number. \
Keep answers tight — this is a chat panel, not a report.

IMPORTING POSITIONS FROM A STATEMENT. When the user attaches a broker file (contract note, \
tradebook, holdings statement, CSV), your job is to TRANSCRIBE it, not to calculate. Read \
every row and call propose_positions once with ALL of them — never skip, merge, net off, or \
average rows yourself; the tool does that arithmetic exactly. For each row:
- company_text: the security's name/ticker exactly as printed.
- side: "buy" or "sell" for transactions; "holding" for a current-holdings snapshot row.
- quantity: shares traded, or shares currently held.
- price: the execution price for a transaction; for a holding, the AVERAGE BUY PRICE / cost \
basis — never the current market price, even when the statement prints both.
- date: that row's own date (yyyy-mm-dd) if printed. Pass the document's own reference date \
as statement_date only if it's labelled as such — never a date-shaped number like a contract \
or account number. Skip cash, charges, taxes, and totals rows.
If propose_positions reports a name with NO match, try search_stocks; if that finds one clear \
answer, call propose_positions again with the `symbol` field set on those rows (resubmit the \
full list). If it reports an AMBIGUOUS name (several stocks tie), do NOT pick one yourself — \
list the options to the user and ask which they meant, then resubmit with their choice. A \
wrong stock in someone's portfolio is worse than one extra question. A confirm card appears for the user — they must click it; nothing is saved by you. \
Never say positions were added or updated. After the tool returns, give a short plain summary: \
how many positions, anything skipped and why, and any warnings."""

agent = Agent(model=None, system_prompt=SYSTEM_PROMPT)


@agent.tool_plain
def get_stock_summary(symbol: str) -> dict:
    """Latest reported fundamentals for one stock: revenue growth, margin, ROCE/ROE,
    debt/equity, promoter holding/pledge, and sector. Use this whenever the user asks
    about a specific stock's numbers. `symbol` must be exchange-qualified, e.g.
    "NSE:RELIANCE" or "NASDAQ:AAPL" — if unsure of the exact form, say so rather than
    guessing one."""
    _note_tool("get_stock_summary", symbol)
    with get_market_conn() as conn:
        stock = conn.execute("SELECT * FROM stocks WHERE symbol = ?", (symbol,)).fetchone()
        if not stock:
            return {"error": f"no stock found for symbol {symbol!r}"}
        latest = conn.execute(
            "SELECT * FROM quarterly_data WHERE symbol = ? ORDER BY period_end_date DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    if not latest:
        return {"error": f"{symbol} is in our universe but has no quarterly fundamentals yet"}
    return {
        "symbol": symbol,
        "name": stock["name"],
        "sector": stock["sector"],
        "period_end_date": latest["period_end_date"],
        "revenue_growth_pct": latest["revenue_growth_pct"],
        "opm_pct": latest["opm_pct"],
        "roce_pct": latest["roce_pct"],
        "roe_pct": latest["roe_pct"],
        "debt_equity": latest["debt_equity"],
        "promoter_pct": latest["promoter_pct"],
        "promoter_pledge": latest["promoter_pledge"],
        "guidance_vs_delivered": latest["guidance_vs_delivered"],
    }


@agent.tool_plain
def get_price_history(symbol: str, days: int = 90) -> dict:
    """Recent daily closing prices for one stock, most recent first. Use this for
    "how has the price moved" / "is it up or down" style questions."""
    _note_tool("get_price_history", symbol)
    with get_market_conn() as conn:
        rows = conn.execute(
            "SELECT date, close FROM price_history WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (symbol, days),
        ).fetchall()
    if not rows:
        return {"error": f"no price history for {symbol!r}"}
    return {"symbol": symbol, "prices": [{"date": r["date"], "close": r["close"]} for r in rows]}


@agent.tool_plain
def get_portfolio_or_watchlist(anchor_type: str | None = None) -> list[dict]:
    """The user's tracked positions and/or watchlist. `anchor_type` is "position"
    (actually bought), "watchlist" (tracking only, not bought), or omit it for both.
    Use this for any question about "my portfolio," "my watchlist," or "what am I
    tracking" — never guess what the user holds."""
    _note_tool("get_portfolio_or_watchlist", anchor_type)
    query = """
        SELECT tp.symbol, tp.company_name, tp.anchor_type, tp.anchor_date, tp.buy_price, tp.quantity,
               ps.signal, ps.valuation_label, ps.since_anchor_pct, ps.checked_at
        FROM tracked_positions tp
        JOIN position_snapshots ps ON ps.position_id = tp.id
        -- latest snapshot per position only — a position accumulates one new row per
        -- day once the daily Insights job is running, a plain JOIN would return every
        -- historical row for every position.
        AND ps.checked_at = (SELECT MAX(ps2.checked_at) FROM position_snapshots ps2 WHERE ps2.position_id = tp.id)
        WHERE tp.user_id = ? AND tp.status = 'active'
    """
    user_id = current_user_id.get()
    if user_id is None:
        return [{"error": "not logged in"}]
    params = [user_id]
    if anchor_type:
        query += " AND tp.anchor_type = ?"
        params.append(anchor_type)
    query += " ORDER BY tp.anchor_type, tp.symbol"

    with get_users_conn() as u:
        rows = u.execute(query, params).fetchall()
    return [dict(r) for r in rows]


@agent.tool_plain
def search_stocks(query: str) -> list[dict]:
    """Find stocks in our universe by ticker or company name — use this to resolve a name
    printed on a statement that propose_positions couldn't match. Returns up to 8
    {symbol, name} candidates, exchange-qualified (e.g. "NSE:RELIANCE")."""
    _note_tool("search_stocks", query.strip()[:40])
    q = query.strip()
    if not q:
        return []
    like = f"%{q}%"
    with get_market_conn() as conn:
        rows = conn.execute(
            "SELECT symbol, name FROM stocks WHERE symbol LIKE ? OR name LIKE ? "
            "ORDER BY (symbol LIKE 'NSE:%') DESC, (symbol LIKE ?) DESC, symbol LIMIT 8",
            (like, like, f"%{q.upper()}%"),
        ).fetchall()
    return [{"symbol": r["symbol"], "name": r["name"]} for r in rows]


class StatementRow(BaseModel):
    company_text: str
    side: Literal["buy", "sell", "holding"]
    quantity: Optional[float] = None
    price: Optional[float] = None
    date: Optional[str] = None
    symbol: Optional[str] = None  # only if already resolved via search_stocks


@agent.tool_plain
def propose_positions(rows: list[StatementRow], statement_date: Optional[str] = None) -> dict:
    """Turn the transcribed statement rows into proposed positions. Pass EVERY row from
    the document untouched — this tool nets buys/sells and computes quantity and
    weighted-average price exactly. It does NOT save anything: it stores a proposal the
    user must confirm with a button. Returns the proposal (positions, skipped rows with
    reasons, warnings)."""
    _note_tool("propose_positions", f"{len(rows)} rows")
    user_id = current_user_id.get()
    if user_id is None:
        return {"error": "not logged in"}
    txns = [
        positions_mod.Txn(r.company_text, r.side, r.quantity, r.price, r.date, r.symbol)
        for r in rows
    ]
    proposal = positions_mod.propose(user_id, txns, statement_date)
    created = proposals_created.get()
    if created is not None:
        created.append(proposal["proposal_id"])
    return proposal
