import json
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import journal as journal_mod
from app.ai.insights_engine import queue_run
from app.db import get_market_conn, get_users_conn
from app.ingestion.runner import enqueue_insights, enqueue_position_analysis
from app.templates_config import templates
from app.tracking import already_tracked, create_tracked_position, nearest_price_on_or_before

router = APIRouter()


def _sparkline_points(prices, width=400, height=140, pad=20):
    if len(prices) < 2:
        return "", None, None
    values = [p["close"] for p in prices]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    n = len(values)
    step = (width - 2 * pad) / (n - 1)

    def y(v):
        return (height - pad) - ((v - lo) / span) * (height - 2 * pad)

    pts = " ".join(f"{pad + i * step:.1f},{y(v):.1f}" for i, v in enumerate(values))
    return pts, {"x": pad, "y": y(values[0]), "v": values[0]}, {"x": width - pad, "y": y(values[-1]), "v": values[-1]}


@router.get("/management", response_class=HTMLResponse)
def management_screen(request: Request, focus: int | None = None, imported: int | None = None, skipped: int | None = None, analyzing: int | None = None):
    user_id = request.state.user_id
    if focus:  # the old inline detail panel moved to the stock's own story page
        return RedirectResponse(f"/management/{focus}", status_code=303)
    with get_users_conn() as u:
        positions = u.execute(
            """
            SELECT tp.*, ps.since_anchor_pct, ps.one_yr_pct, ps.valuation_label, ps.signal,
                   ps.trigger_type, ps.checked_at, ps.reasoning_json, ps.analysis_json,
                   (SELECT MAX(s.checked_at) FROM position_snapshots s WHERE s.position_id = tp.id AND s.analysis_json IS NOT NULL) AS analysed_at,
                   (SELECT r.status FROM insight_runs r WHERE r.position_id = tp.id ORDER BY r.id DESC LIMIT 1) AS run_status,
                   (SELECT r.error FROM insight_runs r WHERE r.position_id = tp.id ORDER BY r.id DESC LIMIT 1) AS run_error
            FROM tracked_positions tp
            JOIN position_snapshots ps ON ps.position_id = tp.id
            -- Latest snapshot per position only: Insights writes a new row per stock per run, so an unfiltered join
            -- would return one row PER SNAPSHOT, not per position.
            AND ps.id = (SELECT MAX(ps2.id) FROM position_snapshots ps2 WHERE ps2.position_id = tp.id)
            WHERE tp.user_id = ? AND tp.status = 'active'
            ORDER BY tp.anchor_type, tp.symbol
            """,
            (user_id,),
        ).fetchall()
        settings = u.execute("SELECT auto_insights FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        closed = u.execute("SELECT * FROM tracked_positions WHERE user_id = ? AND status = 'exited' ORDER BY closed_at DESC", (user_id,)).fetchall()

    enriched = []
    for r in positions:
        d = dict(r)
        a = json.loads(d.pop("analysis_json")) if r["analysis_json"] else None
        flags = (a or {}).get("story_flags") or []
        d["bad_flags"] = sum(1 for f in flags if f["level"] in ("warning", "alert"))
        d["has_alert"] = any(f["level"] == "alert" for f in flags)
        enriched.append(d)
    positions = enriched

    counts = {
        "all": len(positions),
        "positions": sum(1 for p in positions if p["anchor_type"] == "position"),
        "watchlist": sum(1 for p in positions if p["anchor_type"] == "watchlist"),
    }
    return templates.TemplateResponse(
        "management.html",
        {
            "request": request,
            "active_nav": "management",
            "positions": positions,
            "closed": closed,
            "counts": counts,
            "imported": imported,
            "skipped": skipped,
            "analyzing": analyzing,
            "auto_insights": True if settings is None else bool(settings["auto_insights"]),
            "any_running": any(p["run_status"] in ("queued", "running") for p in positions),
        },
    )


def _entry(snap, older_analysis):
    """One timeline entry from a position_snapshots row; `older_analysis` is the previous analysed entry, for change flags."""
    a = json.loads(snap["analysis_json"]) if snap["analysis_json"] else None
    changed = []
    if a and older_analysis:
        if a["verdict"] != older_analysis["verdict"]:
            changed.append(f"Verdict {older_analysis['verdict']} → {a['verdict']}")
        if a["bucket"] != older_analysis["bucket"]:
            changed.append(f"Bucket {older_analysis['bucket']} → {a['bucket']}")
        if a["speed2"]["status"] != older_analysis["speed2"]["status"]:
            changed.append(f"Speed 2: {older_analysis['speed2']['status'].replace('_', ' ')} → {a['speed2']['status'].replace('_', ' ')}")
    return {
        "id": snap["id"], "at": snap["checked_at"], "analysis": a, "changed": changed,
        "signal": snap["signal"], "bucket": snap["valuation_label"],
        "legacy": [] if a else json.loads(snap["reasoning_json"] or "[]"),
        "trigger": snap["trigger_type"],
    }


@router.get("/management/{position_id:int}", response_class=HTMLResponse)
def management_story(request: Request, position_id: int, all: int = 0):
    """One tracked stock's running story: the latest framework verdict in full, then every earlier daily entry."""
    user_id = request.state.user_id
    with get_users_conn() as u:
        pos = u.execute("SELECT * FROM tracked_positions WHERE id = ? AND user_id = ? AND status IN ('active', 'exited')", (position_id, user_id)).fetchone()
        if not pos:
            return RedirectResponse("/management", status_code=303)
        stock = u.execute("SELECT name, sector, market FROM market.stocks WHERE symbol = ?", (pos["symbol"],)).fetchone()
        events = list(reversed(journal_mod.entries(u, position_id)))  # newest first for display
        limit = 400 if all else 30
        snaps = u.execute("SELECT * FROM position_snapshots WHERE position_id = ? ORDER BY id DESC LIMIT ?", (position_id, limit + 1)).fetchall()
        total = u.execute("SELECT COUNT(*) FROM position_snapshots WHERE position_id = ? AND analysis_json IS NOT NULL", (position_id,)).fetchone()[0]
        run = u.execute("SELECT * FROM insight_runs WHERE position_id = ? ORDER BY id DESC LIMIT 1", (position_id,)).fetchone()
    more = len(snaps) > limit
    snaps = snaps[:limit]
    entries, analysed = [], []
    for s in snaps:
        older = next((e["analysis"] for e in analysed_after(snaps, s)), None)
        entries.append(_entry(s, older))
    analysed = [e for e in entries if e["analysis"]]
    latest = analysed[0] if analysed else None
    with get_market_conn() as m:
        px = m.execute("SELECT date, close FROM price_history WHERE symbol = ? ORDER BY date DESC LIMIT 1", (pos["symbol"],)).fetchone()
    price = px["close"] if px else None
    cur = "₹" if (stock and stock["market"] == "IN") else "$"
    qty, avg = pos["quantity"], pos["buy_price"]
    live = {
        "cur": cur, "price": price, "price_date": px["date"] if px else None, "qty": qty, "avg": avg,
        "value": qty * price if qty and price else None,
        "ret_pct": (price / avg - 1) * 100 if price and avg else None,
        "exit_ret_pct": (pos["exit_price"] / avg - 1) * 100 if pos["exit_price"] and avg else None,
    }
    default_price = round(price, 2) if price else None
    return templates.TemplateResponse(
        "management_story.html",
        {
            "request": request, "active_nav": "management", "pos": pos, "stock": stock, "events": events, "live": live,
            "today": journal_mod.today_ist().isoformat(), "default_price": default_price, "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved"),
            "latest": latest, "history": [e for e in entries if e is not latest and (e["analysis"] or (e["legacy"] and not e["legacy"][0].startswith("Just added")))],
            "run": run, "running": bool(run and run["status"] in ("queued", "running")),
            "total_entries": total, "more": more,
        },
    )


def analysed_after(snaps, current):
    """Snapshots older than `current` (snaps is newest-first) that carry a full analysis, nearest first."""
    seen = False
    for s in snaps:
        if s["id"] == current["id"]:
            seen = True
            continue
        if seen and s["analysis_json"]:
            yield {"analysis": json.loads(s["analysis_json"])}


MANUAL_PER_DAY = 30
MANUAL_COOLDOWN_MIN = 2


@router.post("/management/{position_id:int}/analyze")
def management_analyze(request: Request, position_id: int):
    """Run the framework analysis for ONE tracked stock now. Returns immediately; the page polls /status."""
    user_id = request.state.user_id
    with get_users_conn() as u:
        if not u.execute("SELECT 1 FROM tracked_positions WHERE id = ? AND user_id = ? AND status = 'active'", (position_id, user_id)).fetchone():
            return JSONResponse({"error": "That stock isn't in your tracked list."}, status_code=404)
        recent = u.execute(
            "SELECT 1 FROM insight_runs WHERE position_id = ? AND status = 'success' AND finished_at > datetime('now', ?)",
            (position_id, f"-{MANUAL_COOLDOWN_MIN} minutes")).fetchone()
        today = u.execute("SELECT COUNT(*) FROM insight_runs WHERE user_id = ? AND trigger = 'manual' AND started_at > datetime('now', '-1 day')", (user_id,)).fetchone()[0]
    if recent:
        return JSONResponse({"error": "This stock was analysed a moment ago — give it a couple of minutes."}, status_code=429)
    if today >= MANUAL_PER_DAY:
        return JSONResponse({"error": f"You've used today's {MANUAL_PER_DAY} manual analyses. The daily job still runs automatically."}, status_code=429)
    run_id, created = queue_run(user_id, position_id, "manual")
    if created:
        enqueue_position_analysis(run_id)
    return {"run_id": run_id, "started": created}


@router.get("/management/{position_id:int}/status")
def management_status(request: Request, position_id: int):
    user_id = request.state.user_id
    with get_users_conn() as u:
        run = u.execute("SELECT r.id, r.status, r.error FROM insight_runs r JOIN tracked_positions tp ON tp.id = r.position_id "
                        "WHERE r.position_id = ? AND tp.user_id = ? ORDER BY r.id DESC LIMIT 1", (position_id, user_id)).fetchone()
    return {"run": dict(run) if run else None}


@router.post("/management/{position_id:int}/journal")
async def management_journal_add(request: Request, position_id: int):
    """Optional: log that you added more, trimmed, exited, or write a note. Applied to the position straight away."""
    from urllib.parse import quote

    user_id = request.state.user_id
    f = await request.form()
    with get_users_conn() as u:
        try:
            journal_mod.add_entry(u, user_id, position_id, (f.get("type") or "").strip(), (f.get("date") or "").strip(),
                                  f.get("quantity"), f.get("price"), f.get("note"))
            u.commit()
        except journal_mod.JournalError as e:
            return RedirectResponse(f"/management/{position_id}?error={quote(str(e))}#journal", status_code=303)
    return RedirectResponse(f"/management/{position_id}?saved=1#journal", status_code=303)


@router.post("/management/{position_id:int}/journal/undo")
def management_journal_undo(request: Request, position_id: int):
    with get_users_conn() as u:
        journal_mod.undo_last(u, request.state.user_id, position_id)
        u.commit()
    return RedirectResponse(f"/management/{position_id}#journal", status_code=303)


@router.post("/management/analyze-all")
def management_analyze_all(request: Request):
    """Analyse every tracked stock of this user now (the same job the daily schedule runs, ignoring 'analysed today')."""
    enqueue_insights(request.state.user_id, trigger="manual", skip_fresh_hours=0)
    return RedirectResponse("/management?analyzing=1", status_code=303)


@router.post("/management/auto-insights")
async def management_auto_insights(request: Request):
    from app.ai.providers import get_user_settings

    user_id = request.state.user_id
    on = 1 if (await request.form()).get("on") == "1" else 0
    s = get_user_settings(user_id)
    with get_users_conn() as u:
        u.execute(
            """INSERT INTO user_settings (user_id, provider, model_name, use_platform_default, auto_insights, updated_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(user_id) DO UPDATE SET auto_insights = excluded.auto_insights, updated_at = CURRENT_TIMESTAMP""",
            (user_id, s["provider"], s["model_name"], s["use_platform_default"], on),
        )
        u.commit()
    return RedirectResponse("/management", status_code=303)


@router.post("/management/{position_id}/delete")
def management_delete(request: Request, position_id: int):
    user_id = request.state.user_id
    with get_users_conn() as u:
        u.execute(
            "DELETE FROM position_snapshots WHERE position_id IN "
            "(SELECT id FROM tracked_positions WHERE id = ? AND user_id = ?)",
            (position_id, user_id),
        )
        u.execute("DELETE FROM insight_runs WHERE position_id = ? AND user_id = ?", (position_id, user_id))
        u.execute("DELETE FROM position_events WHERE position_id = ? AND user_id = ?", (position_id, user_id))
        u.execute("DELETE FROM tracked_positions WHERE id = ? AND user_id = ?", (position_id, user_id))
        u.commit()
    return RedirectResponse("/management", status_code=303)


@router.post("/management/delete-all")
def management_delete_all(request: Request):
    user_id = request.state.user_id
    with get_users_conn() as u:
        u.execute(
            "DELETE FROM position_snapshots WHERE position_id IN "
            "(SELECT id FROM tracked_positions WHERE user_id = ?)",
            (user_id,),
        )
        u.execute("DELETE FROM insight_runs WHERE user_id = ?", (user_id,))
        u.execute("DELETE FROM position_events WHERE user_id = ?", (user_id,))
        u.execute("DELETE FROM tracked_positions WHERE user_id = ?", (user_id,))
        u.commit()
    return RedirectResponse("/management", status_code=303)


# ---------- Add a position/watchlist entry manually ----------

@router.get("/management/add", response_class=HTMLResponse)
def management_add_form(request: Request):
    return templates.TemplateResponse(
        "management_add.html",
        {"request": request, "active_nav": "management", "form": {}, "today": date.today().isoformat(), "error": None},
    )


@router.get("/management/pick-stock", response_class=HTMLResponse)
def pick_stock(request: Request, q: str | None = None):
    """Same shape as /search-stocks, but results are pick buttons (not links) for use
    inside the track-a-stock forms — see app/static/js/tracking.js's selectStock()."""
    results = []
    if q and len(q.strip()) >= 1:
        like = f"%{q.strip()}%"
        with get_market_conn() as m:
            results = m.execute(
                """
                SELECT symbol, name FROM stocks
                WHERE symbol LIKE ? OR name LIKE ?
                ORDER BY (symbol LIKE ?) DESC, symbol
                LIMIT 12
                """,
                (like, like, f"%{q.strip().upper()}%"),
            ).fetchall()
    return templates.TemplateResponse("_stock_pick_results.html", {"request": request, "results": results, "q": q})


@router.get("/management/quote", response_class=HTMLResponse)
def quote_price(request: Request, symbol: str = "", date: str = "", field_id: str = "price"):
    """Suggests a buy price from our own price history — never authoritative, always
    just a starting value the user can overwrite in the same input."""
    value = ""
    if symbol and date:
        with get_market_conn() as m:
            price, _ = nearest_price_on_or_before(m, symbol, date)
        if price is not None:
            value = f"{price:.2f}"
    return templates.TemplateResponse("_price_input.html", {"request": request, "value": value, "field_id": field_id})


@router.post("/management/add", response_class=HTMLResponse)
async def management_add(request: Request):
    form_data = await request.form()
    symbol = (form_data.get("symbol") or "").strip()
    company_name = (form_data.get("company_name") or "").strip()
    anchor_type = (form_data.get("anchor_type") or "position").strip()
    anchor_date = (form_data.get("date") or "").strip() or date.today().isoformat()
    price_raw = (form_data.get("price") or "").strip()
    qty_raw = (form_data.get("quantity") or "").strip()

    form_echo = {
        "symbol": symbol, "company_name": company_name, "anchor_type": anchor_type,
        "date": anchor_date, "price": price_raw, "quantity": qty_raw,
    }

    if not symbol:
        return templates.TemplateResponse(
            "management_add.html",
            {"request": request, "active_nav": "management", "form": form_echo, "today": date.today().isoformat(),
             "error": "Pick a stock from the search results first."},
        )

    with get_market_conn() as m:
        exists = m.execute("SELECT 1 FROM stocks WHERE symbol = ?", (symbol,)).fetchone()
    if not exists:
        return templates.TemplateResponse(
            "management_add.html",
            {"request": request, "active_nav": "management", "form": form_echo, "today": date.today().isoformat(),
             "error": f'"{symbol}" isn\'t a symbol we track — pick one from the search results.'},
        )

    try:
        price = float(price_raw) if price_raw else None
    except ValueError:
        price = None
    try:
        quantity = float(qty_raw) if qty_raw else None
    except ValueError:
        quantity = None

    user_id = request.state.user_id
    with get_users_conn() as u:
        if already_tracked(u, user_id, symbol):
            return RedirectResponse("/management", status_code=303)
        create_tracked_position(u, user_id, symbol, company_name or symbol, anchor_type, anchor_date, price, quantity)
        u.commit()

    return RedirectResponse("/management", status_code=303)
