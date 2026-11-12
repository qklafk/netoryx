"""Webhook signature shared by ParityPay and Aurapay.

The JSON body is flattened: keys sorted alphabetically, values (``null`` as an empty string) joined without a
separator, and the string is signed with HMAC-SHA256 under the shop's second secret key. The hex digest travels in
the ``X-SIGNATURE`` header.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any

SIGNATURE_HEADER = "X-SIGNATURE"


def parse_body(raw: bytes) -> dict[str, Any]:
    """Decode a JSON object keeping numbers exact (floats become ``Decimal``). Raises ValueError."""
    data = json.loads(raw, parse_float=Decimal)
    if not isinstance(data, dict):
        raise ValueError("webhook body is not an object")
    return data


def _piece(value: Any, plain: bool) -> str:
    if value is None or value is False:
        return ""
    if value is True:
        return "1"
    if isinstance(value, Decimal):
        # ``plain`` renders the number the way a PHP script would print a float: 957.0 -> "957"
        return format(value.normalize(), "f") if plain else str(value)
    if isinstance(value, (str, int)):
        return str(value)
    raise ValueError("nested values cannot be signed")


def concatenated(data: dict[str, Any], *, plain: bool) -> str:
    return "".join(_piece(data[key], plain) for key in sorted(data))


def sign(secret: str, data: dict[str, Any], *, plain: bool = False) -> str:
    return hmac.new(secret.encode(), concatenated(data, plain=plain).encode(), hashlib.sha256).hexdigest()


def verify(secret: str, data: dict[str, Any], signature: str) -> bool:
    """True if ``signature`` matches the body under either rendering of non-integer numbers."""
    try:
        candidates = {sign(secret, data), sign(secret, data, plain=True)}
    except ValueError:
        return False
    given = signature.strip().lower().encode()
    return any(hmac.compare_digest(c.encode(), given) for c in candidates)
