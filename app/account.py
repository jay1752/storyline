"""Permanent account deletion (the Profile page's "Delete my account").

Removes everything tied to the user in users.db: sessions and login tokens, saved AI keys and settings, rules overrides, tracked
positions with their snapshots, journal events, analysis runs, pending imports, community reactions and posts. A post that other people
replied to is blanked and detached from the account (so their replies keep their thread) rather than removed. Market data holds nothing
about users. The whole thing runs in one transaction: it either completes or changes nothing."""
from app.db import get_users_conn


class CannotDelete(Exception):
    """The account may not be deleted from the UI (the message is shown to the user)."""


def delete_account(user_id: int) -> dict:
    with get_users_conn() as u:
        me = u.execute("SELECT is_platform_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if me is None:
            raise CannotDelete("That account no longer exists.")
        if me["is_platform_admin"]:
            raise CannotDelete("Platform admin accounts can't be deleted here, because the platform would be left without an admin. Remove the admin role first.")
        counts = {}
        try:
            u.execute("BEGIN")
            pos_ids = [r[0] for r in u.execute("SELECT id FROM tracked_positions WHERE user_id = ?", (user_id,))]
            if pos_ids:
                marks = ",".join("?" * len(pos_ids))
                u.execute(f"DELETE FROM position_snapshots WHERE position_id IN ({marks})", pos_ids)
            for table in ("position_events", "insight_runs", "tracked_positions", "pending_imports", "framework_overrides", "user_provider_keys",
                          "user_settings", "auth_tokens", "sessions"):
                counts[table] = u.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,)).rowcount
            mine = [r[0] for r in u.execute("SELECT id FROM community_posts WHERE user_id = ?", (user_id,))]
            if mine:
                marks = ",".join("?" * len(mine))
                u.execute(f"DELETE FROM community_reactions WHERE post_id IN ({marks})", mine)
            u.execute("DELETE FROM community_reactions WHERE user_id = ?", (user_id,))
            replied = {r[0] for r in u.execute("SELECT DISTINCT parent_id FROM community_posts WHERE parent_id IS NOT NULL AND user_id != ?", (user_id,))}
            kept = [p for p in mine if p in replied]
            gone = [p for p in mine if p not in replied]
            if kept:
                marks = ",".join("?" * len(kept))
                u.execute(f"UPDATE community_posts SET user_id = NULL, body = '[deleted]', deleted = 1 WHERE id IN ({marks})", kept)
            if gone:  # own replies to own posts go first, then the posts
                marks = ",".join("?" * len(gone))
                u.execute(f"DELETE FROM community_posts WHERE parent_id IN ({marks}) AND user_id = ?", gone + [user_id])
                u.execute(f"DELETE FROM community_posts WHERE id IN ({marks})", gone)
            u.execute("DELETE FROM community_posts WHERE user_id = ?", (user_id,))  # the user's replies in other people's threads
            counts["community_posts"] = len(mine)
            u.execute("DELETE FROM users WHERE id = ?", (user_id,))
            u.execute("COMMIT")
        except Exception:
            u.execute("ROLLBACK")
            raise
    return counts
