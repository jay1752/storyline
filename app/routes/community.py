"""Community, in two parts:

  1. The DAILY BRIEFING (app/ai/community_briefing.py) — a fixed card at the top, identical for every user, written
     only by the scheduled job. It lives in community_posts (kind 'ai_daily') but is deliberately NOT part of the
     discussion: the feed excludes it, and the API refuses replies, reactions and deletes on it.
  2. COMMUNITY NOTES — member conversations. Newest conversation first, 10 at a time ("load more" fetches the next
     10 older ones); every conversation shows its replies inline (the latest 3, with "show earlier replies").

Everything the browser receives is plain JSON; message bodies are rendered client-side with the DOM-only markdown
renderer (never innerHTML), so a post can't inject markup. Tagging a stock is `$MCX` / `$AAPL` / `$NSE:MCX`: the
server resolves it to the canonical symbol (India first, same convention as statement import) so the message links
to that stock's Analytics page and the channel can be filtered per stock."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.db import get_market_conn, get_users_conn
from app.templates_config import templates
from app.tracking import build_symbol_lookup

router = APIRouter()

MAX_BODY = 4000
PAGE_SIZE = 10  # conversations per page in Community notes
REPLIES_INLINE = 3  # replies shown under each conversation before "show earlier replies"
EMOJIS = ("👍", "🐂", "🐻", "🔥", "👀")
RATE_LIMIT = (8, 60)  # at most 8 posts per 60 seconds per user — cheap spam brake, resets on restart
_recent: dict[int, deque] = defaultdict(deque)

_TICKER = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9.\-]{0,14}(?::[A-Za-z0-9.\-]{1,15})?)")
_lookup_cache: dict = {"at": 0.0, "lookup": {}, "symbols": set()}


def _lookup():
    """{TICKER or SYMBOL -> canonical 'EXCH:TICKER'} (NSE preferred), cached for 15 minutes."""
    if time.time() - _lookup_cache["at"] > 900 or not _lookup_cache["lookup"]:
        with get_market_conn() as m:
            lookup, _names = build_symbol_lookup(m)
        _lookup_cache.update(at=time.time(), lookup=lookup, symbols=set(lookup.values()))
    return _lookup_cache["lookup"], _lookup_cache["symbols"]


def tag_stocks(body: str) -> tuple[str, str | None]:
    """Rewrite $TICKER mentions to canonical $EXCH:TICKER where we know the stock; return (body, first symbol)."""
    lookup, symbols = _lookup()
    first = None

    def repl(m: re.Match) -> str:
        nonlocal first
        tok = m.group(1).upper()
        canon = tok if (":" in tok and tok in symbols) else (None if ":" in tok else lookup.get(tok))
        if not canon:
            return m.group(0)
        first = first or canon
        return "$" + canon

    return _TICKER.sub(repl, body), first


def _handle(user_id: int | None, display_name: str | None) -> str:
    """The name shown on a message. Never derived from the email (that would leak half of someone's address to
    every other member): until a member picks a name they appear as 'Investor <id>'."""
    return display_name or (f"Investor {user_id}" if user_id else "Investor")


def _iso(ts: str) -> str:
    return ts.replace(" ", "T") + "Z"  # SQLite CURRENT_TIMESTAMP is UTC


def _need_login(request: Request):
    if request.state.user_id is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    return None


def _serialize(rows, me: int, counts: dict, reactions: dict) -> list[dict]:
    out = []
    for r in rows:
        rid = r["id"]
        deleted = bool(r["deleted"])
        bot = r["kind"] == "ai_daily"
        out.append({
            "id": rid,
            "parent_id": r["parent_id"],
            "kind": r["kind"],
            "bot": bot,
            "author": "Stocktale AI" if bot else _handle(r["user_id"], r["display_name"]),
            "mine": (r["user_id"] == me) and not deleted and not bot,
            "body": "" if deleted else r["body"],
            "deleted": deleted,
            "symbol": r["symbol"],
            "created": _iso(r["created_at"]),
            "reply_count": counts.get(rid, 0),
            "reactions": {} if deleted else reactions.get(rid, {}),
            "meta": json.loads(r["meta_json"]) if (bot and r["meta_json"]) else None,
        })
    return out


_SELECT = ("SELECT p.*, u.email, u.display_name FROM community_posts p "
           "LEFT JOIN users u ON u.id = p.user_id ")


def _decorate(u, rows, me: int) -> list[dict]:
    ids = [r["id"] for r in rows]
    counts, reactions = {}, {}
    if ids:
        ph = ",".join("?" * len(ids))
        counts = {r["parent_id"]: r["n"] for r in u.execute(
            f"SELECT parent_id, COUNT(*) n FROM community_posts WHERE parent_id IN ({ph}) AND deleted = 0 GROUP BY parent_id", ids)}
        for r in u.execute(
                f"SELECT post_id, emoji, COUNT(*) n, SUM(user_id = ?) mine FROM community_reactions WHERE post_id IN ({ph}) GROUP BY post_id, emoji",
                [me, *ids]):
            reactions.setdefault(r["post_id"], {})[r["emoji"]] = {"n": r["n"], "mine": bool(r["mine"])}
    return _serialize(rows, me, counts, reactions)


@router.get("/community", response_class=HTMLResponse)
def community_page(request: Request, symbol: str | None = None):
    user = request.state.user
    with get_users_conn() as u:
        brief, dates = _briefing(u), _briefing_dates(u)
    return templates.TemplateResponse(
        "community.html",
        {
            "request": request,
            "active_nav": "community",
            "briefing": brief,
            "briefing_dates": dates,
            "filter_symbol": symbol,
            "my_handle": _handle(request.state.user_id, user["display_name"] if "display_name" in user.keys() else None),
            "emojis": EMOJIS,
        },
    )


def _latest_replies(u, parent_ids: list[int], me: int, n: int = REPLIES_INLINE) -> dict[int, list[dict]]:
    """The newest `n` replies per conversation, oldest-first within each (so they read in order)."""
    if not parent_ids:
        return {}
    ph = ",".join("?" * len(parent_ids))
    rows = u.execute(
        f"""SELECT * FROM (
                SELECT p.*, usr.email, usr.display_name,
                       ROW_NUMBER() OVER (PARTITION BY p.parent_id ORDER BY p.id DESC) AS rn
                FROM community_posts p LEFT JOIN users usr ON usr.id = p.user_id
                WHERE p.parent_id IN ({ph}) AND p.deleted = 0
            ) WHERE rn <= ? ORDER BY parent_id, id""",
        [*parent_ids, n],
    ).fetchall()
    out: dict[int, list[dict]] = {}
    for r in _decorate(u, rows, me):
        out.setdefault(r["parent_id"], []).append(r)
    return out


def _briefing(u, date: str | None = None):
    where, params = ["kind = 'ai_daily'", "deleted = 0"], []
    if date:
        where.append("briefing_date = ?")
        params.append(date)
    row = u.execute(f"SELECT * FROM community_posts WHERE {' AND '.join(where)} ORDER BY briefing_date DESC LIMIT 1", params).fetchone()
    if not row:
        return None
    return {"id": row["id"], "date": row["briefing_date"], "body": row["body"], "created": _iso(row["created_at"]),
            "meta": json.loads(row["meta_json"]) if row["meta_json"] else None}


def _briefing_dates(u) -> list[str]:
    return [r["briefing_date"] for r in u.execute(
        "SELECT briefing_date FROM community_posts WHERE kind = 'ai_daily' AND deleted = 0 ORDER BY briefing_date DESC LIMIT 30")]


@router.get("/community/api/briefing")
def briefing(request: Request, date: str | None = None):
    """A day's briefing (default: the latest) plus the dates available. Read-only for everyone."""
    if (r := _need_login(request)):
        return r
    with get_users_conn() as u:
        return {"briefing": _briefing(u, date), "dates": _briefing_dates(u)}


@router.get("/community/api/feed")
def feed(request: Request, before: int | None = None, symbol: str | None = None):
    """Community notes: the next PAGE_SIZE conversations older than `before`, newest first, each with its latest replies."""
    if (r := _need_login(request)):
        return r
    me = request.state.user_id
    where, params = ["p.parent_id IS NULL", "p.kind = 'user'"], []
    if symbol:
        where.append("p.symbol = ?")
        params.append(symbol)
    base_where = " AND ".join(where)
    if before:
        where.append("p.id < ?")
        params.append(before)
    with get_users_conn() as u:
        rows = u.execute(_SELECT + f"WHERE {' AND '.join(where)} ORDER BY p.id DESC LIMIT ?", [*params, PAGE_SIZE + 1]).fetchall()
        has_more = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        posts = _decorate(u, rows, me)
        replies = _latest_replies(u, [p["id"] for p in posts], me)
        for p in posts:
            p["replies"] = replies.get(p["id"], [])
        # A deleted conversation disappears unless replies hang off it (then a "deleted" stub keeps them readable).
        posts = [p for p in posts if not p["deleted"] or p["reply_count"]]
        older = 0
        if has_more and rows:
            sym_params = [symbol] if symbol else []
            older = u.execute(f"SELECT COUNT(*) FROM community_posts p WHERE {base_where} AND p.id < ? AND p.deleted = 0",
                              [*sym_params, rows[-1]["id"]]).fetchone()[0]
    return {"posts": posts, "has_more": has_more, "older": older}


@router.get("/community/api/thread/{post_id}")
def thread(request: Request, post_id: int):
    if (r := _need_login(request)):
        return r
    me = request.state.user_id
    with get_users_conn() as u:
        root = u.execute(_SELECT + "WHERE p.id = ? AND p.parent_id IS NULL AND p.kind = 'user'", (post_id,)).fetchone()
        if not root:
            return JSONResponse({"error": "That message doesn't exist."}, status_code=404)
        replies = u.execute(_SELECT + "WHERE p.parent_id = ? AND p.deleted = 0 ORDER BY p.id", (post_id,)).fetchall()
        decorated = _decorate(u, [root, *replies], me)
    return {"post": decorated[0], "replies": decorated[1:]}


@router.get("/community/api/poll")
def poll(request: Request, after: int = 0, symbol: str | None = None, ids: str = ""):
    """New top-level messages since `after`, plus fresh reply counts / reactions for the ids currently on screen."""
    if (r := _need_login(request)):
        return r
    me = request.state.user_id
    visible = [int(x) for x in ids.split(",") if x.strip().isdigit()][:80]
    with get_users_conn() as u:
        where, params = ["p.parent_id IS NULL", "p.kind = 'user'", "p.id > ?", "p.deleted = 0"], [after]
        if symbol:
            where.append("p.symbol = ?")
            params.append(symbol)
        new_rows = u.execute(_SELECT + f"WHERE {' AND '.join(where)} ORDER BY p.id LIMIT 50", params).fetchall()
        new_posts = _decorate(u, new_rows, me)
        for p in new_posts:
            p["replies"] = []
        updates = {}
        if visible:
            ph = ",".join("?" * len(visible))
            rows = u.execute(_SELECT + f"WHERE p.id IN ({ph})", visible).fetchall()
            for p in _decorate(u, rows, me):
                updates[p["id"]] = {"reply_count": p["reply_count"], "reactions": p["reactions"], "deleted": p["deleted"]}
    return {"posts": new_posts, "updates": updates}


@router.post("/community/api/post")
async def create_post(request: Request):
    if (r := _need_login(request)):
        return r
    me = request.state.user_id
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "Bad request."}, status_code=400)
    body = re.sub(r"\n{4,}", "\n\n\n", str(data.get("body") or "").replace("\r\n", "\n")).strip()
    parent_id = data.get("parent_id")
    if not body:
        return JSONResponse({"error": "Write something first."}, status_code=400)
    if len(body) > MAX_BODY:
        return JSONResponse({"error": f"Messages are limited to {MAX_BODY} characters."}, status_code=400)

    now, window = time.time(), RATE_LIMIT[1]
    q = _recent[me]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= RATE_LIMIT[0]:
        return JSONResponse({"error": "You're posting too fast — wait a few seconds."}, status_code=429)
    q.append(now)

    body, symbol = tag_stocks(body)
    with get_users_conn() as u:
        if parent_id is not None:
            parent = u.execute("SELECT id, parent_id, deleted, kind FROM community_posts WHERE id = ?", (parent_id,)).fetchone()
            if not parent or parent["parent_id"] is not None or parent["kind"] != "user":
                return JSONResponse({"error": "You can only reply to a community conversation."}, status_code=400)
            symbol = None  # a reply inherits its thread's topic; only top-level messages carry a stock tag
        cur = u.execute(
            "INSERT INTO community_posts (user_id, kind, body, parent_id, symbol) VALUES (?, 'user', ?, ?, ?)",
            (me, body, parent_id, symbol),
        )
        u.commit()
        row = u.execute(_SELECT + "WHERE p.id = ?", (cur.lastrowid,)).fetchone()
        post = _decorate(u, [row], me)[0]
    return {"post": post}


@router.post("/community/api/delete")
async def delete_post(request: Request):
    if (r := _need_login(request)):
        return r
    me = request.state.user_id
    data = await request.json()
    with get_users_conn() as u:
        cur = u.execute(
            "UPDATE community_posts SET deleted = 1 WHERE id = ? AND user_id = ? AND kind = 'user'", (int(data.get("id", 0)), me)
        )
        u.commit()
    return {"deleted": cur.rowcount == 1}


@router.post("/community/api/react")
async def react(request: Request):
    if (r := _need_login(request)):
        return r
    me = request.state.user_id
    data = await request.json()
    emoji, pid = data.get("emoji"), int(data.get("id", 0))
    if emoji not in EMOJIS:
        return JSONResponse({"error": "Unknown reaction."}, status_code=400)
    with get_users_conn() as u:
        if not u.execute("SELECT 1 FROM community_posts WHERE id = ? AND deleted = 0 AND kind = 'user'", (pid,)).fetchone():
            return JSONResponse({"error": "That message doesn't exist."}, status_code=404)
        had = u.execute("SELECT 1 FROM community_reactions WHERE post_id = ? AND user_id = ? AND emoji = ?", (pid, me, emoji)).fetchone()
        if had:
            u.execute("DELETE FROM community_reactions WHERE post_id = ? AND user_id = ? AND emoji = ?", (pid, me, emoji))
        else:
            u.execute("INSERT INTO community_reactions (post_id, user_id, emoji) VALUES (?, ?, ?)", (pid, me, emoji))
        u.commit()
        row = u.execute(_SELECT + "WHERE p.id = ?", (pid,)).fetchone()
        post = _decorate(u, [row], me)[0]
    return {"reactions": post["reactions"]}


@router.post("/community/api/name")
async def set_name(request: Request):
    if (r := _need_login(request)):
        return r
    me = request.state.user_id
    name = re.sub(r"\s+", " ", str((await request.json()).get("name") or "")).strip()
    if name and not re.fullmatch(r"[A-Za-z0-9 ._'\-]{2,24}", name):
        return JSONResponse({"error": "Use 2–24 letters, numbers, spaces or . _ - '"}, status_code=400)
    with get_users_conn() as u:
        u.execute("UPDATE users SET display_name = ? WHERE id = ?", (name or None, me))
        u.commit()
    return {"name": name or None}
