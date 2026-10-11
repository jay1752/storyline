"""Per-user activity for the Platform admin > Users tab.

Three kinds of row (table user_activity):
  signin  a sign-in
  step    something the user did that matters (welcome seen, file attached, import confirmed, stock added, rules changed...)
  beat    "still here": the page sends one about every 30 seconds while the tab is visible and the user has touched it in the last 2 minutes.
          Time on site is the sum of beat seconds; a visit ends after 30 minutes without a beat.

Only the type, the page area and the time are stored (plus a short non-personal detail such as a file extension or a row count). Never holdings,
prices, uploaded files or chat text. Everything here fails soft: tracking must never break the page it is attached to."""
import logging
from datetime import datetime, timezone

from app.db import get_users_conn

logger = logging.getLogger(__name__)
BEAT_MAX_GAP = 45          # seconds: a gap longer than this is not counted as time on site
VISIT_GAP_MIN = 30         # minutes without a beat that ends a visit
RETENTION_DAYS = 90
AREAS = {"home", "stocks", "stock", "tracking", "story", "portfolio", "rules", "community", "settings", "profile", "welcome", "admin", "other"}


def log(user_id, kind: str, area: str | None = None, secs: int | None = None, detail: str | None = None, conn=None) -> None:
    if not user_id:
        return
    try:
        row = (user_id, kind, area, secs, (detail or None) and str(detail)[:60])
        sql = "INSERT INTO user_activity (user_id, kind, area, secs, detail) VALUES (?,?,?,?,?)"
        if conn is not None:
            conn.execute(sql, row)  # the caller's transaction: it commits
            return
        with get_users_conn() as u:
            u.execute(sql, row)
            u.commit()
    except Exception as e:  # noqa: BLE001
        logger.info("activity log skipped: %s", e)


def beat(user_id, area: str) -> None:
    """One 'still here' ping. Seconds are measured on the server from the user's previous beat, so a client can't inflate them."""
    area = area if area in AREAS else "other"
    try:
        with get_users_conn() as u:
            last = u.execute("SELECT ts FROM user_activity WHERE user_id = ? AND kind = 'beat' ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
            gap = None
            if last:
                gap = (datetime.now(timezone.utc) - datetime.strptime(last["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)).total_seconds()
                if gap < 20:
                    return  # too soon: ignore duplicates
            secs = int(min(gap, BEAT_MAX_GAP)) if gap is not None and gap <= BEAT_MAX_GAP else 15
            u.execute("INSERT INTO user_activity (user_id, kind, area, secs) VALUES (?,?,?,?)", (user_id, "beat", area, secs))
            u.commit()
    except Exception as e:  # noqa: BLE001
        logger.info("beat skipped: %s", e)


def purge() -> int:
    try:
        with get_users_conn() as u:
            n = u.execute("DELETE FROM user_activity WHERE ts < datetime('now', ?)", (f"-{RETENTION_DAYS} days",)).rowcount
            u.commit()
            return n
    except Exception as e:  # noqa: BLE001
        logger.warning("activity purge failed: %s", e)
        return 0
