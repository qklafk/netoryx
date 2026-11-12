"""Telegram MTProto proxy: shared account types, secrets and share links.

Two backends serve personal MTProto proxy accounts: telemt (:mod:`app.services.panels.telemt`, Control API v1) and
3x-ui v3.5+ ``mtproto`` inbounds (:mod:`app.services.panels.xui`). Both implement :class:`MtprotoProxyBackend`
next to the generic :class:`~app.services.panels.base.PanelBackend`.

Secrets
-------
Every account has a 16-byte key (32 hex chars, the *key*). Clients receive it in one of three encodings:

* classic   ``<key>``
* secure    ``dd<key>`` (also the only form a WEB proxy accepts)
* Fake-TLS  ``ee<key><hex(domain)>`` - the domain is the SNI the connection imitates

Links (https://core.telegram.org/api/links)::

    tg://proxy?server=<host>&port=<port>&secret=<secret>
    https://t.me/proxy?server=<host>&port=<port>&secret=<secret>
    tg://webproxy?server=<host>&secret=<dd-secret>         (WEB proxy, always port 443)

Links are bearer credentials: never log them.
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit

KEY_HEX_LEN: Final = 32
_KEY_RE: Final = re.compile(r"[0-9a-f]{32}")
_HEX_RE: Final = re.compile(r"[0-9a-fA-F]+")
_DOMAIN_RE: Final = re.compile(r"[A-Za-z0-9.-]{1,253}")


# ---------------------------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------------------------
def new_key() -> str:
    """A fresh random account key (32 lowercase hex chars)."""
    return secrets.token_hex(16)


def is_key(value: object) -> bool:
    """True for a valid account key (32 hex chars, any case)."""
    return isinstance(value, str) and _KEY_RE.fullmatch(value.lower()) is not None


def is_ad_tag(value: object) -> bool:
    """Ad tags from @MTProxybot are 32 hex chars, like keys."""
    return is_key(value)


@dataclass(frozen=True, slots=True)
class ParsedSecret:
    mode: str  # "classic" | "secure" | "tls"
    key: str  # 32 lowercase hex chars
    domain: str = ""  # Fake-TLS SNI domain


def fake_tls_secret(key: str, domain: str) -> str:
    """``ee`` + key + hex(domain): the secret of a Fake-TLS link."""
    if not is_key(key):
        raise ValueError("MTProto key must be 32 hex chars")
    if not _DOMAIN_RE.fullmatch(domain):
        raise ValueError(f"invalid Fake-TLS domain {domain!r}")
    return "ee" + key.lower() + domain.encode("ascii").hex()


def secure_secret(key: str) -> str:
    """``dd`` + key: the padded-intermediate secret (also used by WEB proxies)."""
    if not is_key(key):
        raise ValueError("MTProto key must be 32 hex chars")
    return "dd" + key.lower()


def _secret_bytes(secret: str) -> bytes | None:
    if _HEX_RE.fullmatch(secret) and len(secret) % 2 == 0:
        return bytes.fromhex(secret)
    try:  # clients also accept base64url secrets
        return base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    except (binascii.Error, ValueError):
        return None


def parse_secret(secret: str) -> ParsedSecret | None:
    """Decode a client secret in any of the three encodings (hex or base64url); None if malformed."""
    raw = _secret_bytes(secret.strip())
    if raw is None:
        return None
    if len(raw) == 16:
        return ParsedSecret("classic", raw.hex())
    if len(raw) == 17 and raw[0] == 0xDD:
        return ParsedSecret("secure", raw[1:].hex())
    if len(raw) > 17 and raw[0] == 0xEE:
        try:
            domain = raw[17:].decode("ascii")
        except UnicodeDecodeError:
            return None
        if not _DOMAIN_RE.fullmatch(domain):
            return None
        return ParsedSecret("tls", raw[1:17].hex(), domain)
    return None


# ---------------------------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProxyLink:
    """One way to reach a proxy: host, port and the full client secret."""

    server: str
    port: int
    secret: str

    def _query(self) -> str:
        return urlencode({"server": self.server, "port": str(self.port), "secret": self.secret})

    @property
    def tg_url(self) -> str:
        """``tg://proxy?...`` - for the subscription web page and copy-paste."""
        return f"tg://proxy?{self._query()}"

    @property
    def tme_url(self) -> str:
        """``https://t.me/proxy?...`` - for inline buttons and ``Telegram.WebApp.openTelegramLink``."""
        return f"https://t.me/proxy?{self._query()}"

    @property
    def domain(self) -> str:
        """The Fake-TLS domain of the secret (empty for classic/secure secrets)."""
        parsed = parse_secret(self.secret)
        return parsed.domain if parsed else ""

    def to_json(self) -> dict[str, Any]:
        return {"server": self.server, "port": self.port, "secret": self.secret}

    @classmethod
    def from_json(cls, data: Any) -> ProxyLink | None:
        if not isinstance(data, dict):
            return None
        server, port, secret = data.get("server"), data.get("port"), data.get("secret")
        if not (isinstance(server, str) and server and isinstance(secret, str) and secret):
            return None
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            return None
        return cls(server, port, secret)


def web_proxy_url(host: str, key: str) -> str:
    """``tg://webproxy?server=<host>&secret=dd<key>`` (WEB proxies accept plain/secure secrets only)."""
    return "tg://webproxy?" + urlencode({"server": host, "secret": secure_secret(key)})


def parse_proxy_link(url: str) -> ProxyLink | None:
    """Parse ``tg://proxy?...`` / ``https://t.me/proxy?...``; None if it is not a complete MTProto link."""
    parts = urlsplit(url.strip())
    if parts.scheme == "tg":
        if parts.netloc != "proxy" and parts.path.strip("/") != "proxy":
            return None
    elif parts.scheme == "https":
        if parts.netloc.lower() not in ("t.me", "telegram.me") or parts.path.strip("/") != "proxy":
            return None
    else:
        return None
    query = dict(parse_qsl(parts.query))
    try:
        port = int(query.get("port", ""))
    except ValueError:
        return None
    return ProxyLink.from_json({"server": query.get("server"), "port": port, "secret": query.get("secret")})


def link_key(link: ProxyLink) -> str | None:
    """The account key carried by a link's secret."""
    parsed = parse_secret(link.secret)
    return parsed.key if parsed else None


# ---------------------------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProxyUserSpec:
    """Desired state of one proxy account. ``0`` limits mean "no limit"; ``None`` key keeps the current one."""

    name: str  # telemt username / 3x-ui client email
    enabled: bool
    key: str | None = None  # 32-hex account key; None = keep the existing secret (generated on create)
    expires_at: datetime | None = None  # server-side safety net; None = never expires
    max_unique_ips: int = 0
    traffic_limit_bytes: int = 0
    rate_limit_bps: int = 0  # per direction, bits per second (telemt only)
    ad_tag: str | None = None  # sponsored channel tag (32 hex) or None
    tls_domain: str = ""  # 3x-ui: the inbound's Fake-TLS domain (used to build the secret)
    tg_id: int = 0
    comment: str = ""


@dataclass(slots=True)
class ProxyAccount:
    """A proxy account as reported by the backend."""

    name: str
    enabled: bool
    key: str | None  # None if the backend did not reveal it
    links: list[ProxyLink] = field(default_factory=list)  # ready links (telemt); empty when built locally
    tls_secret: str | None = None  # full ``ee...`` secret when the backend stores one (3x-ui)
    expires_at: datetime | None = None
    max_unique_ips: int = 0
    traffic_limit_bytes: int = 0
    rate_limit_bps: int = 0
    ad_tag: str | None = None
    used_bytes: int = 0
    current_connections: int = 0
    active_unique_ips: int = 0
    recent_unique_ips: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MtprotoProxyBackend(Protocol):
    """Proxy-account operations. ``target``: None for telemt, the inbound remote id for 3x-ui."""

    async def upsert_proxy_user(self, target: int | None, spec: ProxyUserSpec) -> ProxyAccount:
        """Create the account or bring it to ``spec``; returns it with its effective key and links."""
        ...

    async def get_proxy_user(self, target: int | None, name: str) -> ProxyAccount | None: ...

    async def list_proxy_users(self, target: int | None) -> list[ProxyAccount]: ...

    async def delete_proxy_user(self, target: int | None, name: str) -> None:
        """Delete; "not found" is success."""
        ...
