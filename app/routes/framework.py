"""Investment rules: a page for the user's defaults (and, for a platform admin, the platform defaults), plus the
per-stock overrides edited from a stock's story page. Every save/reset is a small JSON call; see app/framework.py for
the layering (built-in < platform < user < stock) and what "reset" returns to."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import activity, framework as fw
from app.db import get_users_conn
from app.templates_config import templates

router = APIRouter()


def _err(msg: str, code: int = 400):
    return JSONResponse({"error": msg}, status_code=code)


@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, start: int = 0, set: int = 0):
    user_id = request.state.user_id
    with get_users_conn() as u:
        sets = fw.list_sets(u, user_id)
        edit_set = fw.owned_set(u, user_id, set) if set else None
        set_rows = fw.set_layers(u, user_id, edit_set["id"]) if edit_set else None
        for x in sets:   # how many rules each screener changes from your own, for its card
            x["differs"] = sum(1 for v in fw.set_layers(u, user_id, x["id"]).values() if v["customised"])
        mine = fw.layers(u, user_id)
        platform = fw.platform_layers(u) if request.state.is_admin else None
        stocks = fw.stocks_with_overrides(u, user_id)
        story_mine = fw.story_layers(u, user_id)
        story_platform = fw.story_platform_layers(u) if request.state.is_admin else None
    return templates.TemplateResponse("rules.html", {"start": bool(start),
        "request": request, "active_nav": "rules", "defs": fw.DEFS, "mine": mine, "platform": platform, "stocks": stocks,
        "story_defs": fw.STORY_DEFS, "sizing_defs": fw.SIZING_DEFS, "story_mine": story_mine, "story_platform": story_platform,
        "sets": sets, "edit_set": edit_set, "set_rows": set_rows, "max_sets": fw.MAX_SETS,
        "groups": fw.GROUPS, "by_key": fw.BY_KEY, "story_by_key": fw.STORY_BY_KEY})


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
    if scope == "user":
        activity.log(request.state.user_id, "step", "rules_changed")
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


# ---- screeners: named copies of the scan thresholds, picked for Analytics, a stock page or a Deep research run

def _owned(u, request: Request, d: dict):
    return fw.owned_set(u, request.state.user_id, d.get("set_id"))


@router.post("/rules/set/create")
async def set_create(request: Request):
    d = await _payload(request)
    try:
        with get_users_conn() as u:
            sid = fw.create_set(u, request.state.user_id, d.get("name"))
            u.commit()
    except fw.RuleError as e:
        return _err(str(e))
    activity.log(request.state.user_id, "step", "rules_changed")
    return {"ok": True, "id": sid}


@router.post("/rules/set/rename")
async def set_rename(request: Request):
    d = await _payload(request)
    try:
        with get_users_conn() as u:
            if not _owned(u, request, d):
                return _err("Screener not found.", 404)
            fw.rename_set(u, request.state.user_id, int(d["set_id"]), d.get("name"))
            u.commit()
    except fw.RuleError as e:
        return _err(str(e))
    return {"ok": True}


@router.post("/rules/set/delete")
async def set_delete(request: Request):
    d = await _payload(request)
    with get_users_conn() as u:
        if not _owned(u, request, d):
            return _err("Screener not found.", 404)
        fw.delete_set(u, request.state.user_id, int(d["set_id"]))
        u.commit()
    return {"ok": True}


@router.post("/rules/set/save")
async def set_save(request: Request):
    d = await _payload(request)
    try:
        with get_users_conn() as u:
            if not _owned(u, request, d):
                return _err("Screener not found.", 404)
            fw.save_set_value(u, int(d["set_id"]), d.get("key"), d.get("value"), bool(d.get("enabled", True)))
            u.commit()
    except fw.RuleError as e:
        return _err(str(e))
    return {"ok": True}


@router.post("/rules/set/reset")
async def set_reset(request: Request):
    d = await _payload(request)
    with get_users_conn() as u:
        if not _owned(u, request, d):
            return _err("Screener not found.", 404)
        n = fw.reset_set_value(u, request.state.user_id, int(d["set_id"]), d.get("key") or None)
        u.commit()
    return {"ok": True, "cleared": n}


@router.post("/rules/set/assign")
async def set_assign(request: Request):
    """Make one tracked stock follow a screener (set_id), or go back to the user's own rules (set_id null)."""
    d = await _payload(request)
    sym = (d.get("symbol") or "").strip()
    with get_users_conn() as u:
        if not u.execute("SELECT 1 FROM tracked_positions WHERE user_id = ? AND symbol = ? AND status = 'active'", (request.state.user_id, sym)).fetchone():
            return _err("That stock isn't in your tracked list.", 404)
        sid = None
        if d.get("set_id") not in (None, "", 0):
            if not _owned(u, request, d):
                return _err("Screener not found.", 404)
            sid = int(d["set_id"])
        fw.assign(u, request.state.user_id, sym, sid)
        u.commit()
    return {"ok": True}
