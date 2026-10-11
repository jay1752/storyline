"""Public privacy policy and terms (linked from the home page, the sign-in pages and Google's consent screen)."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templates_config import templates

router = APIRouter()
UPDATED = "11 October 2026"
CONTACT = "tenetfolio@gmail.com"


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse("legal_privacy.html", {"request": request, "updated": UPDATED, "contact": CONTACT})


@router.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse("legal_terms.html", {"request": request, "updated": UPDATED, "contact": CONTACT})
