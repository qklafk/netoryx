"""Symmetric encryption of stored secrets (panel passwords, API tokens).

Values are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using a key derived from ``SECRET_KEY`` via
HKDF-SHA256, and stored as ``"enc:<fernet token>"``. Values without the prefix are treated as plaintext, so
databases created before a key was configured keep working.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PREFIX = "enc:"
_HKDF_INFO = b"vpn-bot/secretbox/v1"
_HKDF_SIGN_INFO = b"vpn-bot/secretbox/sign/v1"


class DecryptionError(ValueError):
    """The value carries the ``enc:`` prefix but cannot be decrypted with this key."""


def _derive(secret: str, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(secret.encode("utf-8"))


def _derive_fernet_key(secret: str) -> bytes:
    return base64.urlsafe_b64encode(_derive(secret, _HKDF_INFO))


def is_encrypted(value: str) -> bool:
    """True if ``value`` was produced by :meth:`SecretBox.encrypt`."""
    return value.startswith(PREFIX)


class SecretBox:
    """Encrypts/decrypts short strings with a key derived from the application secret."""

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("SecretBox needs a non-empty key")
        self._fernet = Fernet(_derive_fernet_key(key))
        self._sign_key = _derive(key, _HKDF_SIGN_INFO)

    def rekey(self, key: str) -> None:
        """Switch to another application secret (in place, so every holder of this box sees the new key)."""
        if not key:
            raise ValueError("SecretBox needs a non-empty key")
        self._fernet = Fernet(_derive_fernet_key(key))
        self._sign_key = _derive(key, _HKDF_SIGN_INFO)

    def __repr__(self) -> str:  # never leak key material
        return "SecretBox(<hidden>)"

    def encrypt(self, plaintext: str) -> str:
        """Return ``"enc:<token>"``. Already encrypted values are returned unchanged."""
        if is_encrypted(plaintext):
            return plaintext
        return PREFIX + self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        """Decrypt an ``enc:`` value; plaintext (no prefix) is returned as is.

        Raises :class:`DecryptionError` if the token is corrupt or was encrypted with another key.
        """
        if not is_encrypted(value):
            return value
        try:
            return self._fernet.decrypt(value[len(PREFIX) :].encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as e:
            raise DecryptionError("cannot decrypt value: wrong SECRET_KEY or corrupted data") from e

    def sign(self, message: str) -> str:
        """Deterministic HMAC-SHA256 of ``message`` (hex) under a key of its own derived from the secret."""
        return hmac.new(self._sign_key, message.encode("utf-8"), hashlib.sha256).hexdigest()
