"""The signed-in user's profile: name, phone, contact email and a few basics. The sign-in email itself is shown but not
editable here — it is the account's identity (and, for Google accounts, owned by Google), and changing it safely needs
email verification, which isn't set up yet. A separate contact email is editable."""

import re
import urllib.parse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_users_conn
from app.templates_config import templates

router = APIRouter()

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_PHONE = re.compile(r"^\+?[0-9]{8,15}$")
COUNTRIES = ["India", "United States", "United Kingdom", "Singapore", "United Arab Emirates", "Canada", "Australia", "Other"]


def _load(request: Request):
    with get_users_conn() as u:
        return u.execute("SELECT * FROM users WHERE id = ?", (request.state.user_id,)).fetchone()


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, saved: str | None = None, error: str | None = None):
    return templates.TemplateResponse("profile.html", {
        "request": request, "active_nav": "profile", "me": _load(request), "countries": COUNTRIES, "saved": saved, "error": error})


@router.post("/profile")
def profile_save(request: Request, display_name: str = Form(""), phone: str = Form(""), contact_email: str = Form(""),
                 country: str = Form(""), city: str = Form(""), about: str = Form("")):
    def fail(msg: str):
        return RedirectResponse("/profile?error=" + urllib.parse.quote(msg), status_code=303)

    name, city, about = display_name.strip()[:60], city.strip()[:60], about.strip()[:300]
    phone = re.sub(r"[\s\-().]", "", phone.strip())
    contact_email = contact_email.strip().lower()[:120]
    if phone and not _PHONE.match(phone):
        return fail("Phone number must be 8 to 15 digits, optionally starting with + and your country code")
    if contact_email and not _EMAIL.match(contact_email):
        return fail("That contact email doesn't look right")
    if country and country not in COUNTRIES:
        return fail("Pick a country from the list")
    with get_users_conn() as u:
        u.execute("UPDATE users SET display_name=?, phone=?, contact_email=?, country=?, city=?, about=? WHERE id=?",
                  (name or None, phone or None, contact_email or None, country or None, city or None, about or None, request.state.user_id))
        u.commit()
    return RedirectResponse("/profile?saved=1", status_code=303)
