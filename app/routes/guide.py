"""Quick start: the ten-step walkthrough (sign in -> AI key -> rules -> own screener -> find -> read -> track -> story -> deep research -> position), reachable at /guide and
offered as a dismissible card on the Stocks tab. Progress is read from what the user has actually done, never from checkboxes."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import activity
from app.db import get_users_conn
from app.templates_config import templates

router = APIRouter()

# key, title, short line for the card, where it happens
STEPS = [
    ("signin", "Sign in", "/analytics"),
    ("aikey", "Add your AI key", "/settings"),
    ("rules", "Set your rules", "/rules"),
    ("myscreener", "Make a screener", "/rules#screeners"),
    ("find", "Find stocks", "/analytics"),
    ("analyse", "Read a stock", "/analytics"),
    ("track", "Track it", "/management"),
    ("story", "Check the story", "/management"),
    ("deep", "Go deeper", "/management"),
    ("position", "Add your position", "/management"),
]


def progress(user_id: int) -> dict:
    """Which steps the user has done. Tracking a stock implies they found and read one, so those two follow from tracking."""
    with get_users_conn() as u:
        rules = u.execute("SELECT 1 FROM framework_overrides WHERE scope = 'user' AND user_id = ? LIMIT 1", (user_id,)).fetchone() is not None
        track = u.execute("SELECT 1 FROM tracked_positions WHERE user_id = ? LIMIT 1", (user_id,)).fetchone() is not None
        story = u.execute("SELECT 1 FROM position_snapshots s JOIN tracked_positions t ON t.id = s.position_id WHERE t.user_id = ? LIMIT 1",
                          (user_id,)).fetchone() is not None
        position = u.execute("SELECT 1 FROM position_events WHERE user_id = ? AND event_type IN ('open', 'add') LIMIT 1", (user_id,)).fetchone() is not None
        aikey = u.execute("SELECT 1 FROM user_provider_keys WHERE user_id = ? LIMIT 1", (user_id,)).fetchone() is not None
        mine = u.execute("SELECT 1 FROM rule_sets WHERE user_id = ? LIMIT 1", (user_id,)).fetchone() is not None
        deep = u.execute("SELECT 1 FROM research_reports WHERE user_id = ? AND status = 'success' LIMIT 1", (user_id,)).fetchone() is not None
        dismissed = u.execute("SELECT guide_dismissed_at IS NOT NULL FROM users WHERE id = ?", (user_id,)).fetchone()
    done = {"signin": True, "aikey": aikey, "rules": rules, "myscreener": mine, "find": track, "analyse": track, "track": track, "story": story, "deep": deep, "position": position}
    return {"done": done, "count": sum(done.values()), "total": len(STEPS), "dismissed": bool(dismissed and dismissed[0]),
            "next": next((k for k, _t, _h in STEPS if not done[k]), None)}


@router.get("/guide", response_class=HTMLResponse)
def guide(request: Request):
    activity.log(request.state.user_id, "step", "guide_viewed")
    return templates.TemplateResponse("guide.html", {"request": request, "active_nav": "guide", "prog": progress(request.state.user_id)})


@router.post("/guide/dismiss")
def guide_dismiss(request: Request):
    with get_users_conn() as u:
        u.execute("UPDATE users SET guide_dismissed_at = COALESCE(guide_dismissed_at, CURRENT_TIMESTAMP) WHERE id = ?", (request.state.user_id,))
        u.commit()
    activity.log(request.state.user_id, "step", "guide_dismissed")
    return RedirectResponse("/analytics", status_code=303)
