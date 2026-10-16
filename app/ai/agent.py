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
from app.price_fetch import ensure_prices
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

SYSTEM_PROMPT = """You are Tenetfolio's in-app assistant. You help the user with (1) the screen they are on, using the \
page data you are given and your tools, and (2) general investing questions and research.

Facts about this app's data (a stock's numbers, the user's holdings, their rules, the daily briefing, result dates) must come from the \
page data or a tool call, never from memory - numbers you'd recall are likely stale. If the data you have does not cover the question, \
say so and offer the nearest thing. For general questions (what a ratio means, how a business model works, how to think about valuation, \
comparing approaches) answer from your own knowledge, clearly separate it from the user's data, and note when something may be out of date. \
You have no live web access: for news after your knowledge, say you can't check.

Tools: get_stock_analytics (everything the Analytics page shows for any stock), get_my_rules, get_management_overview (the user's tracked \
stocks and their latest stories), get_portfolio_summary, get_community_briefing, get_stock_summary, get_price_history, search_stocks. \
Call a tool when the user asks about something other than the screen they are on. To judge any stock against the user's rules, use get_stock_analytics (it carries the pass/fail per rule); get_stock_summary is only a quick look. Speak to the user as \"you\" / \"your\", never \"the user\". Do not narrate before calling a tool; just call it, \
then answer.

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

ANSWER RULES.
- State every figure with its period and unit, and say which price date a valuation statement rests on. Keep facts (from the page data or a tool) apart from your own \
inference and label inference as such ("this suggests"). Never present a paraphrase as a quote.
- When the data does not cover the question, say what is missing in one line (for example: filing documents and call transcripts are not read here, the filings list shows \
titles only).
- You do not tell the user to buy, sell, add or trim, and you give no price target. If asked "should I buy or sell", say you cannot decide that for them, then give a \
valuation-versus-price read from THEIR rules, the stock's own history and its peers, plus the measurable things that would change that read. Analyst targets and ratings are \
other people's opinions: attribute them, and never present an analyst's upside as an expected gain.
- A foreign-listed stock: explain the business, financials, risks and valuation context only, with no verdict language.
- "Summarise this stock": one sentence on the business and the picture, then exactly 3 takeaways (one says what would change the view), then one line on what the data does \
not show.
- Judge a business by the drivers that fit its model (banks and lenders: return on assets, equity to assets, price to book; others: margins, cash conversion, working-capital \
days) and say which drivers we hold no figure for. Profit that does not turn into cash is worth saying plainly.
- If the page data shows the user has a Deep research report for this stock, say it exists and use its triggers and gaps.
- Never use em dashes or en dashes, hype or filler (not: it's important to note, deep dive, delve, landscape, robust, compelling, navigate, poised to, overall).

IMPORTING POSITIONS FROM A STATEMENT. When the user attaches a broker file (contract note, \
tradebook, holdings statement, CSV), your job is to TRANSCRIBE it, not to calculate. Read \
every row and call propose_positions once with ALL of them — never skip, merge, net off, or \
average rows yourself; the tool does that arithmetic exactly. For each row:
- company_text: the security's name/ticker exactly as printed.
- side: "buy" or "sell" for transactions; "holding" for a current-holdings snapshot row.
- quantity: shares traded, or shares currently held.
- price: the execution price for a transaction; for a holding, the AVERAGE BUY PRICE / cost \
basis — never the current market price, even when the statement prints both.
- isin: the ISIN printed for that security (e.g. INE002A01018) when the statement shows one — copy it \
exactly; it identifies the stock unambiguously.
- amount: the row's printed trade value (net of nothing — the plain quantity × price figure) when the \
statement shows one; the tool uses it to catch misread numbers.
- date: that row's own date (yyyy-mm-dd) if printed. Pass the document's own reference date \
as statement_date only if it's labelled as such — never a date-shaped number like a contract \
or account number. Skip cash, charges, taxes, and totals rows.
If propose_positions reports a name with NO match, try search_stocks; if that finds one clear \
answer, call propose_positions again with the `symbol` field set on those rows (resubmit the \
full list). If it reports an AMBIGUOUS name (several stocks tie), do NOT pick one yourself — \
list the options to the user and ask which they meant, then resubmit with their choice. A \
wrong stock in someone's portfolio is worse than one extra question. The tool checks every stock against real market prices on the trade dates and fetches prices \
from the internet if we have none; if the printed name matched the wrong stock it switches to the one the \
prices fit, and says so. Rows marked needs_review (low-confidence match, prices that don't fit, a quantity × \
price that disagrees with the printed amount, or no price data anywhere) must be called out to the user by \
name with the reason — never gloss over them. A confirm card appears for the user — they must click it; nothing is saved by you. \
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
    ensure_prices(symbol)  # stale or missing history is fetched from the internet now, not left blank
    with get_market_conn() as conn:
        rows = conn.execute(
            "SELECT date, close FROM price_history WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (symbol, days),
        ).fetchall()
    if not rows:
        return {"error": f"no price history for {symbol!r}, here or online"}
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
    isin: Optional[str] = None    # as printed on the statement, e.g. INE002A01018
    amount: Optional[float] = None  # the row's printed trade value (quantity × price), if shown


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
        positions_mod.Txn(r.company_text, r.side, r.quantity, r.price, r.date, r.symbol, r.isin, r.amount)
        for r in rows
    ]
    proposal = positions_mod.propose(user_id, txns, statement_date)
    created = proposals_created.get()
    if created is not None:
        created.append(proposal["proposal_id"])
    return proposal


@agent.tool_plain
def get_stock_analytics(symbol: str) -> str:
    """Everything the Analytics page shows for one stock: the user's rules verdict (pass/fail per rule), the last 8 quarters (revenue, growth,
    margin, profit, EPS, promoter/pledge/FII/DII), annual figures incl. cash flow, valuation, analyst targets and estimates, the next results
    date, a company description and peers. `symbol` is exchange-qualified, e.g. "NSE:INFY"; use search_stocks if unsure."""
    _note_tool("get_stock_analytics", symbol)
    from app.ai.page_context import stock_digest
    return stock_digest(current_user_id.get(), symbol) or f"no stock found for symbol {symbol!r}"


@agent.tool_plain
def get_my_rules() -> str:
    """The user's own investment-rule thresholds (ROCE, debt/equity, pledge, growth...), position-sizing limits and daily-story settings."""
    _note_tool("get_my_rules")
    from app.ai.page_context import rules_digest
    uid = current_user_id.get()
    return (rules_digest(uid) if uid else None) or "not available"


@agent.tool_plain
def get_management_overview() -> str:
    """Every stock the user tracks (positions and watchlist) with return, valuation label and the latest AI story verdict, summary and flags."""
    _note_tool("get_management_overview")
    from app.ai.page_context import management_digest
    uid = current_user_id.get()
    return (management_digest(uid) if uid else None) or "not available"


@agent.tool_plain
def get_portfolio_summary() -> str:
    """The user's portfolio: holdings with weights, P&L, sector mix, and whether any position or sector breaks their sizing limits."""
    _note_tool("get_portfolio_summary")
    from app.ai.page_context import portfolio_digest
    uid = current_user_id.get()
    return (portfolio_digest(uid) if uid else None) or "not available"


@agent.tool_plain
def get_community_briefing(date: Optional[str] = None) -> str:
    """The daily AI market briefing posted in Community. `date` is yyyy-mm-dd; omit for the latest."""
    _note_tool("get_community_briefing", date)
    from app.ai.page_context import briefing_digest
    return briefing_digest(date) or "no briefing found"
