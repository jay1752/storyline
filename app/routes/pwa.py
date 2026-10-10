"""Installable-app plumbing: the web app manifest, the service worker (served from the site root so it can cover every page) and the
offline page. The worker caches only versioned static files; pages and data always come from the server (see app/static/sw.js)."""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.templates_config import templates

router = APIRouter()
_SW = Path(__file__).resolve().parents[1] / "static" / "sw.js"

MANIFEST = {
    "id": "/",
    "name": "Tenetfolio",
    "short_name": "Tenetfolio",
    "description": "Research any stock and check your portfolio against your own rules.",
    "start_url": "/analytics",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#F4F2EC",
    "theme_color": "#2B4A45",
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}


@router.get("/manifest.webmanifest")
def manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json", headers={"Cache-Control": "public, max-age=3600"})


@router.get("/sw.js")
def service_worker():
    """Never cached by the browser's HTTP cache, so a new worker is picked up on the next visit."""
    return Response(_SW.read_text(), media_type="application/javascript",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Service-Worker-Allowed": "/"})


@router.get("/offline", response_class=HTMLResponse)
def offline(request: Request):
    return templates.TemplateResponse("offline.html", {"request": request})
