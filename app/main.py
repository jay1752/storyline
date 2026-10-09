import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import seed as seed_module
from app.auth.middleware import AuthMiddleware
from app.db import ensure_platform_admin, get_market_conn, get_users_conn, init_db
from app.ingestion.runner import start_worker
from app.scheduler import start_scheduler
from app.routes import admin, analytics, auth, chat, community, framework, google_auth, home, management, portfolio, profile, search, settings
from app.tracking import already_tracked, create_tracked_position

app = FastAPI(title="Stocktale")
app.add_middleware(AuthMiddleware)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if os.environ.get("COOKIE_SECURE", "false").lower() == "true":  # only meaningful (and safe) once served over HTTPS
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(google_auth.router)
app.include_router(framework.router)
app.include_router(profile.router)
app.include_router(home.router)
app.include_router(analytics.router)
app.include_router(management.router)
app.include_router(portfolio.router)
app.include_router(admin.router)
app.include_router(search.router)
app.include_router(settings.router)
app.include_router(chat.router)
app.include_router(community.router)

@app.on_event("startup")
def on_startup():
    init_db()
    with get_market_conn() as m:
        count = m.execute("SELECT COUNT(*) c FROM stocks").fetchone()["c"]
    if count == 0:
        seed_module.seed()
    ensure_platform_admin()  # a fresh install's first account (created by the seed) becomes the platform admin

    start_worker()  # single ingestion worker — every triggered job queues through this, never runs concurrently with another

    start_scheduler()  # in-process: posts the daily Community briefing once per IST day (see app/scheduler.py)


@app.post("/track", response_class=HTMLResponse)
async def track(request: Request, symbol: str = Form(...), company_name: str = Form(...), anchor_type: str = Form(...)):
    """Quick one-click track from the Analytics page — anchor date defaults to today,
    no buy price/quantity captured. For a real buy date/price, use /management/add or
    the chat statement-import flow, both of which use the same create_tracked_position()."""
    user_id = request.state.user_id
    with get_users_conn() as u:
        if already_tracked(u, user_id, symbol):
            return '<span class="section-note">Already tracked — see <a href="/management">Management</a></span>'
        create_tracked_position(u, user_id, symbol, company_name, anchor_type)
        u.commit()

    return (
        '<span class="badge pos">Tracking started</span> '
        '<span class="section-note">— see it in <a href="/management">Management</a></span>'
    )
