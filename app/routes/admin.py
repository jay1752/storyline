from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_market_conn, get_users_conn
from app.ingestion.runner import SOURCES, TARGETS, enqueue_community_briefing, enqueue_ingestion, enqueue_insights
from app import scheduler as sched
from app.templates_config import templates

router = APIRouter()


def _fetch_stocks(conn, q: Optional[str] = None, market: Optional[str] = None,
                   cap_tier: Optional[str] = None, cap: int = 100):
    """Shared by the dashboard's default view and the live-search/filter partial.
    Per stock, per layer: Layer 1 (company) and Layer 3 (institutional) are real
    depth (distinct quarters covered) — Layer 3's source differs by market (BSE
    shareholding enrichment on quarterly_data for India, sec_13f's own table for
    US), so which one a row reads is picked by that row's own market, not a global
    switch. Layers 2 and 4 are prerequisites, not stored facts (both are computed
    at analytics read-time) — a real sector / any price history at all. Layer 5
    is market-wide, not per-stock, so it isn't in this per-row query at all — see
    admin_dashboard's separate macro-status lookup.

    With no query/filter: symbols-with-data first, capped (universe can be
    10,000+ rows). Cap tier is India-only (see app/data/cap_tiers.json) — a US
    filter combined with a cap tier would just return nothing, so the caller is
    expected to not send both (the UI disables the cap select when market=US)."""
    where = []
    params: list = []
    if q:
        where.append("(s.symbol LIKE ? OR s.name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if market:
        where.append("s.market = ?")
        params.append(market)
    if cap_tier:
        where.append("s.cap_tier = ?")
        params.append(cap_tier)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # Two-step on purpose: ordering only needs "has any data?" (cheap EXISTS), while the
    # per-layer counts — COUNT over price_history is the expensive one — are computed
    # afterwards for just the `cap` rows shown. Ordering by the counts themselves forced a
    # full price_history scan for all ~15,000 stocks (~55s page load).
    return conn.execute(
        f"""
        SELECT t.symbol, t.name, t.market, t.cap_tier, t.sector,
               (SELECT COUNT(DISTINCT period_end_date) FROM quarterly_data q WHERE q.symbol = t.symbol) AS l1_quarters,
               (CASE WHEN t.sector IS NOT NULL AND t.sector != 'Unclassified' THEN 1 ELSE 0 END) AS l2_ok,
               (CASE WHEN t.market = 'US'
                     THEN (SELECT COUNT(DISTINCT period_end_date) FROM institutional_holdings i WHERE i.symbol = t.symbol)
                     ELSE (SELECT COUNT(DISTINCT period_end_date) FROM quarterly_data q WHERE q.symbol = t.symbol AND q.promoter_pct IS NOT NULL)
                END) AS l3_quarters,
               (SELECT COUNT(*) FROM price_history p WHERE p.symbol = t.symbol) AS l4_price_points
        FROM (
            SELECT s.symbol, s.name, s.market, s.cap_tier, s.sector
            FROM stocks s
            {where_sql}
            ORDER BY (EXISTS (SELECT 1 FROM quarterly_data q WHERE q.symbol = s.symbol)
                      OR EXISTS (SELECT 1 FROM price_history p WHERE p.symbol = s.symbol)) DESC, s.symbol
            LIMIT ?
        ) t
        """,
        (*params, cap),
    ).fetchall()


def _fetch_runs(conn, q: Optional[str] = None, cap: int = 25):
    if q:
        like = f"%{q}%"
        return conn.execute(
            "SELECT * FROM ingestion_runs WHERE source_name LIKE ? OR target LIKE ? OR status LIKE ? ORDER BY id DESC LIMIT ?",
            (like, like, like, cap),
        ).fetchall()
    return conn.execute("SELECT * FROM ingestion_runs ORDER BY id DESC LIMIT ?", (cap,)).fetchall()


TABS = [("ingestion", "Data ingestion"), ("users", "Users"), ("rules", "Platform rules")]


@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, tab: str = "ingestion"):
    """The Platform admin shell. Only the chosen tab is loaded (by htmx, when opened): the others cost nothing until they are clicked."""
    tab = tab if tab in dict(TABS) else "ingestion"
    return templates.TemplateResponse("admin.html", {"request": request, "active_nav": "admin", "tabs": TABS, "tab": tab})


@router.get("/admin/tab/ingestion", response_class=HTMLResponse)
def admin_tab_ingestion(request: Request):
    with get_market_conn() as m:
        runs = _fetch_runs(m)
        market_counts = {
            "stocks": m.execute("SELECT COUNT(*) c FROM stocks").fetchone()["c"],
            "quarterly_data": m.execute("SELECT COUNT(*) c FROM quarterly_data").fetchone()["c"],
            # MAX(rowid), not COUNT(*): counting ~60M price rows scans the whole index (~tens of seconds);
            # rows are only ever appended/upserted, so the high-water mark is the same number to within noise.
            "price_history": m.execute("SELECT COALESCE(MAX(rowid), 0) c FROM price_history").fetchone()["c"],
            "macro_context": m.execute("SELECT COUNT(*) c FROM macro_context").fetchone()["c"],
        }
        stocks = _fetch_stocks(m)
        stocks_shown = len(stocks)
        # Layer 5 is market-wide, not per-stock: a single fact per market, checked once here.
        macro_markets = {r["market"] for r in m.execute("SELECT DISTINCT market FROM macro_context").fetchall()}

    with get_users_conn() as u:
        user_counts = {
            "users": u.execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
            "tracked_positions": u.execute("SELECT COUNT(*) c FROM tracked_positions WHERE status='active'").fetchone()["c"],
            "position_snapshots": u.execute("SELECT COUNT(*) c FROM position_snapshots").fetchone()["c"],
        }

    return templates.TemplateResponse(
        "_admin_tab_ingestion.html",
        {"request": request, "runs": runs, "market_counts": market_counts, "user_counts": user_counts, "stocks": stocks, "stocks_shown": stocks_shown,
         "macro_markets": macro_markets, "sources": list(SOURCES.keys()), "targets": TARGETS, "jobs": sched.jobs_view()},
    )


@router.get("/admin/tab/rules", response_class=HTMLResponse)
def admin_tab_rules(request: Request):
    from app import framework as fw
    with get_users_conn() as u:
        ctx = {"defs": fw.DEFS, "platform": fw.platform_layers(u), "story_defs": fw.STORY_DEFS, "sizing_defs": fw.SIZING_DEFS, "story_platform": fw.story_platform_layers(u)}
    return templates.TemplateResponse("_admin_tab_rules.html", {"request": request, **ctx})


@router.get("/admin/tab/users", response_class=HTMLResponse)
def admin_tab_users(request: Request, days: int = 7, flt: str = "all", q: str = ""):
    from app import admin_users
    o = admin_users.overview(days if days in (1, 7, 30) else 7, flt if flt in ("all", "new", "returning", "stuck") else "all", q.strip()[:60])
    return templates.TemplateResponse("_admin_tab_users.html", {"request": request, "o": o})


@router.get("/admin/users/{uid:int}", response_class=HTMLResponse)
def admin_user_detail(request: Request, uid: int, reveal: int = 0):
    from app import admin_users
    d = admin_users.detail(uid, bool(reveal))
    if d is None:
        return HTMLResponse('<p class="section-note">That user no longer exists.</p>')
    return templates.TemplateResponse("_admin_user_detail.html", {"request": request, "d": d})


@router.get("/admin/stocks-rows", response_class=HTMLResponse)
def admin_stocks_rows(request: Request, q: Optional[str] = None, market: Optional[str] = None,
                       cap_tier: Optional[str] = None):
    with get_market_conn() as m:
        stocks = _fetch_stocks(m, q, market or None, cap_tier or None)
        macro_markets = {r["market"] for r in m.execute("SELECT DISTINCT market FROM macro_context").fetchall()}
    return templates.TemplateResponse(
        "_admin_stocks_rows.html", {"request": request, "stocks": stocks, "macro_markets": macro_markets}
    )


@router.get("/admin/runs-rows", response_class=HTMLResponse)
def admin_runs_rows(request: Request, q: Optional[str] = None):
    with get_market_conn() as m:
        runs = _fetch_runs(m, q)
    return templates.TemplateResponse("_admin_runs_rows.html", {"request": request, "runs": runs})


@router.post("/admin/run-ingestion")
def trigger_ingestion(
    source: str = Form(...),
    target: str = Form(...),
    limit: Optional[int] = Form(None),
    symbol: Optional[str] = Form(None),
    market: Optional[str] = Form(None),
):
    # Enqueues and returns immediately onto runner.py's bounded thread pool (see its
    # module docstring) — up to MAX_CONCURRENT_JOBS run at once.
    symbol = symbol.strip().upper() or None if symbol else None
    market = market.strip().upper() or None if market else None
    enqueue_ingestion(source, target, limit, symbol, market)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/run-insights")
def trigger_insights(request: Request):
    enqueue_insights(request.state.user_id, trigger="manual", skip_fresh_hours=0)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/run-community-briefing")
def trigger_community_briefing(request: Request, force: str = Form("")):
    """Generate today's Community briefing now (force=1 regenerates it in place if one already exists)."""
    enqueue_community_briefing(force=bool(force))
    return RedirectResponse("/admin", status_code=303)


# ---------- Scheduled jobs (in-app scheduler: app/scheduler.py) ----------

async def _json(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}


@router.post("/admin/jobs/{key}/save")
async def job_save(request: Request, key: str):
    from fastapi.responses import JSONResponse
    d = await _json(request)
    if key not in sched.JOBS:
        return JSONResponse({"error": "Unknown job."}, status_code=404)
    try:
        if sched.JOBS[key].get("every"):  # interval job: minutes between runs
            hm, days = int(d.get("every")), list(sched.ALL_DAYS)
        else:
            hm = sched.parse_hm(str(d.get("time", "")))
            days = [int(x) for x in d.get("days", [])]
        sched.save_job(key, bool(d.get("enabled", True)), hm, days)
    except (ValueError, AttributeError, TypeError):
        return JSONResponse({"error": "Enter a time like 19:00 and pick at least one weekday (or, for an hourly job, 5 to 1440 minutes)."}, status_code=400)
    return {"ok": True}


@router.post("/admin/jobs/{key}/reset")
def job_reset(key: str):
    from fastapi.responses import JSONResponse
    if key not in sched.JOBS:
        return JSONResponse({"error": "Unknown job."}, status_code=404)
    sched.reset_job(key)
    return {"ok": True}


@router.post("/admin/jobs/{key}/run")
def job_run(key: str):
    """Run a scheduled job right now, regardless of its time/days/pause state (a paused job still runs when asked)."""
    from fastapi.responses import JSONResponse
    if key not in sched.JOBS:
        return JSONResponse({"error": "Unknown job."}, status_code=404)
    last = sched.last_runs([key])[key]["last"]
    if last and last["status"] in ("queued", "running"):
        return JSONResponse({"error": "That job is already running."}, status_code=409)
    if key == "community_briefing":
        enqueue_community_briefing(force=False)
        return {"ok": True, "started": "Community briefing"}
    if key == "insights":
        from app.ai.providers import build_chain
        with get_users_conn() as u:
            users = [r[0] for r in u.execute("SELECT DISTINCT user_id FROM tracked_positions WHERE status = 'active'")]
        n = 0
        for uid in users:
            if build_chain(uid):
                enqueue_insights(uid, trigger="manual")
                n += 1
        if not n:
            return JSONResponse({"error": "No user has tracked stocks and a usable AI model right now."}, status_code=400)
        return {"ok": True, "started": f"analysis for {n} user(s)"}
    enqueue_ingestion(sched.JOBS[key]["source"], "prices")
    return {"ok": True, "started": sched.JOBS[key]["label"]}
