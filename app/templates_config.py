"""Single shared Jinja2Templates instance, so global helpers (like asset cache-busting)
are registered once and every router sees them — instead of each route file creating
its own Jinja2Templates() and needing the same setup repeated everywhere."""

import os
import re

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

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


_CITE = re.compile(r"\s*\[(S\d+(?:\s*,\s*S\d+)*)\]")


def _nocite(t):
    """Report text without its [S#] source tags (used where a tag would only clutter: dek, takeaways, leads, figure values)."""
    return _CITE.sub("", t or "").strip()


def _cites(t):
    """Report text with each [S#] tag shown as a small superscript linking to the source list."""
    out = str(escape(t or ""))
    return Markup(_CITE.sub(lambda m: "".join(f'<sup class="rs2-c"><a href="#src-{s}">{s}</a></sup>' for s in re.findall(r"S\d+", m.group(1))), out))


templates.env.filters["nocite"] = _nocite
templates.env.filters["cites"] = _cites


_OWN = re.compile(r"\b(the user|your (own )?(rules?|thresholds?|position|filters?)|user'?s)\b", re.I)
templates.env.tests["personalish"] = lambda t: bool(_OWN.search(t or ""))
