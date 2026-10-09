"""Verification/reset emails via Resend's REST API (no SDK needed — `requests` is
already a dependency). Degrades gracefully when RESEND_API_KEY isn't set: logs the full
link to stdout instead of raising, so signup/login/reset are fully testable locally with
zero email infrastructure (check `docker logs storyline-app-1` for the link). This is
deliberately NOT the same NotConfiguredError-and-fail pattern the ingestion sources use —
email here is best-effort, not something that should block signup/login from working."""

import logging
import os

import requests

logger = logging.getLogger("app.auth.email")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM = os.environ.get("RESEND_FROM", "Stocktale <onboarding@resend.dev>")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")


def _send(to: str, subject: str, html: str, fallback_log_label: str) -> None:
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — %s for %s:\n%s", fallback_log_label, to, html)
        return
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": RESEND_FROM, "to": [to], "subject": subject, "html": html},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        # Best-effort: a failed send should never break signup/login/reset — the token
        # still exists in the DB, just log it the same way the no-key case does.
        logger.warning("Resend send failed (%s) — %s for %s:\n%s", e, fallback_log_label, to, html)


def send_verification_email(email: str, token: str) -> None:
    url = f"{APP_BASE_URL}/verify-email?token={token}"
    _send(
        email,
        "Verify your Stocktale account",
        f'<p>Click to verify your email:</p><p><a href="{url}">{url}</a></p>'
        f"<p>This link expires in 24 hours.</p>",
        "verification link",
    )


def send_reset_email(email: str, token: str) -> None:
    url = f"{APP_BASE_URL}/reset-password?token={token}"
    _send(
        email,
        "Reset your Stocktale password",
        f'<p>Click to set a new password:</p><p><a href="{url}">{url}</a></p>'
        f"<p>This link expires in 1 hour. If you didn't request this, ignore this email.</p>",
        "password reset link",
    )
