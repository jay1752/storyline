"""The model-provider registry and everything that turns "provider + model + key" into a
pydantic_ai model.

Four wire protocols cover every provider offered in AI settings:
  - kind "google"    → Gemini (GoogleModel)
  - kind "anthropic" → Claude (AnthropicModel)
  - kind "openai"    → OpenAI itself AND every OpenAI-compatible endpoint (Groq, Mistral, OpenRouter,
                       xAI, DeepSeek, a local Ollama): OpenAIChatModel pointed at a different base_url.
Adding another OpenAI-compatible provider is a registry entry, not new code.

Keys: each user stores ONE key per provider (user_provider_keys, Fernet-encrypted — see
crypto.py). The server's own env key is a shared "platform default" ONLY for providers flagged
`platform` (Gemini, and the local Ollama which needs no key). Claude and the rest are
bring-your-own-key on purpose: a shared env key would silently spend the owner's money for
every user.

Model lists are curated here (verified IDs where we could check them) but the settings page can
also pull the LIVE list from a provider with the user's own key, because model names change
faster than this file does — and any model ID can be typed in by hand."""

from __future__ import annotations

import os

import requests

from app.ai.crypto import decrypt, encrypt
from app.db import get_users_conn


def _models(*pairs):
    return [{"id": i, "label": l} for i, l in pairs]


OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1")

PROVIDERS: dict[str, dict] = {
    "gemini": {
        "label": "Google Gemini",
        "kind": "google",
        "env_var": "GEMINI_API_KEY",
        "platform": True,
        "needs_key": True,
        "key_url": "https://aistudio.google.com/apikey",
        "files": True,  # reads PDFs / images natively
        "free": "Free tier: about 20 requests/day per model on a new key (each model has its own allowance).",
        "default_model": os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview"),
        "models": _models(
            ("gemini-3-flash-preview", "Gemini 3 Flash (preview)"),
            ("gemini-3.8-flash", "Gemini 3.8 Flash"),
            ("gemini-3.7-flash", "Gemini 3.7 Flash"),
            ("gemini-3.5-flash", "Gemini 3.5 Flash"),
            ("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite"),
            ("gemini-3.1-pro-preview", "Gemini 3.1 Pro (preview)"),
            ("gemini-flash-latest", "Gemini Flash (latest alias)"),
            ("gemini-pro-latest", "Gemini Pro (latest alias)"),
        ),
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "kind": "anthropic",
        "env_var": "ANTHROPIC_API_KEY",
        "platform": False,
        "needs_key": True,
        "key_url": "https://console.anthropic.com/settings/keys",
        "files": True,
        "free": None,
        "default_model": "claude-sonnet-5-5",
        "models": _models(
            ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
            ("claude-opus-5-5", "Claude Opus 5.5"),
            ("claude-fable-5-1", "Claude Fable 5.1"),
            ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
        ),
    },
    "openai": {
        "label": "OpenAI",
        "kind": "openai",
        "base_url": None,
        "env_var": "OPENAI_API_KEY",
        "platform": False,
        "needs_key": True,
        "key_url": "https://platform.openai.com/api-keys",
        "files": True,
        "free": None,
        "default_model": "gpt-5",
        "models": _models(
            ("gpt-5", "GPT-5"),
            ("gpt-5-mini", "GPT-5 mini"),
            ("gpt-4.1", "GPT-4.1"),
            ("gpt-4o", "GPT-4o"),
            ("o4-mini", "o4-mini"),
        ),
    },
    "groq": {
        "label": "Groq",
        "kind": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "env_var": "GROQ_API_KEY",
        "platform": False,
        "needs_key": True,
        "key_url": "https://console.groq.com/keys",
        "files": False,  # text only — no PDF/image reading
        "free": "Free, no card: about 1,000 requests/day and 200K tokens/day per model. Very fast.",
        "default_model": "openai/gpt-oss-120b",
        "models": _models(
            ("openai/gpt-oss-120b", "GPT-OSS 120B"),
            ("openai/gpt-oss-20b", "GPT-OSS 20B"),
            ("qwen/qwen3.8-27b", "Qwen 3.8 27B"),
            ("llama-3.3-70b-versatile", "Llama 3.3 70B"),
        ),
    },
    "mistral": {
        "label": "Mistral",
        "kind": "openai",
        "base_url": "https://api.mistral.ai/v1",
        "env_var": "MISTRAL_API_KEY",
        "platform": False,
        "needs_key": True,
        "key_url": "https://console.mistral.ai/api-keys",
        "files": False,
        "free": "Free 'Experiment' plan: about 1 billion tokens/month. Needs phone verification; prompts may be used for training.",
        "default_model": "mistral-large-latest",
        "models": _models(
            ("mistral-large-latest", "Mistral Large"),
            ("mistral-medium-latest", "Mistral Medium"),
            ("mistral-small-latest", "Mistral Small"),
        ),
    },
    "openrouter": {
        "label": "OpenRouter (hundreds of models)",
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "env_var": "OPENROUTER_API_KEY",
        "platform": False,
        "needs_key": True,
        "key_url": "https://openrouter.ai/keys",
        "files": False,
        "free": "Many models are free (names end in :free): 20 requests/min, 50/day (1,000/day after a one-time $10 top-up).",
        "default_model": "nvidia/nemotron-3-super-120b-a12b:free",
        "models": _models(
            ("nvidia/nemotron-3-super-120b-a12b:free", "Nemotron 3 Super 120B (free)"),
            ("nvidia/nemotron-3-ultra-550b-a55b:free", "Nemotron 3 Ultra 550B (free)"),
            ("google/gemma-4-31b-it:free", "Gemma 4 31B (free)"),
            ("thinkingmachines/inkling:free", "Inkling (free, 1M context)"),
            ("openrouter/free", "Auto-pick a free model"),
        ),
    },
    "xai": {
        "label": "xAI Grok",
        "kind": "openai",
        "base_url": "https://api.x.ai/v1",
        "env_var": "XAI_API_KEY",
        "platform": False,
        "needs_key": True,
        "key_url": "https://console.x.ai",
        "files": False,
        "free": None,
        "default_model": "grok-4",
        "models": _models(("grok-4", "Grok 4")),
    },
    "deepseek": {
        "label": "DeepSeek",
        "kind": "openai",
        "base_url": "https://api.deepseek.com",
        "env_var": "DEEPSEEK_API_KEY",
        "platform": False,
        "needs_key": True,
        "key_url": "https://platform.deepseek.com/api_keys",
        "files": False,
        "free": None,
        "default_model": "deepseek-chat",
        "models": _models(("deepseek-chat", "DeepSeek Chat (V3)"), ("deepseek-reasoner", "DeepSeek Reasoner (R1)")),
    },
    "ollama": {
        "label": "Ollama (runs on your own computer)",
        "kind": "openai",
        "base_url": OLLAMA_BASE_URL,
        "env_var": None,
        "platform": True,
        "needs_key": False,
        "key_url": "https://ollama.com/download",
        "files": False,
        "free": "Free and unlimited, but small models only on an 8 GB laptop — weak at tool use. Needs Ollama running.",
        "default_model": "qwen3:4b",
        "models": _models(("qwen3:4b", "Qwen 3 4B"), ("llama3.2:3b", "Llama 3.2 3B"), ("gemma3:4b", "Gemma 3 4B")),
    },
}

DEFAULT_PROVIDER = "gemini"


def public_registry() -> dict:
    """What the settings page may see: everything except env var names."""
    out = {}
    for pid, p in PROVIDERS.items():
        out[pid] = {
            "label": p["label"], "key_url": p["key_url"], "free": p["free"], "files": p["files"],
            "needs_key": p["needs_key"], "platform_available": platform_available(pid),
            "default_model": p["default_model"], "models": p["models"],
        }
    return out


def platform_available(provider: str) -> bool:
    """Is there a shared server-side key for this provider (and is it configured)?"""
    p = PROVIDERS[provider]
    if not p["platform"]:
        return False
    return (not p["needs_key"]) or bool(p["env_var"] and os.environ.get(p["env_var"]))


def model_label(provider: str, model_id: str) -> str:
    """'Claude Sonnet 5.5' style label for chat/error messages; falls back to the raw model ID."""
    for m in PROVIDERS.get(provider, {}).get("models", []):
        if m["id"] == model_id:
            return m["label"]
    return model_id


# ---------------------------------------------------------------- settings + keys

def get_user_settings(user_id: int) -> dict:
    """The user's row from user_settings, or defaults if they never saved one."""
    with get_users_conn() as conn:
        row = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
    if row:
        d = dict(row)
        d.setdefault("fallback_provider", None)
        d.setdefault("fallback_model", None)
        return d
    return {
        "user_id": user_id,
        "provider": DEFAULT_PROVIDER,
        "model_name": PROVIDERS[DEFAULT_PROVIDER]["default_model"],
        "use_platform_default": 1,
        "encrypted_api_key": None,
        "fallback_provider": None,
        "fallback_model": None,
    }


def get_user_key(user_id: int, provider: str) -> str | None:
    with get_users_conn() as conn:
        row = conn.execute(
            "SELECT encrypted_api_key FROM user_provider_keys WHERE user_id = ? AND provider = ?", (user_id, provider)
        ).fetchone()
    return decrypt(row["encrypted_api_key"]) if row else None


def saved_key_providers(user_id: int) -> set[str]:
    with get_users_conn() as conn:
        return {r["provider"] for r in conn.execute("SELECT provider FROM user_provider_keys WHERE user_id = ?", (user_id,))}


def set_user_key(user_id: int, provider: str, key: str) -> None:
    with get_users_conn() as conn:
        conn.execute(
            """INSERT INTO user_provider_keys (user_id, provider, encrypted_api_key, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(user_id, provider) DO UPDATE SET encrypted_api_key = excluded.encrypted_api_key,
                                                           updated_at = CURRENT_TIMESTAMP""",
            (user_id, provider, encrypt(key.strip())),
        )
        conn.commit()


def delete_user_key(user_id: int, provider: str) -> None:
    with get_users_conn() as conn:
        conn.execute("DELETE FROM user_provider_keys WHERE user_id = ? AND provider = ?", (user_id, provider))
        conn.commit()


def resolve_api_key(user_id: int, provider: str, role: str = "primary") -> str | None:
    """The key to use for `provider`.

    primary: the user's "use the platform's key" checkbox decides between the shared server key
             (only for providers that have one) and their own saved key.
    fallback: their own key if they saved one, else the shared key if the provider has one.
    Returns None when no usable key exists (caller reports "no API key configured")."""
    info = PROVIDERS[provider]
    if not info["needs_key"]:
        return "local"  # Ollama ignores the key but the client library insists on a non-empty string
    own = get_user_key(user_id, provider)
    shared = os.environ.get(info["env_var"]) if platform_available(provider) else None
    if role == "primary":
        want_shared = bool(get_user_settings(user_id)["use_platform_default"])
        return (shared if want_shared and shared else own)
    return own or shared


def make_model(provider: str, model_name: str, api_key: str):
    """Construct the pydantic_ai model for provider+model with an explicit key."""
    info = PROVIDERS[provider]
    kind = info["kind"]
    if kind == "google":
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        return GoogleModel(model_name, provider=GoogleProvider(api_key=api_key))
    if kind == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        return AnthropicModel(model_name, provider=AnthropicProvider(api_key=api_key))
    if kind == "openai":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        return OpenAIChatModel(model_name, provider=OpenAIProvider(base_url=info.get("base_url"), api_key=api_key))
    raise ValueError(f"unsupported provider kind: {kind}")


def build_model(user_id: int, provider: str, model_name: str, role: str = "primary"):
    """The configured model, or None if no key is available (callers raise their own 'not configured')."""
    if provider not in PROVIDERS:
        return None
    key = resolve_api_key(user_id, provider, role)
    if not key:
        return None
    return make_model(provider, model_name, key)


def build_chain(user_id: int) -> list[dict]:
    """[primary, fallback?] — each {provider, model_name, label, model, files}. A missing key drops that link;
    the list can be empty (nothing configured)."""
    s = get_user_settings(user_id)
    chain = []
    for role, prov, mod in (("primary", s["provider"], s["model_name"]),
                            ("fallback", s.get("fallback_provider"), s.get("fallback_model"))):
        if not prov or not mod or prov not in PROVIDERS:
            continue
        if role == "fallback" and prov == s["provider"] and mod == s["model_name"]:
            continue
        m = build_model(user_id, prov, mod, role)
        if m is not None:
            chain.append({
                "provider": prov, "model_name": mod, "role": role, "model": m,
                "label": f"{PROVIDERS[prov]['label'].split(' (')[0]} · {model_label(prov, mod)}",
                "files": PROVIDERS[prov]["files"],
            })
    return chain


# ---------------------------------------------------------------- live model lists + connection test

_NON_CHAT = ("embed", "whisper", "tts", "transcribe", "dall-e", "moderation", "image", "audio", "realtime",
             "orpheus", "guard", "rerank", "ocr", "speech", "imagen", "veo", "lyria", "robotics", "computer-use",
             "omni", "nano-banana", "banana", "live", "native-audio", "customtools")


def list_live_models(provider: str, api_key: str | None) -> list[dict]:
    """Ask the provider which models this key can use. Raises on network/auth errors (caller maps them)."""
    info = PROVIDERS[provider]
    kind = info["kind"]
    if kind == "google":
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                         headers={"x-goog-api-key": api_key}, timeout=25)
        r.raise_for_status()
        out = []
        for m in r.json().get("models", []):
            mid = m["name"].split("/")[-1]
            if "generateContent" in m.get("supportedGenerationMethods", []) and mid.startswith("gemini") \
                    and not any(x in mid for x in _NON_CHAT):
                out.append({"id": mid, "label": m.get("displayName") or mid})
        return sorted(out, key=lambda x: x["id"], reverse=True)
    if kind == "anthropic":
        r = requests.get("https://api.anthropic.com/v1/models?limit=100",
                         headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"}, timeout=25)
        r.raise_for_status()
        return [{"id": m["id"], "label": m.get("display_name") or m["id"]} for m in r.json().get("data", [])]
    if provider == "ollama":
        base = info["base_url"].rstrip("/")
        base = base[:-3] if base.endswith("/v1") else base
        r = requests.get(base + "/api/tags", timeout=8)
        r.raise_for_status()
        return [{"id": m["name"], "label": m["name"]} for m in r.json().get("models", [])]
    # OpenAI and OpenAI-compatible
    base = (info.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = requests.get(base + "/models", headers=headers, timeout=25)
    r.raise_for_status()
    data = r.json().get("data", [])
    if provider == "openrouter":
        out = []
        for m in data:
            mid = m["id"]
            free = str((m.get("pricing") or {}).get("prompt")) in ("0", "0.0")
            famous = mid.split("/")[0] in ("anthropic", "openai", "google", "meta-llama", "mistralai", "x-ai", "deepseek", "qwen", "nvidia")
            if free or famous:
                out.append({"id": mid, "label": mid + (" (free)" if free else ""), "_free": free})
        out.sort(key=lambda x: (not x["_free"], x["id"]))
        return [{"id": m["id"], "label": m["label"]} for m in out][:150]
    return sorted(({"id": m["id"], "label": m["id"]} for m in data if not any(x in m["id"].lower() for x in _NON_CHAT)),
                  key=lambda x: x["id"])


async def test_connection(provider: str, model_name: str, api_key: str) -> str:
    """One tiny real completion. Returns the model's reply; raises on any provider error."""
    import asyncio

    from pydantic_ai import Agent

    agent = Agent(model=make_model(provider, model_name, api_key))
    result = await asyncio.wait_for(agent.run("Reply with the single word: ok"), timeout=60)
    return str(result.output).strip()[:80]


def platform_chain() -> list[dict]:
    """Models for SYSTEM jobs (no user involved, e.g. the daily community briefing): shared platform keys only.
    Override with COMMUNITY_AI_MODELS="gemini:gemini-3-flash-preview,gemini:gemini-3.1-flash-lite". The default
    spreads over several Gemini models because each has its own free-tier daily allowance, and the job needs
    only one request a day."""
    spec = os.environ.get("COMMUNITY_AI_MODELS") or "gemini:gemini-3-flash-preview,gemini:gemini-3.8-flash,gemini:gemini-3.1-flash-lite"
    chain = []
    for item in spec.split(","):
        prov, _, mod = item.strip().partition(":")
        info = PROVIDERS.get(prov)
        if not info or not mod or not info["needs_key"] or not platform_available(prov):
            continue
        chain.append({"provider": prov, "model_name": mod, "role": "system",
                      "label": f"{info['label'].split(' (')[0]} · {model_label(prov, mod)}",
                      "model": make_model(prov, mod, os.environ[info["env_var"]])})
    return chain
