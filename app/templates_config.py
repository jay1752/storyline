"""Single shared Jinja2Templates instance, so global helpers (like asset cache-busting)
are registered once and every router sees them — instead of each route file creating
its own Jinja2Templates() and needing the same setup repeated everywhere."""

import os

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")


def _asset_version(path: str) -> int:
    """mtime of a static file, used as a cache-busting query param — so editing CSS/JS
    during dev doesn't require a manual version bump or a hard-refresh to see it."""
    full_path = os.path.join("app", "static", path.lstrip("/"))
    try:
        return int(os.path.getmtime(full_path))
    except OSError:
        return 0


templates.env.globals["asset_version"] = _asset_version


def _mcap(v, market):
    """Market cap for display: India is stored in Rs crore, US in USD millions."""
    if v is None:
        return "—"
    if market == "IN":
        return f"₹{v / 1e5:.2f} lakh cr" if v >= 1e5 else f"₹{v:,.0f} cr"
    return f"${v / 1e6:.2f}T" if v >= 1e6 else f"${v / 1e3:.1f}B" if v >= 1e3 else f"${v:,.0f}M"


templates.env.filters["mcap"] = _mcap
