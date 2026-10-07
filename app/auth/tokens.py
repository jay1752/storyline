"""Server-side sessions (sessions table) and single-use expiring tokens (auth_tokens,
shared by email verification and password reset — structurally identical: random,
expiring, single-use). Session IDs and tokens are both high-entropy random strings
(secrets.token_urlsafe) used as opaque bearer credentials, validated by DB lookup — not
signed/JWT, so revocation is a plain DELETE, not a blocklist."""

import secrets
from datetime import datetime, timedelta, timezone

from app.db import get_users_conn

SESSION_TTL_DAYS = 30
TOKEN_TTL_HOURS = {"verify_email": 24, "reset_password": 1}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def create_session(user_id: int) -> str:
    session_id = secrets.token_urlsafe(32)
    with get_users_conn() as u:
        u.execute(
            "INSERT INTO sessions (session_id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (session_id, user_id, _now(), _future(timedelta(days=SESSION_TTL_DAYS))),
        )
        u.commit()
    return session_id


def get_session_user(session_id: str):
    """Returns the users row for a valid, unexpired session, or None."""
    with get_users_conn() as u:
        row = u.execute(
            "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
            "WHERE sessions.session_id = ? AND sessions.expires_at > ?",
            (session_id, _now()),
        ).fetchone()
    return row


def delete_session(session_id: str) -> None:
    with get_users_conn() as u:
        u.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        u.commit()


def delete_all_sessions(user_id: int) -> None:
    """Used by password reset — a reset should force re-login everywhere, including a
    device where an attacker's stolen session is still open."""
    with get_users_conn() as u:
        u.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        u.commit()


def create_auth_token(user_id: int, purpose: str) -> str:
    token = secrets.token_urlsafe(32)
    with get_users_conn() as u:
        u.execute(
            "INSERT INTO auth_tokens (token, user_id, purpose, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, purpose, _future(timedelta(hours=TOKEN_TTL_HOURS[purpose]))),
        )
        u.commit()
    return token


def consume_auth_token(token: str, purpose: str) -> int | None:
    """Validates the token (right purpose, unused, unexpired) and marks it used in the
    same step — a token is usable exactly once, even if the request is retried."""
    with get_users_conn() as u:
        row = u.execute(
            "SELECT user_id FROM auth_tokens WHERE token = ? AND purpose = ? "
            "AND used_at IS NULL AND expires_at > ?",
            (token, purpose, _now()),
        ).fetchone()
        if not row:
            return None
        u.execute("UPDATE auth_tokens SET used_at = ? WHERE token = ?", (_now(), token))
        u.commit()
    return row["user_id"]
