"""What the assistant knows about the screen the user is on.

Each page gets a compact text digest built from the same data the page shows (not scraped from it): the Analytics stock page gets the
stock's fundamentals, the user's rules verdict, annual figures, analyst view and next result date; Management gets every tracked stock with
its latest story; Portfolio gets holdings, weights and concentration; Community gets the latest daily briefing; Rules gets the user's
thresholds. The chat route passes the digest as per-run instructions (fresh every turn, never stored in the conversation history), and the
same builders back the agent's tools so a question about ANY stock or about another page can still be answered.

Everything is read-only and fails soft: a digest that cannot be built is None and the assistant falls back to its tools."""
import json
import logging
from datetime import date, timedelta

from app.db import get_market_conn, get_users_conn

logger = logging.getLogger(__name__)


def _n(v, places=1, suffix=""):
    if v is None:
        return "n/a"
    if not isinstance(v, (int, float)):  # a few columns hold text (e.g. a pledge flag)
        return str(v)
    return f"{v:,.{places}f}{suffix}"


def _is_annual(r) -> bool:
    return "annual" in (r["quarter_label"] or "").lower()


def _last_price(m, symbol):
    r = m.execute("SELECT date, close FROM price_history WHERE symbol = ? ORDER BY date DESC LIMIT 1", (symbol,)).fetchone()
    return (r["close"], r["date"]) if r else (None, None)


# ---------------------------------------------------------------------------------------------- one stock

def stock_digest(user_id: int | None, symbol: str) -> str | None:
    from app import framework as fr
    from app.ai.checklist import evaluate_framework
    from app.annual import annual_rows
    from app.routes.analytics import _yoy, _next_result

    with get_market_conn() as m:
        stock = m.execute("SELECT * FROM stocks WHERE symbol = ?", (symbol,)).fetchone()
        if not stock:
            return None
        rows = [r for r in m.execute("SELECT * FROM quarterly_data WHERE symbol = ? ORDER BY period_end_date", (symbol,)).fetchall() if not _is_annual(r)]
        annual = annual_rows(m, symbol)
        met = m.execute("SELECT * FROM stock_metrics WHERE symbol = ?", (symbol,)).fetchone()
        price, price_date = _last_price(m, symbol)
    cur = "₹" if stock["market"] == "IN" else "$"
    unit = "Cr" if stock["market"] == "IN" else "M"
    out = [f"STOCK {symbol} · {stock['name']} · sector {stock['sector'] or 'unclassified'}" + (f" · {met['cap_tier']} cap" if met and met["cap_tier"] else "")]
    px = f"Last price {cur}{_n(price, 2)} ({price_date})" if price else "No price on file"
    if met and met["pe"]:
        px += f" · P/E {_n(met['pe'])} (own 3-year range {_n(met['pe_low'])}–{_n(met['pe_high'])})"
    out.append(px)

    # rules verdict, against THIS user's thresholds
    try:
        with get_users_conn() as u:
            th = fr.resolve(u, user_id, symbol) if user_id else None
        by_end = {r["period_end_date"]: r for r in rows}
        q8 = []
        for r in rows[-8:]:
            d = dict(r)
            if d.get("revenue_growth_pct") is None:
                d["revenue_growth_pct"] = _yoy(r, by_end, stored=False)
            q8.append(d)
        fw = evaluate_framework(q8, stock["sector"], th, annual=annual)
        lines = []
        for f in fw["filters"]:
            if f["status"] in ("pass", "fail"):
                lines.append(f"  {f['label']}: {f['value']} vs {f['threshold']} -> {f['status'].upper()}")
            elif f["status"] == "unknown":
                lines.append(f"  {f['label']}: not enough data")
        if lines:
            ok = sum(1 for f in fw["filters"] if f["status"] == "pass")
            ev = sum(1 for f in fw["filters"] if f["status"] in ("pass", "fail"))
            out.append(f"USER'S INVESTMENT RULES: {ok} of {ev} pass\n" + "\n".join(lines))
    except Exception as e:  # noqa: BLE001
        logger.info("stock_digest verdict skipped: %s", e)

    if rows:
        out.append(f"QUARTERLY ({cur} {unit}; oldest first): period | revenue | YoY% | OPM% | net profit | EPS | promoter% | pledge% | FII% | DII%")
        by_end = {r["period_end_date"]: r for r in rows}
        for r in rows[-8:]:
            g = r["revenue_growth_pct"] if r["revenue_growth_pct"] is not None else _yoy(r, by_end, stored=False)
            out.append(f"  {r['period_end_date']} | {_n(r['revenue_cr'], 0)} | {_n(g)} | {_n(r['opm_pct'])} | {_n(r['net_profit_cr'], 0)} | {_n(r['eps'], 2)} | "
                       f"{_n(r['promoter_pct'])} | {_n(r['promoter_pledge_pct'] if r['promoter_pledge_pct'] is not None else r['promoter_pledge'])} | {_n(r['fii_pct'])} | {_n(r['dii_pct'])}")
        last = rows[-1]
        bits = [f"{lbl} {_n(last[k], 2)}" for lbl, k in (("ROCE%", "roce_pct"), ("ROE%", "roe_pct"), ("debt/equity", "debt_equity")) if last[k] is not None]
        if bits:
            out.append("Latest ratios: " + ", ".join(bits))
    if annual:
        out.append(f"ANNUAL ({cur} {unit}): year | revenue | OPM% | net profit | EPS | operating cash flow | free cash flow | borrowings | ROCE%")
        for r in annual[-6:]:
            g = lambda k: r.get(k) if hasattr(r, "get") else r[k]
            out.append(f"  FY{g('period_end_date')[:4]} | {_n(g('revenue_cr'), 0)} | {_n(g('opm_pct'))} | {_n(g('net_profit_cr'), 0)} | {_n(g('eps'), 2)} | "
                       f"{_n(g('cfo_cr'), 0)} | {_n(g('fcf_cr'), 0)} | {_n(g('borrowings_cr'), 0)} | {_n(g('roce_pct'))}")
    if met:
        extra = [f"{lbl} {_n(met[k], 1, suf)}" for lbl, k, suf in (("sales CAGR 3y", "sales_cagr_3y", "%"), ("avg ROCE 3y", "roce_3y", "%"), ("dividend payout", "div_payout", "%")) if met[k] is not None]
        if extra:
            out.append("Longer view: " + ", ".join(extra))

    if stock["market"] == "IN":
        try:
            from app.routes.analytics import _delivery_block
            with get_market_conn() as m2:
                dv = _delivery_block(m2, symbol)
                ac = m2.execute("SELECT items_json FROM announcements_cache WHERE symbol = ?", (symbol,)).fetchone()
            if dv:
                out.append(f"DELIVERY % (NSE; share of traded shares actually delivered): latest {dv['latest']}% ({dv['date']}), 20-day avg {dv['avg20']}%"
                           + (f", 1-year avg {dv['avg250']}%" if dv.get("avg250") is not None else ""))
            if ac:
                its = json.loads(ac["items_json"])[:6]
                if its:
                    out.append("RECENT FILINGS (BSE): " + " | ".join(f"{i['at'][:10]} {i['title'][:90]}" + (f" — {i['summary'][:120]}" if i.get("summary") else "") for i in its))
        except Exception as e:  # noqa: BLE001
            logger.info("stock_digest delivery/filings skipped: %s", e)
        try:
            from app.ia_panels import analyst_panel, company_panel
            a = analyst_panel(symbol, price)
            if a:
                t, rc = a.get("target"), a.get("recos")
                if t:
                    out.append(f"ANALYSTS: mean target {cur}{_n(t['mean'], 0)} ({_n(t['upside'], 1, '%')} vs price; range {_n(t['low'], 0)}–{_n(t['high'], 0)}; {t['n']} analysts)" + (f"; target moved {_n(t['drift'], 1, '%')} in 90 days" if t.get("drift") is not None else ""))
                if rc:
                    out.append(f"  Consensus: {rc['label']} ({rc['n']} analysts)")
                if a.get("estimates"):
                    out.append("  Estimates " + ", ".join(a["estimates"]["columns"]) + ": " + "; ".join(f"{r['label']} " + "/".join((c["v"] if c else "-") for c in r["cells"]) for r in a["estimates"]["rows"]))
            c = company_panel(symbol)
            nr = _next_result(symbol, c)
            if nr:
                when = "just announced, data updating" if nr["updating"] else (f"in {nr['days']} days" if nr["days"] > 1 else ("tomorrow" if nr["days"] == 1 else "today"))
                out.append(f"NEXT {nr['label'].upper()}: {nr['date']} ({when})")
            if c and c.get("description"):
                out.append("ABOUT: " + c["description"][:350])
            if c and c.get("peers"):
                out.append("PEERS (P/E, P/B): " + "; ".join(f"{p['name']} {_n(p['pe'])}/{_n(p['pb'])}" for p in c["peers"][:5]))
        except Exception as e:  # noqa: BLE001
            logger.info("stock_digest extras skipped: %s", e)
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------- user's rules

def rules_digest(user_id: int) -> str | None:
    from app import framework as fr

    try:
        with get_users_conn() as u:
            th = fr.resolve(u, user_id)
            sz = fr.sizing_plan(u, user_id)
            sp = fr.story_plan(u, user_id)
    except Exception as e:  # noqa: BLE001
        logger.info("rules_digest failed: %s", e)
        return None
    lines = [f"  {r['label']} {r['op']} {r['value']}{r.get('unit') or ''}" + ("" if r.get("enabled", True) else " (switched off)") for r in th.values()]
    size = ", ".join(f"{d['label']}: {sz[d['key']]}{d.get('unit') or ''}" for d in fr.SIZING_DEFS if d["key"] in sz)
    return ("USER'S INVESTMENT RULES (their own thresholds)\n" + "\n".join(lines) + f"\nPosition sizing: {size}\n"
            f"Daily story runs for {'no stocks automatically' if sp['scope'] == 0 else 'positions only' if sp['scope'] == 1 else 'positions and watchlist'}, up to {sp['max']} stocks a day.")


# ---------------------------------------------------------------------------------------------- management

def management_digest(user_id: int) -> str | None:
    with get_users_conn() as u:
        rows = u.execute(
            """SELECT tp.id, tp.symbol, tp.company_name, tp.anchor_type, tp.anchor_date, tp.buy_price, tp.quantity,
                      ps.signal, ps.valuation_label, ps.analysis_json, ps.checked_at
               FROM tracked_positions tp JOIN position_snapshots ps ON ps.position_id = tp.id
                AND ps.id = (SELECT MAX(p2.id) FROM position_snapshots p2 WHERE p2.position_id = tp.id)
               WHERE tp.user_id = ? AND tp.status = 'active' ORDER BY tp.anchor_type, tp.symbol""", (user_id,)).fetchall()
    if not rows:
        return "The user tracks no stocks yet (Management page is empty)."
    out = [f"USER'S TRACKED STOCKS ({len(rows)}): positions first, then watchlist. Each: symbol, name, held qty @ avg, return since tracking / last price, latest story"]
    with get_market_conn() as m:
        for r in rows:
            price, _ = _last_price(m, r["symbol"])
            base = r["buy_price"]
            ret = f"{(price / base - 1) * 100:+.1f}%" if price and base else "n/a"
            line = f"- {r['symbol']} ({r['company_name']}) · {r['anchor_type']}"
            if r["quantity"]:
                line += f" · {r['quantity']:g} @ {_n(base, 2)}"
            line += f" · return {ret}" + (f" · last {_n(price, 2)}" if price else "")
            if r["valuation_label"]:
                line += f" · valuation {r['valuation_label']}"
            try:
                a = json.loads(r["analysis_json"]) if r["analysis_json"] else None
            except ValueError:
                a = None
            if a:
                flags = [f"{f.get('level')}: {f.get('text') or f.get('title') or f.get('label')}" for f in (a.get("story_flags") or []) if f.get("level") in ("warning", "alert")][:3]
                line += f" · story: {a.get('verdict') or r['signal'] or '-'} ({a.get('bucket') or '-'}, {(a.get('generated_at') or r['checked_at'] or '')[:10]})"
                if a.get("what_changed"):
                    line += f" — {a['what_changed'][:220]}"
                if flags:
                    line += " · FLAGS " + "; ".join(str(f)[:120] for f in flags)
            else:
                line += " · no story yet"
            out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------- portfolio

def portfolio_digest(user_id: int) -> str | None:
    from app import fx as fx_mod
    from app import framework as fr

    FX = fx_mod.usd_inr()["rate"]
    with get_users_conn() as u:
        pos = u.execute(
            """SELECT tp.symbol, tp.company_name, tp.quantity, tp.buy_price, tp.anchor_date, s.sector, s.market
               FROM tracked_positions tp JOIN market.stocks s ON s.symbol = tp.symbol
               WHERE tp.user_id = ? AND tp.anchor_type = 'position' AND tp.status = 'active'""", (user_id,)).fetchall()
        sz = fr.sizing_plan(u, user_id)
    hs = []
    with get_market_conn() as m:
        for r in pos:
            price, pdate = _last_price(m, r["symbol"])
            if not price or not r["quantity"]:
                continue
            val = r["quantity"] * price * (FX if r["market"] == "US" else 1.0)
            inv = r["quantity"] * r["buy_price"] * (FX if r["market"] == "US" else 1.0) if r["buy_price"] else None
            hs.append({"s": r["symbol"], "sector": r["sector"] or "Unclassified", "mk": r["market"], "q": r["quantity"], "avg": r["buy_price"], "px": price, "val": val, "inv": inv})
    if not hs:
        return "The user has no holdings with quantity and price yet (Portfolio page is empty)."
    tot = sum(h["val"] for h in hs)
    inv_tot = sum(h["inv"] for h in hs if h["inv"])
    hs.sort(key=lambda h: -h["val"])
    out = [f"USER'S PORTFOLIO: {len(hs)} holdings, total value ₹{tot:,.0f}" + (f", invested ₹{inv_tot:,.0f}, overall {((tot / inv_tot) - 1) * 100:+.1f}%" if inv_tot else "") + f" (US stocks converted at ₹{FX:.2f}/$)",
           "symbol | weight% | qty @ avg | last | P&L% | sector"]
    for h in hs:
        pl = f"{(h['px'] / h['avg'] - 1) * 100:+.1f}" if h["avg"] else "n/a"
        out.append(f"- {h['s']} | {h['val'] / tot * 100:.1f} | {h['q']:g} @ {_n(h['avg'], 2)} | {_n(h['px'], 2)} | {pl} | {h['sector']}")
    sec: dict[str, float] = {}
    for h in hs:
        sec[h["sector"]] = sec.get(h["sector"], 0) + h["val"]
    out.append("SECTORS: " + ", ".join(f"{k} {v / tot * 100:.0f}%" for k, v in sorted(sec.items(), key=lambda kv: -kv[1])[:8]))
    out.append(f"User's sizing limits: largest position {sz.get('size_max')}%, sector cap {sz.get('sector_max')}%. Over the position limit: "
               + (", ".join(h["s"] for h in hs if h["val"] / tot * 100 > sz.get("size_max", 100)) or "none") + ". Sectors over the cap: "
               + (", ".join(k for k, v in sec.items() if v / tot * 100 > sz.get("sector_max", 100)) or "none") + ".")
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------- community

def briefing_digest(day: str | None = None, limit: int = 4500) -> str | None:
    with get_users_conn() as u:
        q = "SELECT briefing_date, body FROM community_posts WHERE kind='ai_daily' AND deleted=0"
        row = u.execute(q + (" AND briefing_date = ?" if day else "") + " ORDER BY briefing_date DESC LIMIT 1", (day,) if day else ()).fetchone()
    if not row:
        return None
    return f"COMMUNITY DAILY AI BRIEFING for {row['briefing_date']}:\n{row['body'][:limit]}"


# ---------------------------------------------------------------------------------------------- routing

PAGE_NAMES = {"analytics": "Analytics", "community": "Community", "management": "Management", "portfolio": "Portfolio", "rules": "Investment rules",
              "home": "Home", "settings": "Settings", "admin": "Admin"}


def page_instructions(user_id: int | None, page: str | None, symbol: str | None) -> str:
    """Per-run instructions for the chat: which screen the user is on, plus that screen's data."""
    if not page:
        return ""
    head = f"The user is on the {PAGE_NAMES.get(page, page)} page" + (f" for {symbol}" if symbol else "") + ". Use the data below to answer questions about this screen; it is current as of this message."
    body = None
    try:
        if page == "analytics" and symbol:
            body = stock_digest(user_id, symbol)
        elif page == "analytics":
            body = "This is the stock discovery page: guided goals, screens judged against the user's own rules, and search. Offer to find stocks or explain a screen.\n" + (rules_digest(user_id) or "")
        elif page == "management" and user_id:
            body = management_digest(user_id)
        elif page == "portfolio" and user_id:
            body = portfolio_digest(user_id)
        elif page == "community":
            body = briefing_digest()
        elif page == "rules" and user_id:
            body = rules_digest(user_id)
    except Exception as e:  # noqa: BLE001 - never let a digest break the chat
        logger.warning("page context failed (%s): %s", page, e)
    return head + ("\n\n" + body if body else "")


# ---------------------------------------------------------------------------------------------- suggestions

def suggestions(user_id: int | None, page: str | None, symbol: str | None) -> dict:
    """Context-aware starter questions (derived from the page's own data), with static fallbacks."""
    imp = {"label": "Import positions from a statement", "attach": True}
    try:
        if page == "analytics" and symbol:
            items = ["Summarise this stock in plain words", "Which of my rules does it fail, and why?"]
            with get_market_conn() as m:
                c = m.execute("SELECT event_date FROM results_calendar WHERE symbol=? AND status='upcoming' AND event_date >= date('now') ORDER BY event_date LIMIT 1", (symbol,)).fetchone()
            if c:
                items.append("What should I watch in the upcoming results?")
            else:
                items.append("How have margins and growth trended?")
            items.append("Is it cheap or expensive against its own history?")
            return {"title": symbol.split(":")[-1], "sub": "I can read this stock's numbers, your rules and the analyst view.", "items": [{"label": x} for x in items]}
        if page == "management" and user_id:
            with get_users_conn() as u:
                n = u.execute("SELECT COUNT(*) FROM tracked_positions WHERE user_id=? AND status='active'", (user_id,)).fetchone()[0]
            if not n:
                return {"title": "Your tracked stocks", "sub": "Nothing tracked yet.", "items": [imp, {"label": "How do I start tracking a stock?"}]}
            return {"title": "Your tracked stocks", "sub": "I can see every stock you track and its latest story.",
                    "items": [{"label": "Which stocks need my attention this week?"}, {"label": "Which stock has the strongest story right now?"}, {"label": "What changed since the last quarter?"}, imp]}
        if page == "portfolio" and user_id:
            return {"title": "Your portfolio", "sub": "I can see your holdings, weights and sector mix.",
                    "items": [{"label": "Am I too concentrated anywhere?"}, {"label": "Which holding is my biggest risk?"}, {"label": "What is driving my returns?"}, imp]}
        if page == "community":
            return {"title": "Today's briefing", "sub": "I've read the latest AI market briefing.",
                    "items": [{"label": "Give me the 3 key takeaways"}, {"label": "Does anything here affect my holdings?"}, {"label": "What should I watch tomorrow?"}]}
        if page == "rules":
            return {"title": "Your investment rules", "sub": "I can see your thresholds and sizing limits.",
                    "items": [{"label": "Are my rules too strict? How many stocks pass?"}, {"label": "Explain what each rule protects me from"}, {"label": "Which of my holdings break these rules?"}]}
        if page == "analytics":
            return {"title": "Find a stock", "sub": "Tell me what you want and I'll point you to it.",
                    "items": [{"label": "Which stocks are steady compounders?"}, {"label": "Explain ROCE in simple words"}, {"label": "How do I find undervalued quality stocks?"}]}
    except Exception as e:  # noqa: BLE001
        logger.info("suggestions fallback: %s", e)
    return {"title": "How can I help?", "sub": "Open a stock or your portfolio and I'll work from that screen.",
            "items": [imp, {"label": "What's on my watchlist?"}, {"label": "Explain a ratio or concept"}]}
