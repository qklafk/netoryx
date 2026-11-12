"""RSA key loading and PKCS#1 v1.5 signatures for providers that sign with RSA.

Keys are accepted as PEM (any RSA label, PKCS#1 or PKCS#8 / SubjectPublicKeyInfo inside) or as the
bare Base64 DER some merchant dashboards hand out. Values pasted into a single-line environment
variable may carry literal ``\\n`` sequences instead of line breaks; they are tolerated too.
"""

from __future__ import annotations

import base64
import binascii

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def _der(text: str) -> bytes:
    """Strip PEM armour and whitespace, then decode the Base64 body. Raises ValueError."""
    normalized = text.replace("\\r", "\n").replace("\\n", "\n")
    body = "".join(line.strip() for line in normalized.splitlines() if not line.strip().startswith("-----"))
    body = "".join(body.split())
    if not body:
        raise ValueError("empty RSA key")
    try:
        return base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("RSA key is not valid Base64 / PEM") from exc


def load_private_key(text: str) -> rsa.RSAPrivateKey:
    """Parse an RSA private key (PKCS#1 or PKCS#8). Raises ValueError."""
    try:
        key = serialization.load_der_private_key(_der(text), password=None)
    except (TypeError, UnsupportedAlgorithm) as exc:
        raise ValueError("cannot load RSA private key") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("private key is not an RSA key")
    return key


def load_public_key(text: str) -> rsa.RSAPublicKey:
    """Parse an RSA public key (SubjectPublicKeyInfo or PKCS#1). Raises ValueError."""
    try:
        key = serialization.load_der_public_key(_der(text))
    except UnsupportedAlgorithm as exc:
        raise ValueError("cannot load RSA public key") from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("public key is not an RSA key")
    return key


def sign(key: rsa.RSAPrivateKey, data: bytes, algorithm: hashes.HashAlgorithm) -> str:
    """Base64 PKCS#1 v1.5 signature of ``data``."""
    return base64.b64encode(key.sign(data, padding.PKCS1v15(), algorithm)).decode()


def verify(key: rsa.RSAPublicKey, data: bytes, signature_b64: str | None, algorithm: hashes.HashAlgorithm) -> bool:
    """True if ``signature_b64`` is a valid Base64 PKCS#1 v1.5 signature of ``data``."""
    if not signature_b64:
        return False
    try:
        raw = base64.b64decode("".join(signature_b64.split()), validate=True)
    except (binascii.Error, ValueError):
        return False
    try:
        key.verify(raw, data, padding.PKCS1v15(), algorithm)
    except InvalidSignature:
        return False
    return True
