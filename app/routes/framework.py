"""Investment rules: a page for the user's defaults (and, for a platform admin, the platform defaults), plus the
per-stock overrides edited from a stock's story page. Every save/reset is a small JSON call; see app/framework.py for
the layering (built-in < platform < user < stock) and what "reset" returns to."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import framework as fw
from app.db import get_users_conn
from app.templates_config import templates

router = APIRouter()


def _err(msg: str, code: int = 400):
    return JSONResponse({"error": msg}, status_code=code)


@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    user_id = request.state.user_id
    with get_users_conn() as u:
        mine = fw.layers(u, user_id)
        platform = fw.platform_layers(u) if request.state.is_admin else None
        stocks = fw.stocks_with_overrides(u, user_id)
        story_mine = fw.story_layers(u, user_id)
        story_platform = fw.story_platform_layers(u) if request.state.is_admin else None
    return templates.TemplateResponse("rules.html", {
        "request": request, "active_nav": "rules", "defs": fw.DEFS, "mine": mine, "platform": platform, "stocks": stocks,
        "story_defs": fw.STORY_DEFS, "story_mine": story_mine, "story_platform": story_platform})


async def _payload(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}


@router.post("/rules/save")
async def rules_save(request: Request):
    d = await _payload(request)
    scope = d.get("scope")
    if scope not in ("user", "platform"):
        return _err("Unknown scope.")
    if scope == "platform" and not request.state.is_admin:
        return _err("Only a platform admin can change the platform defaults.", 403)
    try:
        with get_users_conn() as u:
            fw.save(u, scope, request.state.user_id, "", d.get("key"), d.get("value"), bool(d.get("enabled", True)))
            u.commit()
    except fw.RuleError as e:
        return _err(str(e))
    return {"ok": True}


@router.post("/rules/reset")
async def rules_reset(request: Request):
    d = await _payload(request)
    scope = d.get("scope")
    if scope not in ("user", "platform"):
        return _err("Unknown scope.")
    if scope == "platform" and not request.state.is_admin:
        return _err("Only a platform admin can change the platform defaults.", 403)
    with get_users_conn() as u:
        n = fw.reset(u, scope, request.state.user_id, "", d.get("key") or None)
        u.commit()
    return {"ok": True, "cleared": n}


def _own_symbol(u, user_id: int, position_id: int):
    row = u.execute("SELECT symbol FROM tracked_positions WHERE id = ? AND user_id = ?", (position_id, user_id)).fetchone()
    return row["symbol"] if row else None


@router.post("/management/{position_id:int}/rules/save")
async def stock_rules_save(request: Request, position_id: int):
    d = await _payload(request)
    try:
        with get_users_conn() as u:
            sym = _own_symbol(u, request.state.user_id, position_id)
            if not sym:
                return _err("Stock not found.", 404)
            fw.save(u, "stock", request.state.user_id, sym, d.get("key"), d.get("value"), bool(d.get("enabled", True)))
            u.commit()
    except fw.RuleError as e:
        return _err(str(e))
    return {"ok": True}


@router.post("/management/{position_id:int}/rules/reset")
async def stock_rules_reset(request: Request, position_id: int):
    d = await _payload(request)
    with get_users_conn() as u:
        sym = _own_symbol(u, request.state.user_id, position_id)
        if not sym:
            return _err("Stock not found.", 404)
        n = fw.reset(u, "stock", request.state.user_id, sym, d.get("key") or None)
        u.commit()
    return {"ok": True, "cleared": n}
