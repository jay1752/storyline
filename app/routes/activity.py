"""The 'still here' ping from the page (see app/activity.py)."""
from fastapi import APIRouter, Request, Response

from app import activity

router = APIRouter()


@router.post("/activity/beat")
async def beat(request: Request):
    uid = request.state.user_id
    if uid is not None:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - a malformed ping is simply ignored
            body = {}
        activity.beat(uid, str(body.get("area", "other"))[:20])
    return Response(status_code=204)
