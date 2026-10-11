"""Data for the Platform admin > Users tab: who is on the platform, how long they stay, which steps they reach, what failed.

Two sources: the activity table (app/activity.py: sign-ins, steps, "still here" beats) and what the rest of the database already knows
(accounts, tracked stocks, imports, AI runs, rule changes). Using both means an account that existed before activity tracking still shows
its steps, just without time on site."""
import statistics
from datetime import datetime, timedelta, timezone

from app import activity
from app.db import get_users_conn

STEPS = ["Signed in", "Welcome page", "Explored a stock", "Added or imported", "Rules or story", "Came back"]
AREA_LABEL = {"stock": "Stock pages", "stocks": "Find a stock", "portfolio": "Portfolio", "tracking": "Tracking", "story": "Stock stories",
              "rules": "Rules", "community": "Community", "welcome": "Welcome", "settings": "AI settings", "profile": "Profile", "home": "Home", "other": "Other"}
STEP_LABEL = {"welcome_viewed": "Welcome page seen", "import_attached": "Statement attached", "import_proposed": "Import proposed",
              "import_confirmed": "Import confirmed", "position_added": "Stock added", "rules_changed": "Changed investing rules", "chat_message": "Asked the assistant"}


def _utc(s: str) -> datetime:
    return datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def mask(email: str | None) -> str:
    a, _, d = (email or "").partition("@")
    return (a[:2] + "***@" + d) if a else "-"


def fmt_secs(n: float | None) -> str:
    if not n:
        return "—"
    n = int(n)
    h, rem = divmod(n, 3600)
    m, s = divmod(rem, 60)
    return (f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s")


def _ago(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    mins = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    if mins < 2:
        return "just now"
    if mins < 60:
        return f"{int(mins)} min ago"
    if mins < 60 * 24:
        return f"{int(mins // 60)} h ago"
    return ts.strftime("%d %b")


def _visits(beats: list[tuple[datetime, int, str]]) -> list[dict]:
    """beats = [(ts, secs, area)] oldest first, one user. A new visit starts after VISIT_GAP_MIN minutes without a beat."""
    out: list[dict] = []
    for ts, secs, area in beats:
        if out and (ts - out[-1]["end"]).total_seconds() <= activity.VISIT_GAP_MIN * 60:
            v = out[-1]
            v["end"], v["secs"] = ts, v["secs"] + secs
            v["areas"][area] = v["areas"].get(area, 0) + secs
        else:
            out.append({"start": ts, "end": ts, "secs": secs, "areas": {area: secs}})
    return out


def _facts(u, uid: int, since: str) -> dict:
    """Everything we know about one account, from activity rows and the rest of the database."""
    steps = {r[0] for r in u.execute("SELECT DISTINCT area FROM user_activity WHERE user_id = ? AND kind = 'step'", (uid,))}
    areas = {r[0] for r in u.execute("SELECT DISTINCT area FROM user_activity WHERE user_id = ? AND kind = 'beat'", (uid,))}
    days = {r[0] for r in u.execute("SELECT DISTINCT substr(ts,1,10) FROM user_activity WHERE user_id = ? AND kind IN ('beat','signin')", (uid,))}
    days |= {r[0] for r in u.execute("SELECT DISTINCT substr(created_at,1,10) FROM sessions WHERE user_id = ?", (uid,))}
    tracked = u.execute("SELECT COUNT(*) FROM tracked_positions WHERE user_id = ? AND status = 'active'", (uid,)).fetchone()[0]
    imports_ok = u.execute("SELECT COUNT(*) FROM pending_imports WHERE user_id = ? AND status = 'confirmed'", (uid,)).fetchone()[0]
    rules = u.execute("SELECT COUNT(*) FROM framework_overrides WHERE user_id = ?", (uid,)).fetchone()[0]
    ai_ok = u.execute("SELECT COUNT(*) FROM insight_runs WHERE user_id = ? AND status = 'success'", (uid,)).fetchone()[0]
    ai_bad = u.execute("SELECT COUNT(*) FROM insight_runs WHERE user_id = ? AND status = 'failed'", (uid,)).fetchone()[0]
    ai_limit = u.execute("SELECT COUNT(*) FROM insight_runs WHERE user_id = ? AND status = 'failed' AND (error LIKE '%usage limit%' OR error LIKE '%429%')", (uid,)).fetchone()[0]
    chats = u.execute("SELECT COUNT(*) FROM user_activity WHERE user_id = ? AND area = 'chat_message'", (uid,)).fetchone()[0]
    welcomed = u.execute("SELECT welcomed_at IS NOT NULL FROM users WHERE id = ?", (uid,)).fetchone()[0]
    reached = [True,
               bool(welcomed) or "welcome_viewed" in steps,
               bool(areas & {"stock", "stocks"}) or tracked > 0,
               tracked > 0 or "position_added" in steps or imports_ok > 0,
               bool(areas & {"rules", "story"}) or rules > 0 or "rules_changed" in steps or ai_ok > 0,
               len(days) >= 2]
    return {"reached": reached, "tracked": tracked, "imports_ok": imports_ok, "rules": rules, "ai_ok": ai_ok, "ai_bad": ai_bad, "ai_limit": ai_limit, "chats": chats,
            "imported": imports_ok > 0 or "import_confirmed" in steps}


def _status(f: dict, created: datetime, is_admin: bool) -> tuple[str, str]:
    if is_admin:
        return "Admin", ""
    if f["imported"]:
        return "Imported", "g"
    if f["tracked"]:
        return ("Hit AI limit", "w") if f["ai_limit"] and not f["ai_ok"] else ("Added a stock", "g")
    if (datetime.now(timezone.utc) - created).total_seconds() < 3600 and not f["reached"][2]:
        return "New", ""
    if f["reached"][1] and not f["reached"][2]:
        return "Stuck at welcome", "r"
    return "Browsing", ""


def overview(days: int = 7, flt: str = "all", q: str = "") -> dict:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    prior = (now - timedelta(days=2 * days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_users_conn() as u:
        users = u.execute("SELECT id, email, display_name, created_at, is_platform_admin adm FROM users ORDER BY id").fetchall()
        new_ids = [r["id"] for r in users if r["created_at"] >= since and not r["adm"]]
        prior_new = sum(1 for r in users if prior <= r["created_at"] < since and not r["adm"])
        beats = {}
        for r in u.execute("SELECT user_id, ts, secs, area FROM user_activity WHERE kind = 'beat' AND ts >= ? ORDER BY ts", (since,)):
            beats.setdefault(r["user_id"], []).append((_utc(r["ts"]), r["secs"] or 0, r["area"] or "other"))
        last_seen = {}
        for r in u.execute("SELECT user_id, MAX(ts) t FROM user_activity GROUP BY user_id"):
            last_seen[r["user_id"]] = _utc(r["t"])
        for r in u.execute("SELECT user_id, MAX(created_at) t FROM sessions GROUP BY user_id"):
            t = _utc(r["t"])
            if r["user_id"] not in last_seen or t > last_seen[r["user_id"]]:
                last_seen[r["user_id"]] = t
        all_visits = {uid: _visits(b) for uid, b in beats.items()}
        durations = [v["secs"] for vs in all_visits.values() for v in vs]
        active_now = len({uid for uid, b in beats.items() if (now - b[-1][0]).total_seconds() < 300})
        facts = {r["id"]: _facts(u, r["id"], since) for r in users}
        ai_fail = {"etf": 0, "limit": 0, "other": 0}
        for r in u.execute("SELECT error FROM insight_runs WHERE status = 'failed' AND started_at >= ?", (since,)):
            e = r["error"] or ""
            ai_fail["etf" if "no reported fundamentals" in e else "limit" if ("usage limit" in e or "429" in e) else "other"] += 1

        funnel = [sum(1 for uid in new_ids if facts[uid]["reached"][i]) for i in range(6)]
        rows = []
        for r in users:
            uid = r["id"]
            f = facts[uid]
            vs = all_visits.get(uid, [])
            total = sum(v["secs"] for v in vs)
            status, tone = _status(f, _utc(r["created_at"]), bool(r["adm"]))
            rows.append({"id": uid, "email": mask(r["email"]), "name": r["display_name"] or "", "joined": _utc(r["created_at"]).strftime("%d %b %H:%M"), "joined_dt": _utc(r["created_at"]),
                         "last": _ago(last_seen.get(uid)), "last_dt": last_seen.get(uid), "visits": len(vs) or None, "time": fmt_secs(total) if total else "—",
                         "avg": fmt_secs(total / len(vs)) if vs else "", "reached": f["reached"], "n_reached": sum(f["reached"]), "status": status, "tone": tone, "f": f,
                         "actions": ", ".join(x for x in (
                             f"{f['tracked']} stock{'s' if f['tracked'] != 1 else ''}" if f["tracked"] else "",
                             "imported" if f["imported"] else "",
                             f"AI {f['ai_ok']} ok" + (f", {f['ai_bad']} failed" if f["ai_bad"] else "") if (f["ai_ok"] or f["ai_bad"]) else "",
                             f"{f['chats']} chat" if f["chats"] else "", f"{f['rules']} rule{'s' if f['rules'] != 1 else ''} set" if f["rules"] else "") if x) or "None"})
        rows.sort(key=lambda x: x["last_dt"] or x["joined_dt"], reverse=True)
        if flt == "new":
            rows = [x for x in rows if x["joined_dt"] >= now - timedelta(days=days)]
        elif flt == "returning":
            rows = [x for x in rows if x["reached"][5]]
        elif flt == "stuck":
            rows = [x for x in rows if x["status"] in ("Stuck at welcome",)]
        if q:
            ql = q.lower()
            rows = [x for x in rows if ql in x["email"].lower() or ql in x["name"].lower()]
        came_back = sum(1 for r in users if not r["adm"] and facts[r["id"]]["reached"][5])

    first = funnel[3] if funnel else 0
    return {"days": days, "flt": flt, "q": q, "total_users": len(users), "new": len(new_ids), "prior_new": prior_new, "active_now": active_now,
            "median_visit": fmt_secs(statistics.median(durations)) if durations else "—", "first_step": f"{round(100 * first / len(new_ids))}%" if new_ids else "—",
            "first_step_n": f"{first} of {len(new_ids)} new" if new_ids else "no new users", "came_back": came_back, "ai_fail": ai_fail, "ai_fail_total": sum(ai_fail.values()),
            "funnel": [{"label": STEPS[i], "n": funnel[i], "pct": round(100 * funnel[i] / len(new_ids)) if new_ids else 0,
                        "drop": (round(100 * (funnel[i - 1] - funnel[i]) / funnel[i - 1]) if i and funnel[i - 1] and funnel[i] < funnel[i - 1] else None)} for i in range(6)],
            "rows": rows, "steps": STEPS, "tracking_since": _tracking_since()}


def _tracking_since() -> str | None:
    with get_users_conn() as u:
        r = u.execute("SELECT MIN(ts) FROM user_activity").fetchone()[0]
    return _utc(r).strftime("%d %b %Y") if r else None


def detail(uid: int, reveal: bool = False) -> dict | None:
    with get_users_conn() as u:
        r = u.execute("SELECT id, email, display_name, created_at, is_platform_admin adm FROM users WHERE id = ?", (uid,)).fetchone()
        if r is None:
            return None
        events: list[tuple[datetime, str, str, str]] = []   # (ts, label, note, tone)
        have_signin = False
        for a in u.execute("SELECT ts, kind, area, detail FROM user_activity WHERE user_id = ? AND kind IN ('signin','step') ORDER BY ts DESC LIMIT 200", (uid,)):
            if a["kind"] == "signin":
                have_signin = True
                events.append((_utc(a["ts"]), "Signed in with Google" + (" (first time)" if a["detail"] == "new" else ""), "", ""))
            else:
                events.append((_utc(a["ts"]), STEP_LABEL.get(a["area"], a["area"]), a["detail"] or "", ""))
        for p in u.execute("SELECT created_at, status FROM pending_imports WHERE user_id = ? ORDER BY id DESC LIMIT 30", (uid,)):
            if p["status"] in ("confirmed", "cancelled"):
                events.append((_utc(p["created_at"]), f"Import {p['status']}", "from the database", ""))
        if not have_signin:
            for s in u.execute("SELECT created_at FROM sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (uid,)):
                events.append((_utc(s["created_at"]), "Signed in", "", ""))
        runs: dict[tuple[str, str], int] = {}
        for a in u.execute("SELECT started_at, status, error FROM insight_runs WHERE user_id = ? ORDER BY id DESC LIMIT 400", (uid,)):
            why = ""
            if a["status"] == "failed":
                e = a["error"] or ""
                why = "no fundamentals to analyse (fund or ETF)" if "no reported fundamentals" in e else "AI usage limit" if ("usage limit" in e or "429" in e) else (e[:60] or "failed")
            k = ((a["started_at"] or "")[:16], a["status"] + "|" + why)
            runs[k] = runs.get(k, 0) + 1
        for (minute, sw), n in runs.items():
            status, _, why = sw.partition("|")
            events.append((_utc(minute + ":00"), f"Daily analysis {'ran' if status == 'success' else status}" + (f" × {n}" if n > 1 else ""), why, "bad" if status == "failed" else ""))
        events.sort(key=lambda e: e[0], reverse=True)
        beats = [(_utc(b["ts"]), b["secs"] or 0, b["area"] or "other") for b in u.execute(
            "SELECT ts, secs, area FROM user_activity WHERE user_id = ? AND kind = 'beat' AND ts >= datetime('now','-30 days') ORDER BY ts", (uid,))]
        sessions = [{"at": _utc(s["created_at"]).strftime("%d %b %H:%M")} for s in u.execute("SELECT created_at FROM sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 8", (uid,))]
        f = _facts(u, uid, "")
    vs = _visits(beats)
    by_area: dict[str, int] = {}
    for _, secs, area in beats:
        by_area[area] = by_area.get(area, 0) + secs
    top = max(by_area.values()) if by_area else 1
    status, tone = _status(f, _utc(r["created_at"]), bool(r["adm"]))
    return {"id": uid, "email": r["email"] if reveal else mask(r["email"]), "revealed": reveal, "name": r["display_name"] or "", "joined": _utc(r["created_at"]).strftime("%d %b %Y %H:%M"),
            "status": status, "tone": tone, "reached": f["reached"], "steps": STEPS, "facts": f,
            "events": [{"t": e[0].strftime("%d %b %H:%M"), "label": e[1], "note": e[2], "tone": e[3]} for e in events[:60]],
            "pages": sorted(({"label": AREA_LABEL.get(a, a), "secs": fmt_secs(s), "pct": max(2, round(100 * s / top))} for a, s in by_area.items()), key=lambda x: -x["pct"]),
            "visits": [{"start": v["start"].strftime("%d %b %H:%M"), "dur": fmt_secs(v["secs"]), "where": ", ".join(AREA_LABEL.get(a, a) for a, _ in sorted(v["areas"].items(), key=lambda kv: -kv[1])[:3])} for v in reversed(vs[-8:])],
            "total": fmt_secs(sum(v["secs"] for v in vs)), "n_visits": len(vs), "sessions": sessions}
