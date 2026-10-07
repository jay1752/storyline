"""Turns provider/library exceptions into one readable sentence, and classifies them for the
retry / fallback logic. Every provider (Gemini, Claude, OpenAI and the OpenAI-compatible ones)
reports quota, billing and overload differently — status 429 vs 400 vs 402, different JSON
shapes — so the matching is done on both the status code and the message text. The raw
exception text is never shown to a user: it can be a whole JSON body from the provider."""

from __future__ import annotations


def _text(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def _code(e: Exception):
    """HTTP status from either a provider-SDK exception (.status_code) or a `requests` one (.response.status_code)."""
    return getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)


def is_transient(e: Exception) -> bool:
    """Overloaded / unavailable — usually succeeds on a quick retry of the SAME model."""
    t = _text(e)
    return _code(e) in (500, 502, 503, 504) or "UNAVAILABLE" in t or "overloaded" in t.lower() or "high demand" in t.lower()


def is_quota(e: Exception) -> bool:
    """Out of free quota / credits / rate limit — retrying the same model won't help, a different one might."""
    t = _text(e).lower()
    return (
        _code(e) in (402, 429)
        or "credit balance is too low" in t
        or "resource_exhausted" in t
        or "insufficient_quota" in t
        or "exceeded your current quota" in t
        or "rate limit" in t
    )


def friendly_error(e: Exception, label: str | None = None) -> str:
    """`label` is the human provider/model name, used to say WHICH one failed."""
    who = f"{label} " if label else "The AI model "
    t = _text(e)
    tl = t.lower()
    code = _code(e)
    if "credit balance is too low" in tl:
        return (f"{who}account is out of credits. Add credits at the provider's billing page, "
                "or pick another model in AI settings.")
    if is_quota(e):
        return (f"{who}has hit its usage limit (free tiers reset daily). Try again later, pick another model in "
                "AI settings, or set a fallback model so chat keeps working.")
    if code in (401, 403) or "api key" in tl or "api_key" in tl or "invalid x-api-key" in tl or "authentication" in tl or "permission_denied" in tl:
        return f"{who}rejected the API key. Check the key in AI settings."
    if code == 404 or "model_not_found" in tl or "not found" in tl and "model" in tl:
        return f"{who}doesn't have that model available for this key. Pick another model in AI settings."
    if is_transient(e):
        return f"{who}is busy right now. Try again in a moment."
    if "connect" in tl or "timeout" in tl or "timed out" in tl or "name resolution" in tl:
        return f"Couldn't reach {label or 'the AI provider'} (connection problem). Check your network or the provider's status."
    return "Something went wrong while generating that answer. Try again, or pick another model in AI settings."
