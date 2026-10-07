"""AI settings: choose a provider and model (OpenAI, Claude, Gemini, Groq, Mistral, OpenRouter, xAI,
DeepSeek, local Ollama), keep one key per provider, optionally set a fallback model, and test it.

Keys are write-only: the page only ever learns WHETHER a key is saved for a provider, never the key."""

import re
import urllib.parse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.ai import errors
from app.ai.providers import (
    PROVIDERS,
    delete_user_key,
    get_user_key,
    get_user_settings,
    list_live_models,
    model_label,
    platform_available,
    public_registry,
    resolve_api_key,
    saved_key_providers,
    set_user_key,
    test_connection,
)
from app.db import get_users_conn
from app.templates_config import templates

router = APIRouter()

_MODEL_ID = re.compile(r"^[A-Za-z0-9._:/@+\-]{1,120}$")


@router.get("/settings", response_class=HTMLResponse)
def settings_screen(request: Request, saved: str | None = None, error: str | None = None):
    user_id = request.state.user_id
    settings = get_user_settings(user_id)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "active_nav": "settings",
            "settings": settings,
            "registry": public_registry(),
            # Which providers already have a key stored — a boolean per provider, never the key itself.
            "saved_keys": sorted(saved_key_providers(user_id)),
            "saved": saved,
            "error": error,
        },
    )


@router.post("/settings")
async def settings_save(request: Request):
    user_id = request.state.user_id
    form = await request.form()

    def val(name: str) -> str:
        return (form.get(name) or "").strip()

    def fail(msg: str):
        return RedirectResponse("/settings?error=" + urllib.parse.quote(msg), status_code=303)

    provider = val("provider")
    model = val("custom_model") or val("model_name")
    if provider not in PROVIDERS:
        return fail("Pick a provider from the list.")
    if not _MODEL_ID.match(model):
        return fail("Model name is missing or has unusual characters.")

    fb_provider = val("fallback_provider") or None
    fb_model = (val("fallback_custom") or val("fallback_model")) if fb_provider else None
    if fb_provider and fb_provider not in PROVIDERS:
        return fail("Pick a fallback provider from the list.")
    if fb_provider and not _MODEL_ID.match(fb_model or ""):
        return fail("Fallback model name is missing or has unusual characters.")

    # Keys: only ever written, never read back to the page. A blank field keeps whatever is saved.
    for pid in form.getlist("remove_key"):
        if pid in PROVIDERS:
            delete_user_key(user_id, pid)
    api_key, fb_key = val("api_key"), val("fallback_api_key")
    if api_key:
        set_user_key(user_id, provider, api_key)
    if fb_provider and fb_key:
        set_user_key(user_id, fb_provider, fb_key)

    # "Use the shared server key" only makes sense where one exists, and never when a key was just pasted.
    use_shared = bool(form.get("use_platform_default")) and platform_available(provider) and not api_key

    with get_users_conn() as u:
        u.execute(
            """
            INSERT INTO user_settings (user_id, provider, model_name, use_platform_default, fallback_provider, fallback_model, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                provider = excluded.provider,
                model_name = excluded.model_name,
                use_platform_default = excluded.use_platform_default,
                fallback_provider = excluded.fallback_provider,
                fallback_model = excluded.fallback_model,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, provider, model, int(use_shared), fb_provider, fb_model),
        )
        u.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)


def _key_for(user_id: int, provider: str, typed: str, role: str = "primary") -> str | None:
    """The key to test/list with: what the user just typed, else what's saved/shared for that provider."""
    if typed.strip():
        return typed.strip()
    if not PROVIDERS[provider]["needs_key"]:
        return "local"
    # For a one-off check, prefer the user's own saved key, then the shared one.
    return get_user_key(user_id, provider) or resolve_api_key(user_id, provider, "fallback")


@router.post("/settings/models")
async def settings_models(request: Request, provider: str = Form(...), api_key: str = Form("")):
    """Live model list for a provider, using the typed or saved key. Falls back to the curated list on failure."""
    if provider not in PROVIDERS:
        return JSONResponse({"error": "Unknown provider."}, status_code=400)
    key = _key_for(request.state.user_id, provider, api_key)
    curated = PROVIDERS[provider]["models"]
    if not key and provider != "openrouter":
        return {"models": curated, "live": False, "message": "Add an API key first to load this provider's live model list."}
    try:
        models = await run_in_threadpool(list_live_models, provider, key)
    except Exception as e:  # noqa: BLE001 — surface a readable reason, keep the page usable with the curated list
        return {"models": curated, "live": False, "message": errors.friendly_error(e, PROVIDERS[provider]["label"].split(" (")[0])}
    if not models:
        return {"models": curated, "live": False, "message": "The provider returned no models; showing the usual ones."}
    return {"models": models, "live": True, "message": f"{len(models)} models available to this key."}


@router.post("/settings/test")
async def settings_test(request: Request, provider: str = Form(...), model: str = Form(...), api_key: str = Form("")):
    """One tiny real completion with the chosen provider/model/key, so mistakes show up here, not mid-chat."""
    if provider not in PROVIDERS:
        return JSONResponse({"ok": False, "message": "Unknown provider."}, status_code=400)
    model = model.strip()
    if not _MODEL_ID.match(model):
        return {"ok": False, "message": "Model name is missing or has unusual characters."}
    key = _key_for(request.state.user_id, provider, api_key)
    label = PROVIDERS[provider]["label"].split(" (")[0]
    if not key:
        return {"ok": False, "message": f"No API key for {label} yet — paste one first."}
    try:
        reply = await test_connection(provider, model, key)
    except Exception as e:  # noqa: BLE001
        if provider == "ollama" and "not found" in str(e).lower():
            return {"ok": False, "message": f"Ollama is running, but that model isn't downloaded yet. In a terminal run: ollama pull {model}"}
        return {"ok": False, "message": errors.friendly_error(e, f"{label} · {model_label(provider, model)}")}
    return {"ok": True, "message": f"Connected. {label} · {model_label(provider, model)} replied “{reply}”."}
