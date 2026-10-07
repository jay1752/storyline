"""Password hashing — Argon2id via argon2-cffi, OWASP's current recommendation (better
resistance to GPU/ASIC cracking than bcrypt). Verified live before writing this:
PasswordHasher() defaults to the argon2id variant, not argon2i/argon2d."""

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(password_hash: str, plaintext: str) -> bool:
    try:
        return _hasher.verify(password_hash, plaintext)
    except VerifyMismatchError:
        return False
