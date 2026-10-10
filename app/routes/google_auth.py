"""Sign in with Google (OpenID Connect, authorization-code flow + PKCE). No new dependency: the code is
exchanged server-to-server over TLS, and an id_token received directly from Google's token endpoint is trusted
for its claims once iss/aud/exp/nonce/email_verified are checked (OIDC Core 3.1.3.7).

Account rules:
  - A returning Google identity (sub) signs straight in.
  - A Google-verified email matching an existing account links to it. If that account's email was NEVER verified,
    its password is wiped and its sessions revoked first: someone may have pre-registered the address to squat on it,
    and the real owner proving the address via Google must not inherit a password a stranger knows.
  - Otherwise a new, already-verified account is created (no password)."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.auth.tokens import create_session
from app.db import get_users_conn
from app.routes.auth import _set_session_cookie

router = APIRouter()

CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
STATE_COOKIE = "g_oauth"


def google_enabled() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def _redirect_uri() -> str:
    return f"{APP_BASE_URL}/auth/google/callback"


def _fail(msg: str) -> RedirectResponse:
    resp = RedirectResponse("/login?" + urlencode({"error": msg}), status_code=303)
    resp.delete_cookie(STATE_COOKIE, path="/auth/google")
    return resp


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _claims(id_token: str) -> dict:
    payload = id_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


@router.get("/auth/google")
def google_start():
    if not google_enabled():
        return _fail("Google sign-in isn't set up yet.")
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    params = {
        "client_id": CLIENT_ID, "redirect_uri": _redirect_uri(), "response_type": "code", "scope": "openid email profile",
        "state": state, "nonce": nonce, "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256", "prompt": "select_account",
    }
    resp = RedirectResponse(f"{AUTH_URL}?{urlencode(params)}", status_code=303)
    resp.set_cookie(STATE_COOKIE, f"{state}.{nonce}.{verifier}", max_age=600, httponly=True, secure=COOKIE_SECURE,
                    samesite="lax", path="/auth/google")
    return resp


@router.get("/auth/google/callback")
def google_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code or not state:
        return _fail("Google sign-in was cancelled.")
    parts = (request.cookies.get(STATE_COOKIE) or "").split(".")
    if len(parts) != 3 or not hmac.compare_digest(parts[0], state):
        return _fail("That sign-in attempt expired. Please try again.")
    _, nonce, verifier = parts
    try:
        r = requests.post(TOKEN_URL, timeout=15, data={
            "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code", "code_verifier": verifier})
        r.raise_for_status()
        c = _claims(r.json()["id_token"])
    except (requests.RequestException, KeyError, ValueError, IndexError):
        return _fail("Google sign-in failed. Please try again.")

    email = (c.get("email") or "").strip().lower()
    if (c.get("iss") not in ISSUERS or c.get("aud") != CLIENT_ID or float(c.get("exp", 0)) < time.time()
            or not hmac.compare_digest(str(c.get("nonce", "")), nonce) or not c.get("sub") or not email):
        return _fail("Google sign-in couldn't be verified.")
    if c.get("email_verified") not in (True, "true"):
        return _fail("Your Google email isn't verified, so we can't use it.")

    now = datetime.now(timezone.utc).isoformat()
    name = (c.get("name") or "").strip()[:60] or None
    is_new = False
    with get_users_conn() as u:
        user = u.execute("SELECT * FROM users WHERE google_sub = ?", (c["sub"],)).fetchone()
        if user is None:
            user = u.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if user is not None:
                if user["google_sub"] and user["google_sub"] != c["sub"]:
                    return _fail("This email is already linked to a different Google account.")
                if not user["email_verified_at"]:  # possible squatter: remove any password they set, drop their sessions
                    u.execute("UPDATE users SET password_hash = NULL WHERE id = ?", (user["id"],))
                    u.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
                u.execute("UPDATE users SET google_sub = ?, email_verified_at = COALESCE(email_verified_at, ?), "
                          "display_name = COALESCE(display_name, ?) WHERE id = ?", (c["sub"], now, name, user["id"]))
                user_id = user["id"]
            else:
                user_id = u.execute("INSERT INTO users (email, google_sub, email_verified_at, display_name) VALUES (?,?,?,?)",
                                    (email, c["sub"], now, name)).lastrowid
                is_new = True
        else:
            user_id = user["id"]
        u.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))  # housekeeping: expired sessions were never purged
        u.commit()

    resp = RedirectResponse("/welcome" if is_new else "/analytics", status_code=303)  # a brand-new account gets the getting-started page
    resp.delete_cookie(STATE_COOKIE, path="/auth/google")
    _set_session_cookie(resp, create_session(user_id))
    return resp
