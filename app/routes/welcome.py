"""Getting-started page: where a brand-new account lands after its first sign-in (reachable any time at /welcome)."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import activity
from app.db import get_users_conn
from app.templates_config import templates

router = APIRouter()


@router.get("/welcome", response_class=HTMLResponse)
def welcome(request: Request):
    with get_users_conn() as u:
        me = u.execute("SELECT display_name FROM users WHERE id = ?", (request.state.user_id,)).fetchone()
        u.execute("UPDATE users SET welcomed_at = COALESCE(welcomed_at, CURRENT_TIMESTAMP) WHERE id = ?", (request.state.user_id,))
        u.commit()
    activity.log(request.state.user_id, "step", "welcome_viewed")
    first = ((me["display_name"] or "").split() or [""])[0] if me else ""
    return templates.TemplateResponse("welcome.html", {"request": request, "active_nav": "welcome", "first_name": first})
