"""Shadowsocks-2022 on 3x-ui ``shadowsocks`` inbounds: methods and per-subscription keys.

A multi-user SS-2022 inbound has one server key (``settings.password``, shared by everybody) and one key per
client (``clients[].password``); both are base64 strings of the method's key size. The share link 3x-ui
(``internal/sub/service.go``, ``genShadowsocksLink``) generates for these methods is

    ``ss://<method>:<server key>:<client key>@host:port?<query>#<remark>``

with every userinfo part escaped like Go's ``url.QueryEscape`` and *no* base64 around the userinfo: SIP022 forbids it
for the 2022 methods. Older ciphers (``base64url(method:password)`` userinfo) are not served: they are deprecated and
fingerprintable, so such inbounds are imported inactive.

Client keys are derived, not stored: ``base64(SHA-256(label, client uuid, inbound id, key size)[:key size])``. The
subscription's ``client_uuid`` is already the per-subscription secret (it is what a VLESS link carries) and it
changes when the link is rotated, so rotating a subscription rotates its Shadowsocks keys with it and no extra column
or migration is needed.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Mapping
from typing import Any, Final

SHADOWSOCKS: Final = "shadowsocks"
DEFAULT_METHOD: Final = "2022-blake3-aes-128-gcm"
# Key size in bytes of every SS-2022 method.
KEY_BYTES: Final = {
    "2022-blake3-aes-128-gcm": 16,
    "2022-blake3-aes-256-gcm": 32,
    "2022-blake3-chacha20-poly1305": 32,
}
# xray-core refuses the chacha20 variant on an inbound with users (the multi-user identity header is AES-only).
MULTI_USER_METHODS: Final = ("2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm")

# Keys of the cached inbound params that belong to Shadowsocks.
PARAM_METHOD: Final = "ss_method"
PARAM_PASSWORD: Final = "ss_password"  # the inbound's server key (every client link carries it)
PARAM_UNSUPPORTED: Final = "unsupported"  # human-readable reason the inbound cannot serve subscriptions
_KEY_LABEL: Final = "vpn-bot:ss2022:v1"


def key_bytes(method: str) -> int:
    """Key size of an SS-2022 method; 0 for any other method."""
    return KEY_BYTES.get(method, 0)


def unsupported_reason(method: str, *, network_ok: bool = True, security: str = "none") -> str | None:
    """Why an inbound with this method cannot serve subscriptions (None when it can)."""
    if not method.startswith("2022-"):
        return f"method {method or '?'} is deprecated and insecure: use a 2022-blake3 method"
    if method not in KEY_BYTES:
        return f"unknown Shadowsocks-2022 method {method}"
    if method not in MULTI_USER_METHODS:
        return f"{method} does not support several users: use 2022-blake3-aes-128-gcm or 2022-blake3-aes-256-gcm"
    if not network_ok:
        return "the inbound's transport is not supported"
    if security == "reality":
        return "Reality cannot secure a Shadowsocks inbound"
    return None


def generate_key(method: str) -> str:
    """A fresh random base64 key of the method's size (server key of a new inbound)."""
    size = key_bytes(method)
    if not size:
        raise ValueError(f"not a Shadowsocks-2022 method: {method!r}")
    return base64.b64encode(secrets.token_bytes(size)).decode("ascii")


def derive_client_key(client_uuid: str, inbound_id: int, method: str) -> str:
    """The stable per-subscription, per-inbound client key (base64, the method's key size)."""
    size = key_bytes(method)
    if not size:
        raise ValueError(f"not a Shadowsocks-2022 method: {method!r}")
    material = f"{_KEY_LABEL}:{client_uuid}:{inbound_id}:{size}".encode()
    return base64.b64encode(hashlib.sha256(material).digest()[:size]).decode("ascii")


def is_shadowsocks(params: Mapping[str, Any]) -> bool:
    return str(params.get("protocol") or "").lower() == SHADOWSOCKS


def blocked_reason(params: Mapping[str, Any]) -> str | None:
    """The cached reason a Shadowsocks inbound cannot be used; None for a usable one or another protocol."""
    if not is_shadowsocks(params):
        return None
    reason = params.get(PARAM_UNSUPPORTED)
    if reason:
        return str(reason)
    # Cached params written before the check existed, or edited by hand.
    return unsupported_reason(str(params.get(PARAM_METHOD) or "")) or (
        None if params.get(PARAM_PASSWORD) else "the inbound has no server key"
    )


def client_identity(params: Mapping[str, Any], client_uuid: str, inbound_id: int) -> str | None:
    """The password the subscription's client must carry on this Shadowsocks inbound (None for other protocols)."""
    if not is_shadowsocks(params):
        return None
    method = str(params.get(PARAM_METHOD) or "")
    return derive_client_key(client_uuid, inbound_id, method) if key_bytes(method) else None
