"""A tiny in-process scheduler (no host cron/launchd needed, so it survives a sleeping laptop: it simply
catches up on the next tick).

Today it drives one job: the daily Community briefing. Every few minutes it asks "is it past 07:30 IST and
is there no briefing for today's date yet?" — if so, it enqueues one. A recent attempt (any status, within
2 hours) pauses it, so an outage or an exhausted model quota doesn't turn into a retry storm. The
unique briefing_date in community_posts is the real guarantee against duplicates."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from app.db import get_market_conn, get_users_conn

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
BRIEFING_HOUR_MIN = 7 * 60 + 30  # 07:30 IST — after the US close and before India's 09:15 open
INSIGHTS_HOUR_MIN = 8 * 60 + 30  # 08:30 IST — after the briefing, before the India open
INSIGHTS_RETRY = timedelta(hours=3)  # after any attempt (even a quota stop), wait this long before trying a user again
TICK_SECONDS = 300
RETRY_AFTER = timedelta(hours=2)

_started = False


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
    _tick_briefing()
    _tick_insights()


def _tick_insights() -> None:
    """Daily per-stock analysis for every user who has tracked stocks, an AI model they can use, and hasn't switched
    automatic analysis off. Runs after 08:30 IST; the batch itself skips stocks already analysed today and stops quietly
    when the model runs out of quota (it is picked up again by a later tick, i.e. after the quota resets)."""
    from app.ai.providers import build_chain
    from app.ingestion.runner import enqueue_insights

    now = datetime.now(IST)
    if now.hour * 60 + now.minute < INSIGHTS_HOUR_MIN:
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
    if now.hour * 60 + now.minute < BRIEFING_HOUR_MIN:
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
