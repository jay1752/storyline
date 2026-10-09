"""One middleware gates every route — chosen over per-route Depends() specifically
because retrofitting a dependency into every route signature across 7 existing files
(all of which currently hardcode MOCK_USER_ID = 1) is more invasive than one function.
Each route then reads request.state.user_id instead of the old constant.

Also publishes the resolved user_id on a contextvar (current_user_id) — app/ai/agent.py's
tools are called BY the pydantic_ai agent loop, not directly by FastAPI, so they have no
Request object to read request.state from. A contextvar set here, at the top of every
request, is readable from anywhere in that request's call stack without threading a
parameter through PydanticAI's own tool-calling machinery."""

import contextvars

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from app.auth.tokens import get_session_user

SESSION_COOKIE = "session_id"

# Paths (or prefixes) reachable with no session at all.
PUBLIC_PATHS = {"/", "/auth/google", "/auth/google/callback", "/login", "/signup", "/logout", "/forgot-password", "/reset-password", "/verify-email"}
PUBLIC_PREFIXES = ("/static/",)

current_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_user_id", default=None)


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)


def _is_admin_path(path: str) -> bool:
    """The whole /admin tree: the dashboard, its live-search partials, and every job trigger (ingestion, Insights,
    the community briefing)."""
    return path == "/admin" or path.startswith("/admin/")


def _forbidden(request: Request):
    """403 for a signed-in user who isn't a platform admin — an HTML page for browsing, JSON for everything else.
    Deliberately says nothing about what is behind the door."""
    if request.method == "GET" and "text/html" in request.headers.get("accept", "text/html"):
        from app.templates_config import templates

        return templates.TemplateResponse("forbidden.html", {"request": request, "active_nav": ""}, status_code=403)
    return JSONResponse({"error": "Forbidden."}, status_code=403)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        session_id = request.cookies.get(SESSION_COOKIE)
        user = get_session_user(session_id) if session_id else None

        request.state.user = user
        request.state.user_id = user["id"] if user else None
        # Platform admin = users.is_platform_admin = 1. Drives both the nav (hidden for everyone else) and the hard
        # gate below, so hiding the link is never the only protection.
        request.state.is_admin = bool(user and user["is_platform_admin"])
        token = current_user_id.set(user["id"] if user else None)
        try:
            if user is None and not _is_public(request.url.path):
                # /chat/stream can't usefully follow an HTML redirect mid-stream — it
                # gets a JSON/SSE error frame instead, handled in app/routes/chat.py
                # itself (which checks request.state.user_id), not here.
                if request.url.path == "/chat/stream":
                    return await call_next(request)
                return RedirectResponse("/login", status_code=303)
            if _is_admin_path(request.url.path) and not request.state.is_admin:
                return _forbidden(request)
            return await call_next(request)
        finally:
            current_user_id.reset(token)
