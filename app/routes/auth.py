"""Signup, login, logout, email verification, forgot/reset password. Plain POST +
redirect throughout (not htmx) — matches settings.html's convention, the closest
existing precedent for a simple form page, not the htmx-partial pattern used elsewhere."""

import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth.crypto import hash_password, verify_password
from app.auth.email import send_reset_email, send_verification_email
from app.auth.middleware import SESSION_COOKIE
from app.auth.tokens import (
    consume_auth_token,
    create_auth_token,
    create_session,
    delete_all_sessions,
    delete_session,
)
from app.db import get_users_conn
from app.templates_config import templates

router = APIRouter()

COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

# In-memory login lockout — a cheap brute-force friction, not a hardened rate limiter
# (resets on restart, per the plan's explicit v1 scope). {email: [timestamp, ...]}
_failed_logins: dict[str, list[float]] = {}
LOCKOUT_MAX_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 15 * 60


def _is_locked_out(email: str) -> bool:
    attempts = _failed_logins.get(email, [])
    cutoff = time.time() - LOCKOUT_WINDOW_SECONDS
    attempts = [t for t in attempts if t > cutoff]
    _failed_logins[email] = attempts
    return len(attempts) >= LOCKOUT_MAX_ATTEMPTS


def _record_failed_login(email: str) -> None:
    _failed_logins.setdefault(email, []).append(time.time())


def _set_session_cookie(response, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=30 * 24 * 3600,
    )


@router.get("/signup", response_class=HTMLResponse)
def signup_screen(request: Request, error: str | None = None):
    return templates.TemplateResponse("signup.html", {"request": request, "error": error})


@router.post("/signup")
def signup_submit(email: str = Form(...), password: str = Form(...), confirm_password: str = Form(...)):
    email = email.strip().lower()
    if len(password) < 8:
        return RedirectResponse("/signup?error=Password+must+be+at+least+8+characters", status_code=303)
    if password != confirm_password:
        return RedirectResponse("/signup?error=Passwords+don%27t+match", status_code=303)

    with get_users_conn() as u:
        existing = u.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        if existing and existing["password_hash"]:
            return RedirectResponse("/signup?error=An+account+with+this+email+already+exists", status_code=303)

        password_hash = hash_password(password)
        if existing:
            # Claims the pre-existing seeded row (see app/db.py's migration comment) —
            # preserves its tracked_positions/user_settings rather than erroring or
            # creating a duplicate account for the same email.
            user_id = existing["id"]
            u.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        else:
            cur = u.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, password_hash))
            user_id = cur.lastrowid
        u.commit()

    token = create_auth_token(user_id, "verify_email")
    send_verification_email(email, token)

    session_id = create_session(user_id)
    resp = RedirectResponse("/analytics", status_code=303)
    _set_session_cookie(resp, session_id)
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_screen(request: Request, error: str | None = None):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@router.post("/login")
def login_submit(email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    generic_error = "/login?error=Invalid+email+or+password"

    if _is_locked_out(email):
        return RedirectResponse("/login?error=Too+many+attempts+%E2%80%94+try+again+in+15+minutes", status_code=303)

    with get_users_conn() as u:
        user = u.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    if not user or not user["password_hash"] or not verify_password(user["password_hash"], password):
        _record_failed_login(email)
        return RedirectResponse(generic_error, status_code=303)

    session_id = create_session(user["id"])
    resp = RedirectResponse("/analytics", status_code=303)
    _set_session_cookie(resp, session_id)
    return resp


@router.post("/logout")
def logout(request: Request):
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        delete_session(session_id)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/verify-email")
def verify_email(token: str):
    user_id = consume_auth_token(token, "verify_email")
    if user_id is None:
        return RedirectResponse("/login?error=That+verification+link+is+invalid+or+expired", status_code=303)
    with get_users_conn() as u:
        u.execute("UPDATE users SET email_verified_at = ? WHERE id = ?", (datetime.now(timezone.utc).isoformat(), user_id))
        u.commit()
    return RedirectResponse("/analytics?verified=1", status_code=303)


@router.post("/resend-verification")
def resend_verification(request: Request):
    user = request.state.user
    if user and not user["email_verified_at"]:
        token = create_auth_token(user["id"], "verify_email")
        send_verification_email(user["email"], token)
    return RedirectResponse(request.headers.get("referer", "/analytics"), status_code=303)


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_screen(request: Request, sent: bool = False):
    return templates.TemplateResponse("forgot_password.html", {"request": request, "sent": sent})


@router.post("/forgot-password")
def forgot_password_submit(email: str = Form(...)):
    email = email.strip().lower()
    with get_users_conn() as u:
        user = u.execute("SELECT id, email FROM users WHERE email = ?", (email,)).fetchone()
    if user:
        token = create_auth_token(user["id"], "reset_password")
        send_reset_email(user["email"], token)
    # Same response whether or not the email exists — never reveals which emails have
    # an account.
    return RedirectResponse("/forgot-password?sent=true", status_code=303)


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_screen(request: Request, token: str, error: str | None = None):
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token, "error": error})


@router.post("/reset-password")
def reset_password_submit(token: str = Form(...), password: str = Form(...), confirm_password: str = Form(...)):
    if len(password) < 8:
        return RedirectResponse(f"/reset-password?token={token}&error=Password+must+be+at+least+8+characters", status_code=303)
    if password != confirm_password:
        return RedirectResponse(f"/reset-password?token={token}&error=Passwords+don%27t+match", status_code=303)

    user_id = consume_auth_token(token, "reset_password")
    if user_id is None:
        return RedirectResponse("/login?error=That+reset+link+is+invalid+or+expired", status_code=303)

    with get_users_conn() as u:
        u.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
        u.commit()
    delete_all_sessions(user_id)  # force re-login everywhere — see module docstring

    return RedirectResponse("/login?error=Password+reset+%E2%80%94+please+log+in", status_code=303)
