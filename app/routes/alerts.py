"""Alerts pages: the list, mark-as-read, the email preferences (saved from the Profile page) and the public unsubscribe link (app/alerts.py)."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import alerts
from app.db import get_users_conn
from app.templates_config import templates

router = APIRouter()


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request):
    user_id = request.state.user_id
    with get_users_conn() as u:
        items = alerts.recent(u, user_id, 100)
        prefs = alerts.get_prefs(u, user_id)
        unread = {a["id"] for a in items if not a["read_at"]}
        alerts.mark_read(u, user_id)   # seen now; the page still shows which were new
    return templates.TemplateResponse("alerts.html", {"request": request, "active_nav": "management", "items": items, "unread": unread, "prefs": prefs,
                                                      "days": alerts.DAYS, "line": alerts.LINE})


@router.post("/alerts/read")
def alerts_read(request: Request):
    with get_users_conn() as u:
        alerts.mark_read(u, request.state.user_id)
    return RedirectResponse("/management", status_code=303)


@router.post("/alerts/prefs")
def alerts_prefs(request: Request, digest_email: str = Form(""), digest_day: int = Form(6), exit_email: str = Form(""), results_alerts: str = Form("")):
    with get_users_conn() as u:
        alerts.set_prefs(u, request.state.user_id, digest_email=bool(digest_email), digest_day=digest_day,
                         exit_email=bool(exit_email), results_alerts=bool(results_alerts))
    return RedirectResponse("/profile?saved=1#alerts", status_code=303)


@router.get("/alerts/unsubscribe", response_class=HTMLResponse)
def unsubscribe_page(request: Request, token: str = ""):
    """Shows a confirm button rather than acting on the GET, so a mail scanner opening the link cannot unsubscribe anyone by accident."""
    return templates.TemplateResponse("alerts_unsubscribe.html", {"request": request, "active_nav": "", "token": token, "done": False})


@router.post("/alerts/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_do(request: Request):
    """Both the page's button and a mail client's one-click unsubscribe (token in the query string) land here. No sign-in: the token is the proof."""
    token = request.query_params.get("token", "")
    if not token:
        try:
            token = str((await request.form()).get("token", ""))
        except Exception:  # noqa: BLE001
            token = ""
    with get_users_conn() as u:
        ok = alerts.unsubscribe(u, token)
    return templates.TemplateResponse("alerts_unsubscribe.html", {"request": request, "active_nav": "", "token": "", "done": True, "ok": ok})
