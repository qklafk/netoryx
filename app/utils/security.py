"""Cryptographically secure random identifiers and constant-time comparison."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string

# Upper-case letters and digits without look-alikes (0/O, 1/I/L) - safe to read aloud and retype.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
SUB_ID_ALPHABET = string.ascii_lowercase + string.digits
SUB_ID_LENGTH = 16


def random_token(nbytes: int = 24) -> str:
    """URL-safe random token with ``nbytes`` of entropy (24 bytes -> 32 characters)."""
    if nbytes < 1:
        raise ValueError("token length must be positive")
    return secrets.token_urlsafe(nbytes)


def random_code(n: int = 8) -> str:
    """Human-friendly random code of ``n`` characters from :data:`CODE_ALPHABET`."""
    if n < 1:
        raise ValueError("code length must be positive")
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(n))


def random_sub_id() -> str:
    """Random 16-character ``[a-z0-9]`` id (3x-ui ``subId`` format)."""
    return "".join(secrets.choice(SUB_ID_ALPHABET) for _ in range(SUB_ID_LENGTH))


def sha256_hex(s: str) -> str:
    """Hex SHA-256 of the UTF-8 encoding of ``s``."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def const_eq(a: str | bytes, b: str | bytes) -> bool:
    """Constant-time equality for secrets (str is compared by its UTF-8 bytes)."""
    a_bytes = a.encode("utf-8") if isinstance(a, str) else a
    b_bytes = b.encode("utf-8") if isinstance(b, str) else b
    return hmac.compare_digest(a_bytes, b_bytes)
