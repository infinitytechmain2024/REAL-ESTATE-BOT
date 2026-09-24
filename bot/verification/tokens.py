"""Opaque secrets for links and cookies; only their SHA-256 is ever stored."""

from __future__ import annotations

import hashlib
import secrets

TOKEN_BYTES = 32  # 256 bits: guessing is not a realistic path


def new_secret() -> tuple[str, str]:
    """Return ``(secret, sha256_hex)``. The secret goes to the user, the hash to the database."""
    secret = secrets.token_urlsafe(TOKEN_BYTES)
    return secret, digest(secret)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def looks_like_secret(value: str) -> bool:
    """Cheap shape check before touching the database (43 url-safe characters)."""
    return len(value) == 43 and all(c.isalnum() or c in "-_" for c in value)
