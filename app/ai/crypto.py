"""Encrypts a user's own BYOK LLM API key before it's stored in users.db — the first
time this app has ever stored a secret anywhere but an env var (see GEMINI_API_KEY in
app/ai/providers.py for the established "env var, module-level constant" convention every other
secret in this codebase follows). A plaintext key sitting in SQLite would be a real step
down in this app's existing security posture, so this wraps `cryptography`'s Fernet
(symmetric, authenticated encryption) with a single server-side master key.

APP_MASTER_KEY is read once at import time, same pattern as every other env-var secret
here. Losing or rotating it makes every previously-saved BYOK key permanently
undecryptable — there's no recovery path, so this is deliberately a loud failure
(MasterKeyNotConfiguredError) rather than silently falling back to plaintext."""

import os

from cryptography.fernet import Fernet, InvalidToken

_MASTER_KEY = os.environ.get("APP_MASTER_KEY")


class MasterKeyNotConfiguredError(Exception):
    """Raised when APP_MASTER_KEY isn't set — distinct from
    app.ingestion.base.NotConfiguredError, which means "a provider's API key is
    missing," a different failure domain from "the server has no way to encrypt
    anything at all.\""""


def _fernet() -> Fernet:
    if not _MASTER_KEY:
        raise MasterKeyNotConfiguredError(
            "APP_MASTER_KEY not set — generate one with: python3 -c \"import secrets,base64; "
            "print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\" and add it to .env"
        )
    return Fernet(_MASTER_KEY)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        # Wrong/rotated APP_MASTER_KEY, or the stored value was never actually a
        # Fernet token — either way, never half-decrypt or guess, surface it clearly.
        raise MasterKeyNotConfiguredError(
            "Could not decrypt the stored API key — APP_MASTER_KEY may have changed since it was saved."
        ) from e
