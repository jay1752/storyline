"""A tiny in-process scheduler (no host cron/launchd needed, so it survives a sleeping laptop: it simply catches up on the
next tick). Every few minutes it asks, for each job in JOBS, "is it enabled, a day it runs on, past its time, and not
already done for today?" — and if so enqueues it. A recent attempt (any status, within 2 hours) pauses a job, so an outage or
an exhausted model quota doesn't turn into a retry storm.

Each job's time, weekdays and on/off can be changed by a platform admin (Admin > Scheduled jobs); the values live in
users.db `job_settings` and fall back to the defaults below. The tick re-reads them every time, so a change applies at once."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from app.db import get_market_conn, get_users_conn

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
INSIGHTS_RETRY = timedelta(hours=3)  # after any attempt (even a quota stop), wait this long before trying a user again
TICK_SECONDS = 300
RETRY_AFTER = timedelta(hours=2)

_started = False

ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)
# key -> definition. `hm` = minute of the IST day it may start; `days` = IST weekdays it runs (Mon=0); `source` = the
# ingestion_runs.source_name its runs are recorded under.
JOBS = {
    "daily_prices_in": {
        "label": "India end-of-day prices", "source": "daily_prices_in", "hm": 19 * 60, "days": (0, 1, 2, 3, 4),
        "desc": "NSE's daily price file for every listed stock, plus a re-check of the last two weeks. After NSE publishes it (~18:30–19:00 IST).",
    },
    "daily_prices_us": {
        "label": "US end-of-day prices", "source": "daily_prices_us", "hm": 5 * 60 + 30, "days": (1, 2, 3, 4, 5),
        "desc": "Yahoo prices in batches for every US stock with fundamentals, stalest first. After the 16:00 ET close (early morning IST, so Tue–Sat).",
    },
    "community_briefing": {
        "label": "Community briefing", "source": "community_briefing", "hm": 7 * 60 + 30, "days": ALL_DAYS,
        "desc": "The AI-written market briefing at the top of Community. After the US close, before India opens.",
    },
    "insights": {
        "label": "Daily stock analysis", "source": "insights", "hm": 8 * 60 + 30, "days": ALL_DAYS,
        "desc": "Each user's per-stock story and flags (users with tracked stocks, a usable AI model, and automatic analysis on). Which stocks run follows each user's Investment rules: platform default, own setting, per-stock switch.",
    },
}


def hm_text(hm: int) -> str:
    return f"{hm // 60:02d}:{hm % 60:02d}"


def parse_hm(text: str) -> int:
    h, m = text.strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("time out of range")
    return h * 60 + m


def job_config(key: str, u=None) -> dict:
    """Effective settings for one job: defaults, overridden by an admin's saved row."""
    d = JOBS[key]
    cfg = {"enabled": True, "hm": d["hm"], "days": set(d["days"]), "customised": False}

    def read(conn):
        return conn.execute("SELECT enabled, hm, days FROM job_settings WHERE key = ?", (key,)).fetchone()

    if u is None:
        with get_users_conn() as conn:
            row = read(conn)
    else:
        row = read(u)
    if row:
        cfg.update(enabled=bool(row["enabled"]), hm=row["hm"], days={int(x) for x in row["days"].split(",") if x != ""}, customised=True)
    return cfg


def save_job(key: str, enabled: bool, hm: int, days) -> None:
    days = sorted({int(x) for x in days})
    if key not in JOBS or not days or any(x not in ALL_DAYS for x in days):
        raise ValueError("Pick at least one valid weekday.")
    d = JOBS[key]
    with get_users_conn() as u:
        if enabled and hm == d["hm"] and tuple(days) == tuple(d["days"]):  # identical to the defaults: not a customisation
            u.execute("DELETE FROM job_settings WHERE key = ?", (key,))
        else:
            u.execute("INSERT INTO job_settings (key, enabled, hm, days) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                      "enabled=excluded.enabled, hm=excluded.hm, days=excluded.days, updated_at=CURRENT_TIMESTAMP",
                      (key, 1 if enabled else 0, hm, ",".join(str(x) for x in days)))
        u.commit()


def reset_job(key: str) -> None:
    with get_users_conn() as u:
        u.execute("DELETE FROM job_settings WHERE key = ?", (key,))
        u.commit()


def _due(key: str, now: datetime | None = None) -> bool:
    """Enabled, scheduled for today's weekday, and past its start time (IST)."""
    now = now or datetime.now(IST)
    cfg = job_config(key)
    return cfg["enabled"] and now.weekday() in cfg["days"] and now.hour * 60 + now.minute >= cfg["hm"]


def next_run_text(key: str, last_success_utc: str | None = None) -> str:
    """Human 'next run' for the admin table, IST."""
    cfg, now = job_config(key), datetime.now(IST)
    if not cfg["enabled"]:
        return "paused"
    if now.weekday() in cfg["days"] and now.hour * 60 + now.minute >= cfg["hm"]:
        start = now.replace(hour=cfg["hm"] // 60, minute=cfg["hm"] % 60, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        if not last_success_utc or last_success_utc < start:
            return "due now — starts within 5 minutes"
    for add in range(0, 8):
        day = now + timedelta(days=add)
        if day.weekday() not in cfg["days"]:
            continue
        at = day.replace(hour=cfg["hm"] // 60, minute=cfg["hm"] % 60, second=0, microsecond=0)
        if at > now:
            return ("today " if add == 0 else "tomorrow " if add == 1 else at.strftime("%a %d %b ")) + hm_text(cfg["hm"]) + " IST"
    return "—"


def ist_text(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).astimezone(IST).strftime("%d %b %H:%M")
    except ValueError:
        return iso[:16]


def jobs_view() -> list[dict]:
    """Everything the Admin 'Scheduled jobs' table shows."""
    runs, out = last_runs(), []
    for key, d in JOBS.items():
        cfg, r = job_config(key), runs[key]
        last = r["last"]
        out.append({
            "key": key, "label": d["label"], "desc": d["desc"], "enabled": cfg["enabled"], "time": hm_text(cfg["hm"]),
            "days": sorted(cfg["days"]), "customised": cfg["customised"], "default_time": hm_text(d["hm"]), "default_days": list(d["days"]),
            "last": ({**last, "started_ist": ist_text(last["started_at"]),
                      "took": _took(last)} if last else None),
            "next": next_run_text(key, r["last_success"]),
        })
    return out


def _took(run: dict) -> str:
    if not run.get("finished_at"):
        return ""
    try:
        sec = int((datetime.fromisoformat(run["finished_at"]) - datetime.fromisoformat(run["started_at"])).total_seconds())
    except ValueError:
        return ""
    return f"{sec // 60}m {sec % 60}s" if sec >= 60 else f"{sec}s"


def last_runs(keys=None) -> dict:
    """Latest ingestion_runs row per job, keyed by job key."""
    out = {}
    with get_market_conn() as m:
        for key, d in JOBS.items():
            if keys and key not in keys:
                continue
            r = m.execute("SELECT id, status, started_at, finished_at, rows_written, error_message FROM ingestion_runs "
                          "WHERE source_name = ? ORDER BY id DESC LIMIT 1", (d["source"],)).fetchone()
            ok = m.execute("SELECT started_at FROM ingestion_runs WHERE source_name = ? AND status = 'success' ORDER BY id DESC LIMIT 1", (d["source"],)).fetchone()
            out[key] = {"last": dict(r) if r else None, "last_success": ok["started_at"] if ok else None}
    return out



def start_scheduler() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="scheduler").start()


def _loop() -> None:
    time.sleep(90)  # let startup (migrations, worker pool) finish before the first check
    while True:
        try:
            _tick()
        except Exception:  # noqa: BLE001 — the scheduler must never die
            logger.exception("scheduler tick failed")
        time.sleep(TICK_SECONDS)


def _tick() -> None:
    _tick_prices()      # first: the briefing and the position analyses should read today's prices
    _tick_briefing()
    _tick_insights()


def _tick_prices() -> None:
    """Daily end-of-day prices for every stock (app/ingestion/daily_prices_source.py): once per market per day, retried
    after RETRY_AFTER if it failed, and it catches up over a gap by itself (it walks every missed session)."""
    from app.ingestion.runner import enqueue_ingestion

    now = datetime.now(IST)
    for key in ("daily_prices_in", "daily_prices_us"):
        if not _due(key, now):
            continue
        cfg = job_config(key)
        due_since = now.replace(hour=cfg["hm"] // 60, minute=cfg["hm"] % 60, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        with get_market_conn() as m:
            last = m.execute("SELECT started_at, status, error_message FROM ingestion_runs WHERE source_name = ? ORDER BY id DESC LIMIT 1", (JOBS[key]["source"],)).fetchone()
        if last:
            if last["status"] in ("queued", "running"):
                continue
            if last["status"] == "success" and last["started_at"] >= due_since:
                continue  # already done for today's close
            # A run cut off by an app restart/redeploy didn't fail on its own merits — start it again straight away
            # instead of making it wait out the retry delay.
            restarted = (last["error_message"] or "").startswith("Interrupted by app restart")
            if not restarted and datetime.now(timezone.utc) - datetime.fromisoformat(last["started_at"]) < RETRY_AFTER:
                continue
        logger.info("scheduler: enqueueing %s", key)
        enqueue_ingestion(JOBS[key]["source"], "prices")


def _tick_insights() -> None:
    """Daily per-stock analysis for every user who has tracked stocks, an AI model they can use, and hasn't switched
    automatic analysis off. Runs after 08:30 IST; the batch itself skips stocks already analysed today and stops quietly
    when the model runs out of quota (it is picked up again by a later tick, i.e. after the quota resets)."""
    from app.ai.providers import build_chain
    from app.ingestion.runner import enqueue_insights

    now = datetime.now(IST)
    if not _due("insights", now):
        return
    day_start_utc = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    recent_cut = (datetime.now(timezone.utc) - INSIGHTS_RETRY).strftime("%Y-%m-%d %H:%M:%S")
    with get_users_conn() as u:
        users = [r["user_id"] for r in u.execute(
            """SELECT DISTINCT tp.user_id FROM tracked_positions tp
               LEFT JOIN user_settings us ON us.user_id = tp.user_id
               WHERE tp.status = 'active' AND COALESCE(us.auto_insights, 1) = 1""")]
        due = []
        for uid in users:
            pending = u.execute(
                """SELECT COUNT(*) FROM tracked_positions tp WHERE tp.user_id = ? AND tp.status = 'active' AND NOT EXISTS
                   (SELECT 1 FROM position_snapshots ps WHERE ps.position_id = tp.id AND ps.analysis_json IS NOT NULL AND ps.checked_at >= ?)""",
                (uid, day_start_utc)).fetchone()[0]
            recent = u.execute("SELECT 1 FROM insight_runs WHERE user_id = ? AND (status IN ('queued','running') OR started_at > ?) LIMIT 1",
                               (uid, recent_cut)).fetchone()
            if pending and not recent:
                due.append(uid)
    for uid in due:
        if build_chain(uid):  # no model/key → nothing to run (and nothing to retry every few hours)
            logger.info("scheduler: enqueueing daily stock analysis for user %s", uid)
            enqueue_insights(uid, trigger="daily")


def _tick_briefing() -> None:
    from app.ingestion.runner import enqueue_community_briefing

    now = datetime.now(IST)
    if not _due("community_briefing", now):
        return
    with get_users_conn() as u:
        if u.execute("SELECT 1 FROM community_posts WHERE briefing_date = ?", (now.date().isoformat(),)).fetchone():
            return
    with get_market_conn() as m:
        last = m.execute(
            "SELECT started_at, status FROM ingestion_runs WHERE source_name = 'community_briefing' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if last:
        if last["status"] in ("queued", "running"):
            return
        started = datetime.fromisoformat(last["started_at"])
        if datetime.now(timezone.utc) - started < RETRY_AFTER:
            return
    logger.info("scheduler: enqueueing today's community briefing")
    enqueue_community_briefing()
