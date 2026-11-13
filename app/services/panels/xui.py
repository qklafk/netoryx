"""3x-ui panel backend (https://github.com/MHSanaei/3x-ui), both client-API generations.

3x-ui has two incompatible client APIs:

* **modern** (v3.1+): clients live in their own table, keyed by email, under ``/panel/api/clients/*``
  (JSON bodies only; read responses carry the VLESS id in ``uuid``, write bodies in ``id``);
* **legacy** (<= v3.0): clients are stored inside the inbound ``settings`` JSON and are managed with
  ``/panel/api/inbounds/addClient|updateClient/{uuid}|{id}/delClient/{uuid}|getClientTraffics/{email}``.

The generation is detected once per backend (``GET /panel/api/clients/get/<probe>``: JSON -> modern,
404 -> legacy) and cached; it is re-detected if a generation-specific route disappears (panel upgrade).

Authentication:

* ``api_token`` -> ``Authorization: Bearer`` (v3.1+; no cookie, no CSRF). If the token is rejected or the
  panel predates tokens and a username/password is configured, the backend falls back to a session login.
* session login -> ``GET /csrf-token`` (v3.0+; 404 on older panels), ``POST /login``, cookie jar with
  ``unsafe=True`` (panels addressed by IP). Every non-GET request carries ``X-CSRF-Token``; a 403 refreshes
  the token and retries once. ``X-Requested-With: XMLHttpRequest`` makes an expired session answer 401
  instead of 404 on v3; 401 (and 404 on a route that must exist) triggers one re-login and retry.

Quirks handled here (all verified live on v2.9.4 and v3.11.0):

* errors are HTTP 200 with ``{"success": false, "msg": ...}`` -> :class:`XuiApiError`;
* updates replace the whole client, so every write sends the full object (read-merge-write);
* ``tgId`` must be an integer, ``expiryTime`` is milliseconds (0 = never, negative = "starts on first use"),
  ``totalGB`` is bytes (0 = unlimited);
* inbound ``settings``/``streamSettings`` are JSON strings on legacy panels and objects on v3.1+;
* legacy panels refuse to delete the last client of an inbound; a disabled placeholder client
  (``vpn-bot-keep-<inbound>``) is added first so that ``delClient`` works. The inbound itself is never
  rewritten: a whole-inbound update would wipe clients added concurrently (lost update).

Shadowsocks-2022: a ``shadowsocks`` inbound with a multi-user 2022 method takes one client per subscription,
identified by its ``email`` (there is no ``id``/``flow``; the client's own key is ``password``). On <= v3.0 panels
``updateClient``/``delClient`` address such a client by its email instead of its id.

Telegram proxy: v3.5+ panels serve ``mtproto`` inbounds through an mtg sidecar. Their clients have no ``id``/
``flow``; each carries its own Fake-TLS ``secret`` (``ee`` + 16-byte key + hex(domain)) and optional ``adTag``.
They are managed with the same modern clients API via the :class:`~app.services.panels.mtproto.MtprotoProxyBackend`
methods, under their own emails (never shared with VLESS clients, or the traffic quota would be summed).
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import hashlib
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote
from uuid import uuid4

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app.db.models import PanelKind
from app.services.panels import shadowsocks
from app.services.panels.base import (
    AccountSpec,
    InboundInfo,
    PanelAuthError,
    PanelError,
    PanelUnsupportedError,
    RemoteAccount,
    ServerStatus,
)
from app.services.panels.mtproto import ProxyAccount, ProxyUserSpec, fake_tls_secret, parse_secret
from app.services.panels.socks import MIXED, SOCKS_PROTOCOLS, SocksAccount, parse_accounts
from app.services.panels.xray_template import (
    TemplateError,
    YoutubeExit,
    ad_blocking_enabled,
    apply_extras,
    parse_template,
    set_ad_blocking,
    set_tg_only,
    sniffing_update,
    tg_only_enabled,
    youtube_exit_enabled,
)

if TYPE_CHECKING:
    from app.services.devices import DeviceInfo

logger = logging.getLogger(__name__)

# Transports for which normalized link parameters are produced (VLESS share links).
SUPPORTED_NETWORKS: Final = frozenset({"tcp", "ws", "grpc", "httpupgrade", "xhttp"})
VISION_FLOW: Final = "xtls-rprx-vision"
# Telegram MTProto proxy inbounds (3x-ui v3.5+): multi-client, one Fake-TLS secret per client.
MTPROTO: Final = "mtproto"
MTPROTO_DEFAULT_DOMAIN: Final = "www.cloudflare.com"  # the panel's default when an inbound sets none
MTPROTO_MIN_VERSION: Final = (3, 5)
SOCKS_WRITE_ATTEMPTS: Final = 3  # read-modify-write rounds before a concurrent editor is given up on
SOCKS_RETRY_DELAY: Final = 0.3

_API: Final = "/panel/api"
_XRAY_PREFIXES: Final = (f"{_API}/xray", "/panel/xray")
_PROBE_EMAIL: Final = "vpn-bot-api-probe"
_NETWORK_RETRIES: Final = 2  # extra attempts on connection errors (any method) / timeouts (GET only)
_RETRY_BASE_DELAY: Final = 0.5
_MAX_ERROR_BODY: Final = 200

# xray-core defaults that 3x-ui drops from the xhttp `extra` blob (see buildXhttpExtra in 3x-ui).
_XHTTP_CORE_DEFAULTS: Final = {"scMaxEachPostBytes": "1000000", "scMinPostsIntervalMs": "30"}
_XHTTP_STRING_FIELDS: Final = (
    "uplinkHTTPMethod",
    "sessionIDPlacement",
    "sessionIDKey",
    "sessionIDTable",
    "sessionIDLength",
    "seqPlacement",
    "seqKey",
    "uplinkDataPlacement",
    "uplinkDataKey",
    "scMaxEachPostBytes",
    "scMinPostsIntervalMs",
)
_XHTTP_PADDING_OBFS_FIELDS: Final = ("xPaddingKey", "xPaddingHeader", "xPaddingPlacement", "xPaddingMethod")

# Client fields the bot owns; everything else on the remote client is preserved on update.
_MANAGED_FIELDS: Final = (
    "id",
    "email",
    "enable",
    "expiryTime",
    "totalGB",
    "limitIp",
    "tgId",
    "subId",
    "comment",
    "flow",
)
# v3.1+ read responses contain fields whose write type differs (e.g. allowedIPs: "" vs []string), so only
# these known-safe fields are carried over from the remote client into an update body.
_MODERN_CARRY_FIELDS: Final = (
    "limitHwid",
    "group",
    "reset",
    "resetDay",
    "resetWeekday",
    "resetMax",
    "trafficReset",
    "trafficResetDay",
)
# Shadowsocks clients carry a password instead of the VLESS id and flow.
_SS_MANAGED_FIELDS: Final = (*(f for f in _MANAGED_FIELDS if f not in ("id", "flow")), "password")
_NOT_FOUND_MARKERS: Final = ("not found",)
_PLACEHOLDER_PREFIX: Final = "vpn-bot-keep-"  # legacy: disabled client that keeps an inbound non-empty
_VERSION_IN_HTML: Final = re.compile(r"\.(?:css|js)\?v?(\d+\.\d+\.\d+)")


class XuiApiError(PanelError):
    """The panel answered ``success: false``; ``msg`` is the panel's message."""

    def __init__(self, path: str, msg: str) -> None:
        self.path = path
        self.msg = msg.strip()
        super().__init__(f"3x-ui {path}: {self.msg or 'request failed'}")

    @property
    def not_found(self) -> bool:
        low = self.msg.lower()
        return any(marker in low for marker in _NOT_FOUND_MARKERS)


class XuiRouteNotFoundError(PanelError):
    """HTTP 404 on a route that should exist (wrong URL / web base path, or API generation changed)."""


class ApiGeneration(StrEnum):
    MODERN = "modern"  # v3.1+: /panel/api/clients/*
    LEGACY = "legacy"  # <= v3.0: clients inside inbound settings


# ---------------------------------------------------------------------------------------------
# Value conversion helpers
# ---------------------------------------------------------------------------------------------
def _as_dict(value: Any) -> dict[str, Any]:
    """Inbound ``settings``/``streamSettings`` arrive as JSON strings (legacy) or objects (v3.1+)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _int(value: Any, default: int = 0) -> int:
    """Lenient int: legacy panels may store ``tgId: ""`` or numbers as strings."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip() or default)
        except ValueError:
            return default
    return default


def to_expiry_ms(expires_at: datetime | None) -> int:
    """Aware datetime -> 3x-ui ``expiryTime`` (ms since epoch; 0 = never expires)."""
    if expires_at is None:
        return 0
    if expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware")
    return int(expires_at.timestamp() * 1000)


def from_expiry_ms(value: Any) -> datetime | None:
    """3x-ui ``expiryTime`` -> aware UTC datetime. 0 and negative values ("start on first use") -> None."""
    ms = _int(value)
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _search_key(data: Any, key: str) -> Any:
    """Depth-first lookup of ``key`` (top level first), like 3x-ui's ``searchKey``."""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for value in data.values():
            found = _search_key(value, key)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _search_key(value, key)
            if found is not None:
                return found
    return None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _first_str(value: Any) -> str:
    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0]
    return _str(value)


# ---------------------------------------------------------------------------------------------
# Link parameter normalization (mirrors 3x-ui internal/sub genVlessLink)
# ---------------------------------------------------------------------------------------------
def _host_header(headers: Any) -> str:
    if not isinstance(headers, dict):
        return ""
    for name, value in headers.items():
        if name.lower() == "host":
            return _first_str(value)
    return ""


def _path_and_host(transport: dict[str, Any], params: dict[str, Any]) -> None:
    params["path"] = _str(transport.get("path"))
    host = _str(transport.get("host"))
    params["host"] = host or _host_header(transport.get("headers"))


def _tcp_params(stream: dict[str, Any], params: dict[str, Any]) -> None:
    header = _as_dict(_as_dict(stream.get("tcpSettings")).get("header"))
    if header.get("type") != "http":
        return
    request = _as_dict(header.get("request"))
    paths = request.get("path")
    if isinstance(paths, list) and paths:
        params["path"] = _str(paths[0])
    host = _host_header(_as_dict(header.get("response")).get("headers")) or _host_header(request.get("headers"))
    params["host"] = host
    params["headerType"] = "http"


def _grpc_params(stream: dict[str, Any], params: dict[str, Any]) -> None:
    grpc = _as_dict(stream.get("grpcSettings"))
    params["serviceName"] = _str(grpc.get("serviceName"))
    params["authority"] = _str(grpc.get("authority"))
    if grpc.get("multiMode") is True:
        params["mode"] = "multi"


def xhttp_extra(xhttp: dict[str, Any]) -> dict[str, Any]:
    """Bidirectional xhttp fields for the link's ``extra`` JSON (3x-ui ``buildXhttpExtra``)."""
    extra: dict[str, Any] = {}
    if mode := _str(xhttp.get("mode")):
        extra["mode"] = mode
    if padding := _str(xhttp.get("xPaddingBytes")):
        extra["xPaddingBytes"] = padding
    if xhttp.get("xPaddingObfsMode") is True:
        extra["xPaddingObfsMode"] = True
        for name in _XHTTP_PADDING_OBFS_FIELDS:
            if value := _str(xhttp.get(name)):
                extra[name] = value
    for name in _XHTTP_STRING_FIELDS:
        value = _str(xhttp.get(name))
        if value and value != _XHTTP_CORE_DEFAULTS.get(name):
            extra[name] = value
    for legacy, renamed in (("sessionPlacement", "sessionIDPlacement"), ("sessionKey", "sessionIDKey")):
        if renamed not in extra and (value := _str(xhttp.get(legacy))):
            extra[renamed] = value
    # Older clients read the pre-rename keys; 3x-ui emits both.
    for renamed, legacy in (("sessionIDPlacement", "sessionPlacement"), ("sessionIDKey", "sessionKey")):
        if renamed in extra:
            extra[legacy] = extra[renamed]
    chunk = xhttp.get("uplinkChunkSize")
    if isinstance(chunk, (int, float, str)) and not isinstance(chunk, bool) and chunk not in (0, ""):
        extra["uplinkChunkSize"] = chunk
    if xhttp.get("noGRPCHeader") is True:
        extra["noGRPCHeader"] = True
    for name in ("xmux", "downloadSettings"):
        nested = xhttp.get(name)
        if isinstance(nested, (dict, list)) and nested:
            extra[name] = nested
    headers = xhttp.get("headers")
    if isinstance(headers, dict):
        kept = {k: v for k, v in headers.items() if k.lower() != "host"}
        if kept:
            extra["headers"] = kept
    return extra


def _xhttp_params(stream: dict[str, Any], params: dict[str, Any]) -> None:
    xhttp = _as_dict(stream.get("xhttpSettings"))
    _path_and_host(xhttp, params)
    if isinstance(xhttp.get("mode"), str):
        params["mode"] = xhttp["mode"]
    if padding := _str(xhttp.get("xPaddingBytes")):
        params["x_padding_bytes"] = padding
    if extra := xhttp_extra(xhttp):
        params["extra"] = extra


def _tls_params(stream: dict[str, Any], params: dict[str, Any]) -> None:
    tls = _as_dict(stream.get("tlsSettings"))
    alpn = [a for a in tls.get("alpn") or [] if isinstance(a, str) and a]
    if alpn:
        params["alpn"] = ",".join(alpn)
    sni = tls.get("serverName")
    if sni is not None:
        params["sni"] = _str(sni)
    client = tls.get("settings")
    if client is None:
        return
    fingerprint = _search_key(client, "fingerprint")
    if fingerprint is not None:
        params["fp"] = _str(fingerprint)
    if ech := _str(_search_key(client, "echConfigList")):
        params["ech"] = ech
    if vcn := _str(_search_key(client, "verifyPeerCertByName")).strip():
        params["vcn"] = vcn
    pins = _search_key(client, "pinnedPeerCertSha256")
    if isinstance(pins, list) and (joined := ",".join(p for p in pins if isinstance(p, str) and p)):
        params["pcs"] = joined


def _reality_params(stream: dict[str, Any], params: dict[str, Any]) -> None:
    reality = _as_dict(stream.get("realitySettings"))
    if not reality:
        return
    client = reality.get("settings")
    if sni := _first_str(_search_key(reality, "serverNames")):
        params["sni"] = sni
    if (pbk := _search_key(client, "publicKey")) is not None:
        params["pbk"] = _str(pbk)
    if sid := _first_str(_search_key(reality, "shortIds")):
        params["sid"] = sid
    if fp := _str(_search_key(client, "fingerprint")):
        params["fp"] = fp
    if pqv := _str(_search_key(client, "mldsa65Verify")):
        params["pqv"] = pqv
    # The SpiderX *seed*; v3.11 derives a per-client path from it when rendering links.
    params["spx"] = _str(_search_key(client, "spiderX"))


def _vless_encryption_enabled(settings: Mapping[str, Any]) -> bool:
    return any(_str(settings.get(key)) not in ("", "none") for key in ("encryption", "decryption"))


def normalize_link_params(
    stream_settings: Any, inbound_settings: Any = None, *, disable_flow: bool = False
) -> dict[str, Any]:
    """Turn an inbound's ``streamSettings`` (+ ``settings``) into VLESS share-link query parameters.

    Keys mirror 3x-ui's own links: ``type``, ``encryption``, ``security`` and, depending on the transport,
    ``path``/``host``/``headerType`` (tcp-http, ws, httpupgrade), ``serviceName``/``authority``/``mode`` (grpc),
    ``path``/``host``/``mode``/``x_padding_bytes``/``extra`` (xhttp; ``extra`` is a dict); for TLS ``sni``,
    ``fp``, ``alpn`` (comma-joined), ``ech``, ``vcn``, ``pcs``; for Reality ``sni``, ``pbk``, ``sid``, ``fp``,
    ``pqv`` and ``spx`` (the SpiderX seed). ``flow_allowed`` tells whether a Vision flow may be used
    (raw TCP with TLS/Reality, or VLESS encryption). Missing values are omitted, as 3x-ui does.
    """
    stream = _as_dict(stream_settings)
    settings = _as_dict(inbound_settings)
    network = _str(stream.get("network")) or "tcp"
    security = _str(stream.get("security")) or "none"
    encryption = _str(settings.get("encryption")) or "none"
    params: dict[str, Any] = {"type": network, "encryption": encryption}

    network_handlers: dict[str, Callable[[dict[str, Any], dict[str, Any]], None]] = {
        "tcp": _tcp_params,
        "ws": lambda s, p: _path_and_host(_as_dict(s.get("wsSettings")), p),
        "httpupgrade": lambda s, p: _path_and_host(_as_dict(s.get("httpupgradeSettings")), p),
        "grpc": _grpc_params,
        "xhttp": _xhttp_params,
    }
    if handler := network_handlers.get(network):
        handler(stream, params)

    if security == "tls":
        params["security"] = "tls"
        _tls_params(stream, params)
    elif security == "reality":
        params["security"] = "reality"
        _reality_params(stream, params)
    else:
        params["security"] = "none"

    tls_flow = network == "tcp" and security in ("tls", "reality")
    params["flow_allowed"] = (
        not disable_flow and network != "xdrive" and (tls_flow or _vless_encryption_enabled(settings))
    )
    return params


def mtproto_params(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Link parameters of an ``mtproto`` inbound (v3.5+, served by the panel's mtg sidecar, not by Xray).

    ``fake_tls_domain`` is the SNI every client secret of the inbound fronts; ``local`` is False for inbounds
    owned by a remote node (the sidecar serves local inbounds only).
    """
    settings = _as_dict(raw.get("settings"))
    domain = _str(settings.get("fakeTlsDomain")).strip() or MTPROTO_DEFAULT_DOMAIN
    return {"fake_tls_domain": domain, "local": raw.get("nodeId") is None}


def socks_params(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Parameters of a ``mixed``/``socks`` inbound used for the Telegram SOCKS5 proxy.

    ``socks_auth`` is the inbound's authentication mode (the bot only serves ``password``); ``local`` is False for
    inbounds owned by a remote node.
    """
    settings = _as_dict(raw.get("settings"))
    return {"socks_auth": _str(settings.get("auth")) or "noauth", "local": raw.get("nodeId") is None}


def shadowsocks_params(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Link parameters of a ``shadowsocks`` inbound.

    Besides the transport fields of :func:`normalize_link_params` (minus the VLESS-only ones) they hold
    ``ss_method``, ``ss_password`` (the inbound's server key) and, when subscriptions cannot be served from the
    inbound (deprecated method, no multi-user support, unsupported transport), ``unsupported`` with the reason.
    """
    settings = _as_dict(raw.get("settings"))
    params = normalize_link_params(raw.get("streamSettings"), settings)
    for key in ("encryption", "flow_allowed"):
        params.pop(key, None)
    method = _str(settings.get("method"))
    password = _str(settings.get("password"))
    params[shadowsocks.PARAM_METHOD] = method
    params[shadowsocks.PARAM_PASSWORD] = password
    reason = shadowsocks.unsupported_reason(
        method,
        network_ok=params.get("type") in SUPPORTED_NETWORKS,
        security=_str(params.get("security")) or "none",
    )
    if reason is None and not password:
        reason = "the inbound has no server key"
    if reason:
        params[shadowsocks.PARAM_UNSUPPORTED] = reason
    return params


def parse_inbound(raw: Mapping[str, Any]) -> InboundInfo:
    """Convert one ``/panel/api/inbounds/list`` entry into :class:`InboundInfo`."""
    protocol = _str(raw.get("protocol"))
    if protocol == MTPROTO:
        params = mtproto_params(raw)
    elif protocol == shadowsocks.SHADOWSOCKS:
        params = shadowsocks_params(raw)
    elif protocol in SOCKS_PROTOCOLS:
        params = socks_params(raw)
    else:
        params = normalize_link_params(
            raw.get("streamSettings"), raw.get("settings"), disable_flow=bool(raw.get("disableFlow"))
        )
    return InboundInfo(
        remote_id=_int(raw.get("id")),
        remark=_str(raw.get("remark")),
        protocol=protocol,
        port=_int(raw.get("port")),
        enabled=bool(raw.get("enable", True)),
        params=params,
    )


# ---------------------------------------------------------------------------------------------
# Client payloads
# ---------------------------------------------------------------------------------------------
def build_client(spec: AccountSpec, carry: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Full client object for add/update bodies. ``carry`` keeps unmanaged fields.

    A VLESS client is identified by ``id`` (= the subscription's uuid) and may carry a ``flow``; a Shadowsocks client
    (``spec.password`` set) has neither, its ``password`` is its key.
    """
    client = dict(carry or {})
    client.update(
        email=spec.email,
        enable=spec.enabled,
        expiryTime=to_expiry_ms(spec.expires_at),
        totalGB=max(0, spec.traffic_limit_bytes),
        limitIp=max(0, spec.device_limit),
        tgId=int(spec.tg_id),
        subId=spec.sub_id,
        comment=spec.comment,
    )
    if spec.password:
        client["password"] = spec.password
        client.pop("id", None)
        client.pop("flow", None)
    else:
        client.update(id=spec.uuid, flow=spec.flow)
    client.setdefault("reset", 0)
    return client


def _identity(client: Mapping[str, Any], key: str) -> str:
    """The client's identity: its VLESS id (``key``: ``uuid`` on v3.1+, ``id`` before), else its Shadowsocks key."""
    return _str(client.get(key)) or _str(client.get("password"))


def _managed_view(client: Mapping[str, Any], uuid: str) -> dict[str, Any]:
    """The managed fields of a remote client, normalized for comparison with :func:`build_client`."""
    return {
        "id": uuid,
        "password": _str(client.get("password")),
        "email": _str(client.get("email")),
        "enable": bool(client.get("enable", True)),
        "expiryTime": _int(client.get("expiryTime")),
        "totalGB": _int(client.get("totalGB")),
        "limitIp": _int(client.get("limitIp")),
        "tgId": _int(client.get("tgId")),
        "subId": _str(client.get("subId")),
        "comment": _str(client.get("comment")),
        "flow": _str(client.get("flow")),
    }


def _differs(current: dict[str, Any], desired: Mapping[str, Any]) -> bool:
    fields = _MANAGED_FIELDS if "id" in desired else _SS_MANAGED_FIELDS
    return any(current[name] != desired[name] for name in fields)


def _remote_account(client: Mapping[str, Any], uuid: str, used_bytes: int) -> RemoteAccount:
    return RemoteAccount(
        email=_str(client.get("email")),
        uuid=uuid,
        enabled=bool(client.get("enable", True)),
        expires_at=from_expiry_ms(client.get("expiryTime")),
        device_limit=_int(client.get("limitIp")),
        traffic_limit_bytes=_int(client.get("totalGB")),
        used_bytes=used_bytes,
        raw=dict(client),
    )


def _account_from_spec(spec: AccountSpec, raw: Mapping[str, Any], used_bytes: int) -> RemoteAccount:
    return RemoteAccount(
        email=spec.email,
        uuid=spec.password or spec.uuid,
        enabled=spec.enabled,
        expires_at=from_expiry_ms(to_expiry_ms(spec.expires_at)),
        device_limit=max(0, spec.device_limit),
        traffic_limit_bytes=max(0, spec.traffic_limit_bytes),
        used_bytes=used_bytes,
        raw=dict(raw),
    )


def _traffic_bytes(traffic: Any) -> int:
    data = _as_dict(traffic)
    return _int(data.get("up")) + _int(data.get("down"))


def _q(segment: str) -> str:
    return quote(segment, safe="")


# ---------------------------------------------------------------------------------------------
# MTProto (Telegram proxy) client payloads
# ---------------------------------------------------------------------------------------------
_MTPROTO_MANAGED_FIELDS: Final = (
    "email",
    "enable",
    "expiryTime",
    "totalGB",
    "limitIp",
    "tgId",
    "subId",
    "comment",
    "secret",
    "adTag",
)


def mtproto_sub_id(email: str) -> str:
    """Deterministic ``subId`` of a proxy client (3x-ui refuses to re-add an email under another subId)."""
    return hashlib.sha256(f"vpn-bot:tg-proxy:{email}".encode()).hexdigest()[:16]


def mtproto_secret(spec: ProxyUserSpec, current: str = "") -> str:
    """The client secret to send: ``ee<key><hex(domain)>`` for a given key, else the current one ("" = generate).

    The domain is the inbound's (``spec.tls_domain``), falling back to the one in the current secret.
    """
    if not spec.key:
        return current
    parsed = parse_secret(current) if current else None
    domain = spec.tls_domain or (parsed.domain if parsed else "") or MTPROTO_DEFAULT_DOMAIN
    return fake_tls_secret(spec.key, domain)


def build_mtproto_client(
    spec: ProxyUserSpec, carry: Mapping[str, Any] | None = None, *, current_secret: str = ""
) -> dict[str, Any]:
    """Full mtproto client object (no ``id``/``flow``: the identity is the per-client ``secret``)."""
    client = dict(carry or {})
    client.update(
        email=spec.name,
        enable=spec.enabled,
        expiryTime=to_expiry_ms(spec.expires_at),
        totalGB=max(0, spec.traffic_limit_bytes),
        limitIp=max(0, spec.max_unique_ips),
        tgId=int(spec.tg_id),
        subId=mtproto_sub_id(spec.name),
        comment=spec.comment,
        secret=mtproto_secret(spec, current_secret),
        adTag=(spec.ad_tag or "").lower(),
    )
    client.setdefault("reset", 0)
    return client


def _mtproto_view(client: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "email": _str(client.get("email")),
        "enable": bool(client.get("enable", True)),
        "expiryTime": _int(client.get("expiryTime")),
        "totalGB": _int(client.get("totalGB")),
        "limitIp": _int(client.get("limitIp")),
        "tgId": _int(client.get("tgId")),
        "subId": _str(client.get("subId")),
        "comment": _str(client.get("comment")),
        "secret": _str(client.get("secret")).lower(),
        "adTag": _str(client.get("adTag")).lower(),
    }


def _mtproto_differs(current: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    view, wanted = _mtproto_view(current), _mtproto_view(desired)
    return any(view[name] != wanted[name] for name in _MTPROTO_MANAGED_FIELDS)


def _proxy_account(client: Mapping[str, Any], used_bytes: int) -> ProxyAccount:
    secret = _str(client.get("secret"))
    parsed = parse_secret(secret) if secret else None
    tag = _str(client.get("adTag")).lower()
    return ProxyAccount(
        name=_str(client.get("email")),
        enabled=bool(client.get("enable", True)),
        key=parsed.key if parsed else None,
        tls_secret=secret.lower() or None,
        expires_at=from_expiry_ms(client.get("expiryTime")),
        max_unique_ips=_int(client.get("limitIp")),
        traffic_limit_bytes=_int(client.get("totalGB")),
        ad_tag=tag or None,
        used_bytes=used_bytes,
        raw=dict(client),
    )


def _version_tuple(version: str | None) -> tuple[int, ...] | None:
    match = re.match(r"v?(\d+)\.(\d+)", version or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


# ---------------------------------------------------------------------------------------------
# Server status, client IPs, Reality inbounds
# ---------------------------------------------------------------------------------------------
_NO_IP_RECORD: Final = "no ip record"
_IP_WITH_TIME: Final = re.compile(r"^\s*(?P<ip>[0-9A-Fa-f.:\[\]]+)\s*(?:\((?P<ts>[^)]*)\))?\s*$")
REALITY_SHORT_ID_BYTES: Final = 8
REALITY_FINGERPRINT: Final = "chrome"


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _used_total(value: Any) -> tuple[int | None, int | None]:
    data = value if isinstance(value, dict) else {}
    return _opt_int(data.get("current")), _opt_int(data.get("total"))


def parse_server_status(obj: Mapping[str, Any]) -> ServerStatus:
    """Convert ``/panel/api/server/status`` into :class:`ServerStatus` (missing fields stay ``None``)."""
    mem_used, mem_total = _used_total(obj.get("mem"))
    swap_used, swap_total = _used_total(obj.get("swap"))
    disk_used, disk_total = _used_total(obj.get("disk"))
    net_io = _as_dict(obj.get("netIO"))
    traffic = _as_dict(obj.get("netTraffic"))
    xray = _as_dict(obj.get("xray"))
    loads = obj.get("loads")
    load = tuple(v for v in (_opt_float(x) for x in loads) if v is not None) if isinstance(loads, list) else ()
    return ServerStatus(
        cpu_percent=_opt_float(obj.get("cpu")),
        cpu_cores=_opt_int(obj.get("cpuCores")) or _opt_int(obj.get("logicalPro")),
        mem_used=mem_used,
        mem_total=mem_total,
        swap_used=swap_used,
        swap_total=swap_total,
        disk_used=disk_used,
        disk_total=disk_total,
        load=load,
        uptime_sec=_opt_int(obj.get("uptime")),
        net_up_bps=_opt_int(net_io.get("up")),
        net_down_bps=_opt_int(net_io.get("down")),
        traffic_sent=_opt_int(traffic.get("sent")),
        traffic_recv=_opt_int(traffic.get("recv")),
        tcp_connections=_opt_int(obj.get("tcpCount")),
        core_state=_str(xray.get("state")) or None,
        core_version=_str(xray.get("version")) or None,
        core_error=_str(xray.get("errorMsg")) or None,
    )


def _parse_ip_time(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def parse_client_ips(obj: Any) -> list[tuple[str, datetime | None]]:
    """The ``clientIps`` answer as ``(ip, last seen)`` pairs.

    Panels answer with the text ``"No IP Record"``, a JSON-encoded list, or a list; entries are ``"1.2.3.4"``,
    ``"1.2.3.4 (2026-01-02 03:04:05)"`` or ``{"ip": ..., "timestamp": ...}`` depending on the version.
    """
    if isinstance(obj, str):
        text = obj.strip()
        if not text or text.lower() == _NO_IP_RECORD:
            return []
        try:
            obj = json.loads(text)
        except ValueError:
            obj = [line for line in re.split(r"[\n,]", text) if line.strip()]
    if not isinstance(obj, list):
        return []
    result: dict[str, datetime | None] = {}
    for item in obj:
        ip, seen = "", None
        if isinstance(item, dict):
            ip = _str(item.get("ip")).strip()
            stamp = item.get("timestamp")
            if isinstance(stamp, int | float) and stamp > 0:
                seen = datetime.fromtimestamp(stamp / 1000 if stamp > 1e11 else stamp, tz=UTC)
            elif isinstance(stamp, str):
                seen = _parse_ip_time(stamp)
        elif isinstance(item, str) and (match := _IP_WITH_TIME.match(item)):
            ip = match.group("ip")
            seen = _parse_ip_time(match.group("ts")) if match.group("ts") else None
        if ip and ip not in result:
            result[ip] = seen
    return list(result.items())


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_reality_keys() -> tuple[str, str]:
    """A fresh X25519 key pair for Reality as Xray prints it: ``(private_key, public_key)``, base64url."""
    private = X25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64url(private_raw), _b64url(public_raw)


def reality_public_key(private_key: str) -> str:
    """The public key of a base64url Reality private key (raises ``ValueError`` for a malformed key)."""
    raw = base64.urlsafe_b64decode(private_key + "=" * (-len(private_key) % 4))
    if len(raw) != 32:
        raise ValueError("a Reality private key is 32 bytes")
    public_raw = (
        X25519PrivateKey.from_private_bytes(raw)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return _b64url(public_raw)


def build_reality_inbound(
    port: int,
    sni: str,
    *,
    dest: str | None = None,
    remark: str = "",
    private_key: str | None = None,
    short_id: str | None = None,
) -> dict[str, Any]:
    """Body of ``POST /panel/api/inbounds/add`` for a VLESS + Reality (raw TCP) inbound.

    ``settings``, ``streamSettings`` and ``sniffing`` are JSON-encoded strings: the only form every panel
    version accepts. Keys are generated locally unless given.
    """
    if not 0 < port < 65536:
        raise ValueError("port must be 1..65535")
    server_name = sni.strip().lower()
    if not server_name or any(ch.isspace() or ch in "/:" for ch in server_name):
        raise ValueError(f"invalid SNI {sni!r}")
    target = (dest or f"{server_name}:443").strip()
    if private_key is None:
        private_key, public_key = generate_reality_keys()
    else:
        public_key = reality_public_key(private_key)
    sid = short_id if short_id is not None else secrets.token_hex(REALITY_SHORT_ID_BYTES)
    if len(sid) > 16 or len(sid) % 2 or any(ch not in "0123456789abcdef" for ch in sid):
        raise ValueError("short id must be up to 16 lowercase hex characters (even length)")
    stream = {
        "network": "tcp",
        "security": "reality",
        "externalProxy": [],
        "realitySettings": {
            "show": False,
            "xver": 0,
            "target": target,
            "dest": target,  # older builds read "dest"
            "serverNames": [server_name],
            "privateKey": private_key,
            "minClientVer": "",
            "maxClientVer": "",
            "maxTimediff": 0,
            "shortIds": [sid],
            "mldsa65Seed": "",
            "settings": {
                "publicKey": public_key,
                "fingerprint": REALITY_FINGERPRINT,
                "serverName": "",
                "spiderX": "/",
                "mldsa65Verify": "",
            },
        },
        "tcpSettings": {"acceptProxyProtocol": False, "header": {"type": "none"}},
    }
    settings = {"clients": [], "decryption": "none", "encryption": "none", "fallbacks": []}
    sniffing = {"enabled": True, "destOverride": ["http", "tls", "quic"], "metadataOnly": False, "routeOnly": False}
    return {
        "up": 0,
        "down": 0,
        "total": 0,
        "remark": remark or f"reality-{port}",
        "enable": True,
        "expiryTime": 0,
        "listen": "",
        "port": port,
        "protocol": "vless",
        "settings": json.dumps(settings),
        "streamSettings": json.dumps(stream),
        "sniffing": json.dumps(sniffing),
    }


def build_mixed_inbound(port: int, *, remark: str = "") -> dict[str, Any]:
    """Body of ``POST /panel/api/inbounds/add`` for a password-protected ``mixed`` (SOCKS5 + HTTP) inbound.

    It starts without accounts: the bot adds one per user (see ``apply_socks_users``).
    """
    if not 0 < port < 65536:
        raise ValueError("port must be 1..65535")
    settings = {"auth": "password", "accounts": [], "udp": False, "ip": "127.0.0.1"}
    sniffing = {"enabled": False, "destOverride": ["http", "tls"], "metadataOnly": False, "routeOnly": False}
    return {
        "up": 0,
        "down": 0,
        "total": 0,
        "remark": remark or f"socks-{port}",
        "enable": True,
        "expiryTime": 0,
        "listen": "",
        "port": port,
        "protocol": MIXED,
        "settings": json.dumps(settings),
        "streamSettings": json.dumps({}),
        "sniffing": json.dumps(sniffing),
    }


def build_shadowsocks_inbound(
    port: int,
    *,
    method: str = shadowsocks.DEFAULT_METHOD,
    remark: str = "",
    server_key: str | None = None,
) -> dict[str, Any]:
    """Body of ``POST /panel/api/inbounds/add`` for a multi-user Shadowsocks-2022 inbound (raw TCP, TCP + UDP relay).

    It starts without clients: the bot adds one per subscription. ``server_key`` (base64, the method's key size) is
    generated unless given. Only methods that take several users are accepted.
    """
    if not 0 < port < 65536:
        raise ValueError("port must be 1..65535")
    if method not in shadowsocks.MULTI_USER_METHODS:
        raise ValueError(f"method must be one of {', '.join(shadowsocks.MULTI_USER_METHODS)}")
    if server_key is None:
        server_key = shadowsocks.generate_key(method)
    else:
        try:
            raw = base64.b64decode(server_key, validate=True)
        except ValueError:
            raw = b""
        if len(raw) != shadowsocks.key_bytes(method):
            raise ValueError(f"the server key must be {shadowsocks.key_bytes(method)} bytes, base64-encoded")
    settings = {"method": method, "password": server_key, "network": "tcp,udp", "clients": [], "ivCheck": False}
    tcp = {"acceptProxyProtocol": False, "header": {"type": "none"}}
    stream = {"network": "tcp", "security": "none", "tcpSettings": tcp}
    sniffing = {"enabled": True, "destOverride": ["http", "tls", "quic"], "metadataOnly": False, "routeOnly": False}
    return {
        "up": 0,
        "down": 0,
        "total": 0,
        "remark": remark or f"ss-{port}",
        "enable": True,
        "expiryTime": 0,
        "listen": "",
        "port": port,
        "protocol": shadowsocks.SHADOWSOCKS,
        "settings": json.dumps(settings),
        "streamSettings": json.dumps(stream),
        "sniffing": json.dumps(sniffing),
    }


# ---------------------------------------------------------------------------------------------
# HTTP transport: auth (Bearer / session + CSRF), retries, envelope parsing
# ---------------------------------------------------------------------------------------------
@dataclass(slots=True)
class _Response:
    status: int
    body: bytes


class _Transport:
    """One aiohttp session per panel; serializes logins; transparently re-authenticates once."""

    def __init__(
        self,
        url: str,
        *,
        username: str | None,
        password: str | None,
        api_token: str | None,
        tls_verify: bool,
        timeout: float,
        extra_headers: Mapping[str, str] | None,
    ) -> None:
        self.base = url.rstrip("/")
        self._username = username or None
        self._password = password or None
        self._token = api_token or None
        self._tls_verify = tls_verify
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._extra_headers = dict(extra_headers or {})
        self._http: aiohttp.ClientSession | None = None
        self._closed = False  # set by close(): a closed transport never opens a new session
        self._login_lock = asyncio.Lock()
        self._logged_in = False
        self._auth_epoch = 0  # bumped on every (re)login / CSRF refresh to coalesce concurrent retries
        self._csrf: str | None = None
        if not self._token and not self.has_credentials:
            raise ValueError("3x-ui backend needs username+password or api_token")

    @property
    def has_credentials(self) -> bool:
        return bool(self._username and self._password)

    @property
    def uses_token(self) -> bool:
        return self._token is not None

    def _session(self) -> aiohttp.ClientSession:
        if self._closed:
            # The registry closes replaced backends that callers may still hold; reopening would leak a
            # session nobody closes (and keep using stale credentials).
            raise PanelError(f"3x-ui {self.base}: backend is closed")
        if self._http is None or self._http.closed:
            connector = aiohttp.TCPConnector(ssl=False) if not self._tls_verify else None
            self._http = aiohttp.ClientSession(
                connector=connector,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=self._timeout,
                headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest", **self._extra_headers},
            )
        return self._http

    async def close(self) -> None:
        """Close the session for good: later requests raise :class:`PanelError`."""
        self._closed = True
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None
        self._logged_in = False
        self._csrf = None

    # --- raw send with connection retries -------------------------------------------------------
    async def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        form: Mapping[str, str] | None = None,
        follow_redirects: bool = False,
    ) -> _Response:
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        elif method != "GET" and self._csrf:
            headers["X-CSRF-Token"] = self._csrf
        idempotent = method == "GET"
        attempt = 0
        while True:
            try:
                async with self._session().request(
                    method,
                    self.base + path,
                    json=json_body,
                    data=form,
                    headers=headers,
                    allow_redirects=follow_redirects,
                ) as resp:
                    return _Response(resp.status, await resp.read())
            except (aiohttp.ClientError, TimeoutError) as exc:
                # A connect error means nothing was sent, so even a POST may be repeated.
                retryable = idempotent or isinstance(exc, aiohttp.ClientConnectorError)
                if not retryable or attempt >= _NETWORK_RETRIES:
                    detail = str(exc) or "timed out"
                    raise PanelError(f"3x-ui {method} {path}: {type(exc).__name__}: {detail}") from exc
                await asyncio.sleep(_RETRY_BASE_DELAY * 2**attempt)
                attempt += 1

    # --- session login ---------------------------------------------------------------------------
    async def _fetch_csrf(self) -> None:
        resp = await self._send("GET", "/csrf-token")
        if resp.status == 404:
            self._csrf = None  # pre-v3.0 panel: no CSRF protection
            return
        token = _parse_envelope("/csrf-token", resp).get("obj")
        self._csrf = token if isinstance(token, str) and token else None

    async def _login(self) -> None:
        if not self.has_credentials:
            raise PanelAuthError("3x-ui: session expired and no username/password configured")
        self._session().cookie_jar.clear()
        self._csrf = None
        await self._fetch_csrf()
        form = {"username": self._username or "", "password": self._password or "", "twoFactorCode": ""}
        resp = await self._send("POST", "/login", form=form)
        if resp.status == 404:
            raise XuiRouteNotFoundError(f"3x-ui login page not found at {self.base}/login: check the URL/web base path")
        if resp.status == 403:
            raise PanelAuthError("3x-ui rejected the login request (CSRF)")
        envelope = _parse_envelope("/login", resp)
        if not envelope.get("success"):
            raise PanelAuthError(f"3x-ui login failed: {_str(envelope.get('msg')).strip() or 'invalid credentials'}")
        self._logged_in = True
        self._auth_epoch += 1
        logger.info("Logged in to 3x-ui at %s", self.base)

    async def _ensure_login(self) -> None:
        if self._token or self._logged_in:
            return
        async with self._login_lock:
            if not self._logged_in:
                await self._login()

    async def _reauthenticate(self, epoch: int, *, csrf_only: bool) -> None:
        """Re-login (or just refresh CSRF) unless a concurrent request already did since ``epoch``."""
        async with self._login_lock:
            if self._auth_epoch != epoch:
                return
            if csrf_only:
                await self._fetch_csrf()
                self._auth_epoch += 1
            else:
                self._logged_in = False
                await self._login()

    def _drop_token(self, reason: str) -> None:
        logger.warning("3x-ui %s: %s; falling back to username/password login", self.base, reason)
        self._token = None
        self._logged_in = False

    # --- public request API ------------------------------------------------------------------------
    async def text(self, path: str) -> str | None:
        """GET an HTML page (following redirects); None on any failure. Used only for version sniffing."""
        try:
            resp = await self._send("GET", path, follow_redirects=True)
        except PanelError:
            return None
        return resp.body.decode("utf-8", "replace") if resp.status == 200 else None

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        form: Mapping[str, str] | None = None,
        missing_ok: bool = False,
    ) -> dict[str, Any] | None:
        """Send an API request and return the JSON envelope.

        ``missing_ok``: a 404 returns None instead of triggering re-login (used for route probing).
        Raises :class:`PanelAuthError` on auth failures, :class:`XuiRouteNotFoundError` on a persistent 404,
        :class:`PanelError` on transport errors / non-JSON answers. ``success: false`` is NOT raised here.
        """
        for attempt in range(2):
            await self._ensure_login()
            epoch = self._auth_epoch
            resp = await self._send(method, path, json_body=json_body, form=form)
            if resp.status == 404 and missing_ok:
                return None
            if resp.status in (401, 404) and attempt == 0 and await self._recover_unauthorized(resp.status, epoch):
                continue
            if resp.status == 403 and attempt == 0 and not self._token:
                await self._reauthenticate(epoch, csrf_only=True)
                continue
            return self._finish(method, path, resp)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _recover_unauthorized(self, status: int, epoch: int) -> bool:
        """Decide whether a 401/404 can be fixed by authenticating again; do it and return True if so."""
        if self._token:
            if not self.has_credentials:
                return False
            self._drop_token("API token rejected" if status == 401 else "API token not supported (panel < v3.1)")
            return True
        await self._reauthenticate(epoch, csrf_only=False)
        return True

    def _finish(self, method: str, path: str, resp: _Response) -> dict[str, Any]:
        if resp.status == 401:
            raise PanelAuthError(f"3x-ui {path}: unauthorized" + (" (bad API token)" if self._token else ""))
        if resp.status == 403:
            raise PanelAuthError(f"3x-ui {path}: forbidden: {_body_snippet(resp.body)}")
        if resp.status == 404:
            if self._token:
                raise PanelUnsupportedError(f"3x-ui {path}: 404 with API token (tokens need 3x-ui v3.1+)")
            raise XuiRouteNotFoundError(f"3x-ui {method} {path}: 404 (check URL/web base path or panel version)")
        return _parse_envelope(path, resp)


def _body_snippet(body: bytes) -> str:
    return body[:_MAX_ERROR_BODY].decode("utf-8", "replace").strip()


def _parse_envelope(path: str, resp: _Response) -> dict[str, Any]:
    if resp.status != 200:
        raise PanelError(f"3x-ui {path}: HTTP {resp.status}: {_body_snippet(resp.body)}")
    try:
        data = json.loads(resp.body)
    except ValueError as exc:
        raise PanelError(f"3x-ui {path}: non-JSON response (check the panel URL/web base path)") from exc
    if not isinstance(data, dict) or "success" not in data:
        raise PanelError(f"3x-ui {path}: unexpected response: {_body_snippet(resp.body)}")
    return data


def _obj(path: str, envelope: Mapping[str, Any] | None) -> Any:
    """Return ``obj`` of a successful envelope or raise :class:`XuiApiError`."""
    if envelope is None:
        raise XuiRouteNotFoundError(f"3x-ui {path}: 404")
    if not envelope.get("success"):
        raise XuiApiError(path, _str(envelope.get("msg")))
    return envelope.get("obj")


# ---------------------------------------------------------------------------------------------
# Client API adapters
# ---------------------------------------------------------------------------------------------
class _ModernClients:
    """v3.1+: ``/panel/api/clients/*`` keyed by email; a client may be attached to several inbounds."""

    def __init__(self, transport: _Transport) -> None:
        self._t = transport

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        full = f"{_API}/clients/{path}"
        return _obj(full, await self._t.request(method, full, json_body=body))

    async def _find(self, email: str) -> dict[str, Any] | None:
        try:
            obj = await self._call("GET", f"get/{_q(email)}")
        except XuiApiError as exc:
            if exc.not_found:
                return None
            raise
        return obj if isinstance(obj, dict) else None

    @staticmethod
    def _inbound_ids(found: Mapping[str, Any]) -> list[int]:
        return [_int(i) for i in found.get("inboundIds") or []]

    @staticmethod
    def _to_account(found: Mapping[str, Any]) -> RemoteAccount:
        client = _as_dict(found.get("client"))
        return _remote_account(client, _identity(client, "uuid"), _int(found.get("usedTraffic")))

    async def get(self, target: int, email: str) -> RemoteAccount | None:
        found = await self._find(email)
        if found is None or target not in self._inbound_ids(found):
            return None
        return self._to_account(found)

    async def used_bytes(self, target: int, email: str) -> int | None:
        found = await self._find(email)
        if found is None or target not in self._inbound_ids(found):
            return None
        return _int(found.get("usedTraffic"))

    async def list_many(self, targets: list[int]) -> dict[int, list[RemoteAccount]]:
        """Accounts of several inbounds from one ``clients/list`` download (it holds the whole panel)."""
        accounts: dict[int, list[RemoteAccount]] = {target: [] for target in targets}
        for item in await self._call("GET", "list") or []:
            if not isinstance(item, dict):
                continue
            for target in self._inbound_ids(item):
                if target in accounts:
                    used = _traffic_bytes(item.get("traffic"))
                    accounts[target].append(_remote_account(item, _identity(item, "uuid"), used))
        return accounts

    async def list(self, target: int) -> list[RemoteAccount]:
        items = await self._call("GET", "list") or []
        accounts = []
        for item in items:
            if isinstance(item, dict) and target in self._inbound_ids(item):
                used = _traffic_bytes(item.get("traffic"))
                accounts.append(_remote_account(item, _identity(item, "uuid"), used))
        return accounts

    async def upsert(self, target: int, spec: AccountSpec) -> RemoteAccount:
        found = await self._find(spec.email)
        if found is None:
            client = build_client(spec)
            try:
                await self._call("POST", "add", {"inboundIds": [target], "client": client})
                return _account_from_spec(spec, client, 0)
            except XuiApiError as exc:
                if "already" not in exc.msg.lower():
                    raise
                found = await self._find(spec.email)  # created concurrently: fall through to update
                if found is None:
                    raise
        return await self._update(target, spec, found)

    async def _update(self, target: int, spec: AccountSpec, found: Mapping[str, Any]) -> RemoteAccount:
        remote = _as_dict(found.get("client"))
        if target not in self._inbound_ids(found):
            await self._call("POST", f"{_q(spec.email)}/attach", {"inboundIds": [target]})
        carry: dict[str, Any] = {k: remote[k] for k in _MODERN_CARRY_FIELDS if k in remote}
        client = build_client(spec, carry)
        if _differs(_managed_view(remote, _identity(remote, "uuid")), client):
            await self._call("POST", f"update/{_q(spec.email)}", client)
        return _account_from_spec(spec, client, _int(found.get("usedTraffic")))

    async def delete(self, target: int, email: str) -> None:
        found = await self._find(email)
        if found is None:
            return
        inbound_ids = self._inbound_ids(found)
        if target not in inbound_ids:
            return
        try:
            if len(inbound_ids) > 1:  # shared with other inbounds: only detach from this one
                await self._call("POST", f"{_q(email)}/detach", {"inboundIds": [target]})
            else:
                await self._call("POST", f"del/{_q(email)}")
        except XuiApiError as exc:
            if not exc.not_found:
                raise

    async def clear_ips(self, email: str) -> None:
        await self._call("POST", f"clearIps/{_q(email)}")

    async def reset_traffic(self, target: int, email: str) -> None:
        try:
            await self._call("POST", f"resetTraffic/{_q(email)}")
        except XuiApiError as exc:
            if not exc.not_found:
                raise

    # --- mtproto (Telegram proxy) clients -------------------------------------------------------------
    async def _read_back(self, target: int, email: str) -> ProxyAccount:
        """The stored client after a write: the panel generates/normalizes the secret on save."""
        found = await self._find(email)
        if found is None or target not in self._inbound_ids(found):
            raise PanelError(f"3x-ui: mtproto client {email} vanished right after it was written")
        return _proxy_account(_as_dict(found.get("client")), _int(found.get("usedTraffic")))

    async def get_mtproto(self, target: int, email: str) -> ProxyAccount | None:
        found = await self._find(email)
        if found is None or target not in self._inbound_ids(found):
            return None
        return _proxy_account(_as_dict(found.get("client")), _int(found.get("usedTraffic")))

    async def list_mtproto(self, target: int) -> builtins.list[ProxyAccount]:
        accounts = []
        for item in await self._call("GET", "list") or []:
            if isinstance(item, dict) and target in self._inbound_ids(item):
                accounts.append(_proxy_account(item, _traffic_bytes(item.get("traffic"))))
        return accounts

    async def upsert_mtproto(self, target: int, spec: ProxyUserSpec) -> ProxyAccount:
        found = await self._find(spec.name)
        if found is None:
            client = build_mtproto_client(spec)
            try:
                await self._call("POST", "add", {"inboundIds": [target], "client": client})
                return await self._read_back(target, spec.name)
            except XuiApiError as exc:
                if "already" not in exc.msg.lower():
                    raise
                found = await self._find(spec.name)  # created concurrently: fall through to update
                if found is None:
                    raise
        remote = _as_dict(found.get("client"))
        if target not in self._inbound_ids(found):
            await self._call("POST", f"{_q(spec.name)}/attach", {"inboundIds": [target]})
        carry: dict[str, Any] = {k: remote[k] for k in _MODERN_CARRY_FIELDS if k in remote}
        client = build_mtproto_client(spec, carry, current_secret=_str(remote.get("secret")))
        if not _mtproto_differs(remote, client):
            return _proxy_account(remote, _int(found.get("usedTraffic")))
        await self._call("POST", f"update/{_q(spec.name)}", client)
        return await self._read_back(target, spec.name)


class _LegacyClients:
    """<= v3.0: clients stored in the inbound ``settings`` JSON; traffic in ``clientStats``."""

    def __init__(self, transport: _Transport) -> None:
        self._t = transport

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        full = f"{_API}/inbounds/{path}"
        return _obj(full, await self._t.request(method, full, json_body=body))

    async def _inbound(self, target: int) -> dict[str, Any]:
        obj = await self._call("GET", f"get/{target}")
        if not isinstance(obj, dict):
            raise PanelError(f"3x-ui inbound {target}: unexpected response")
        return obj

    @staticmethod
    def _clients(inbound: Mapping[str, Any]) -> list[dict[str, Any]]:
        clients = _as_dict(inbound.get("settings")).get("clients") or []
        return [c for c in clients if isinstance(c, dict)]

    @classmethod
    def _find(cls, inbound: Mapping[str, Any], email: str) -> dict[str, Any] | None:
        clients = cls._clients(inbound)
        exact = next((c for c in clients if c.get("email") == email), None)
        return exact or next((c for c in clients if _str(c.get("email")).lower() == email.lower()), None)

    async def _stats(self, email: str) -> dict[str, Any] | None:
        """The client's traffic record (``clientStats`` row: inboundId, up, down, ...); None if there is none.

        Cheap (one indexed row) compared with ``get/{id}``, whose ``settings`` JSON carries every client.
        """
        try:
            obj = await self._call("GET", f"getClientTraffics/{_q(email)}")
        except XuiApiError as exc:
            if exc.not_found:
                return None
            raise
        return obj if isinstance(obj, dict) else None

    async def get(self, target: int, email: str) -> RemoteAccount | None:
        client = self._find(await self._inbound(target), email)
        if client is None:
            return None
        used = _traffic_bytes(await self._stats(_str(client.get("email"))))
        return _remote_account(client, _identity(client, "id"), used)

    async def used_bytes(self, target: int, email: str) -> int | None:
        stats = await self._stats(email)
        if stats is None:
            return None
        if stats.get("inboundId") is not None and _int(stats.get("inboundId")) != target:
            return None  # emails are unique per panel: the client lives on another inbound
        return _traffic_bytes(stats)

    # (defined before ``list``: below it, the name ``list`` in annotations means the method)
    @classmethod
    def _accounts_of(cls, inbound: Mapping[str, Any]) -> list[RemoteAccount]:
        stats = {_str(s.get("email")): s for s in inbound.get("clientStats") or [] if isinstance(s, dict)}
        return [
            _remote_account(c, _identity(c, "id"), _traffic_bytes(stats.get(_str(c.get("email")))))
            for c in cls._clients(inbound)
            if not _str(c.get("email")).startswith(_PLACEHOLDER_PREFIX)  # not an account (see _add_placeholder)
        ]

    async def list_many(self, targets: list[int]) -> dict[int, list[RemoteAccount]]:
        """Accounts of several inbounds from one ``inbounds/list`` download (it holds every inbound's clients)."""
        inbounds = await self._call("GET", "list") or []
        by_id = {_int(i.get("id")): i for i in inbounds if isinstance(i, dict)}
        accounts: dict[int, list[RemoteAccount]] = {}
        for target in targets:
            if (inbound := by_id.get(target)) is None:
                raise XuiApiError(f"{_API}/inbounds/list", f"inbound {target} not found")
            accounts[target] = self._accounts_of(inbound)
        return accounts

    async def list(self, target: int) -> list[RemoteAccount]:
        # `get/{id}` does not preload clientStats on legacy panels; `list` does.
        inbounds = await self._call("GET", "list") or []
        inbound = next((i for i in inbounds if isinstance(i, dict) and _int(i.get("id")) == target), None)
        if inbound is None:
            raise XuiApiError(f"{_API}/inbounds/list", f"inbound {target} not found")
        return self._accounts_of(inbound)

    async def _add(self, target: int, spec: AccountSpec) -> RemoteAccount:
        client = build_client(spec)
        await self._call("POST", "addClient", {"id": target, "settings": json.dumps({"clients": [client]})})
        return _account_from_spec(spec, client, 0)

    async def upsert(self, target: int, spec: AccountSpec) -> RemoteAccount:
        stats = await self._stats(spec.email)
        if stats is None:
            # No traffic record: almost surely a new client. Add it without downloading the inbound (whose
            # settings hold every client); the panel rejects a duplicate email (case-insensitively), and
            # then the full read below finds the existing client.
            try:
                return await self._add(target, spec)
            except XuiApiError as exc:
                if "duplicate" not in exc.msg.lower():
                    raise
        inbound = await self._inbound(target)
        remote = self._find(inbound, spec.email)
        if remote is None:
            return await self._add(target, spec)
        client = build_client(spec, remote)
        client["email"] = _str(remote.get("email")) or spec.email  # keep the stored spelling
        if _differs(_managed_view(remote, _identity(remote, "id")), client):
            body = {"id": target, "settings": json.dumps({"clients": [client]})}
            await self._call("POST", f"updateClient/{_q(self._client_key(inbound, remote))}", body)
        return _account_from_spec(spec, client, _traffic_bytes(stats))

    async def delete(self, target: int, email: str) -> None:
        try:
            inbound = await self._inbound(target)
        except XuiApiError as exc:
            if exc.not_found:
                return
            raise
        client = self._find(inbound, email)
        if client is None:
            return
        key = self._client_key(inbound, client)
        if len(self._clients(inbound)) > 1:
            try:
                await self._del_client(target, key)
                return
            except XuiApiError as exc:
                if "no client remained" not in exc.msg.lower():
                    raise  # else concurrent deletes left ours the last client
        await self._add_placeholder(target, _as_dict(inbound.get("settings")))
        await self._del_client(target, key)

    @staticmethod
    def _client_key(inbound: Mapping[str, Any], client: Mapping[str, Any]) -> str:
        """What ``updateClient``/``delClient`` call the client: its email on Shadowsocks inbounds, else its id."""
        if _str(inbound.get("protocol")) == shadowsocks.SHADOWSOCKS or not client.get("id"):
            return _str(client.get("email"))
        return _str(client.get("id"))

    async def _del_client(self, target: int, key: str) -> None:
        try:
            await self._call("POST", f"{target}/delClient/{_q(key)}")
        except XuiApiError as exc:
            if not exc.not_found:
                raise

    async def _add_placeholder(self, target: int, settings: Mapping[str, Any] | None = None) -> None:
        """Legacy panels refuse to delete an inbound's last client: keep a disabled placeholder in it.

        Only per-client calls are used (``addClient`` + ``delClient``): rewriting the whole inbound would race
        with clients added concurrently (lost update). The placeholder is disabled (Xray never loads it), has a
        random id, and its email is outside the bot's naming scheme (never treated as an orphan). It is
        reused by later deletes and is harmless if an admin removes it. On a Shadowsocks inbound it carries a random
        key of the method's size instead of an id.
        """
        method = _str((settings or {}).get("method"))
        placeholder = build_client(
            AccountSpec(
                email=f"{_PLACEHOLDER_PREFIX}{target}",
                uuid=str(uuid4()),
                enabled=False,
                expires_at=None,
                device_limit=0,
                traffic_limit_bytes=0,
                tg_id=0,
                sub_id=secrets.token_hex(8),
                comment="vpn-bot: keeps the inbound non-empty (3x-ui <= v3.0 cannot delete its last client)",
                password=shadowsocks.generate_key(method) if shadowsocks.key_bytes(method) else "",
            )
        )
        try:
            await self._call("POST", "addClient", {"id": target, "settings": json.dumps({"clients": [placeholder]})})
        except XuiApiError as exc:
            if "duplicate" not in exc.msg.lower():
                raise  # duplicate: it already exists (then delClient tells whether it is in this inbound)

    async def clear_ips(self, email: str) -> None:
        await self._call("POST", f"clearClientIps/{_q(email)}")

    async def reset_traffic(self, target: int, email: str) -> None:
        try:
            await self._call("POST", f"{target}/resetClientTraffic/{_q(email)}")
        except XuiApiError as exc:
            if not exc.not_found:
                raise


# ---------------------------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------------------------
class XuiBackend:
    """:class:`~app.services.panels.base.PanelBackend` for 3x-ui. Targets are inbound remote ids (int)."""

    kind = PanelKind.XUI

    def __init__(
        self,
        url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        api_token: str | None = None,
        tls_verify: bool = True,
        timeout: float = 15,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._t = _Transport(
            url,
            username=username,
            password=password,
            api_token=api_token,
            tls_verify=tls_verify,
            timeout=timeout,
            extra_headers=extra_headers,
        )
        self._generation: ApiGeneration | None = None
        self._clients: _ModernClients | _LegacyClients | None = None
        self._detect_lock = asyncio.Lock()
        self._socks_locks: dict[int, asyncio.Lock] = {}
        self.version: str | None = None
        self._xray_prefix: str | None = None

    def __repr__(self) -> str:
        return f"XuiBackend({self._t.base!r}, generation={self._generation})"

    @property
    def url(self) -> str:
        return self._t.base

    @property
    def generation(self) -> ApiGeneration | None:
        """Detected client-API generation (None until the first call)."""
        return self._generation

    async def close(self) -> None:
        await self._t.close()

    # --- detection -------------------------------------------------------------------------------
    async def _status(self) -> dict[str, Any]:
        path = f"{_API}/server/status"
        obj = _obj(path, await self._t.request("GET", path))
        return obj if isinstance(obj, dict) else {}

    async def _detect(self) -> _ModernClients | _LegacyClients:
        async with self._detect_lock:
            if self._clients is not None:
                return self._clients
            status = await self._status()  # authenticates first, so a 404 below is a real 404
            probe = await self._t.request("GET", f"{_API}/clients/get/{_PROBE_EMAIL}", missing_ok=True)
            if probe is None:
                self._generation, self._clients = ApiGeneration.LEGACY, _LegacyClients(self._t)
            else:
                self._generation, self._clients = ApiGeneration.MODERN, _ModernClients(self._t)
            self.version = await self._version_from(status)
            logger.info("3x-ui %s: version %s, %s client API", self._t.base, self.version, self._generation)
            return self._clients

    async def _version_from(self, status: Mapping[str, Any]) -> str:
        if version := _str(status.get("panelVersion")):
            return version
        html = await self._t.text("/")  # login page assets carry "?<version>" on legacy panels
        if html and (match := _VERSION_IN_HTML.search(html)):
            return match.group(1)
        return "legacy" if self._generation is ApiGeneration.LEGACY else "unknown"

    async def _run[T](self, op: Callable[[_ModernClients | _LegacyClients], Awaitable[T]]) -> T:
        """Run a client operation; re-detect the API generation once if its routes vanished."""
        clients = self._clients or await self._detect()
        try:
            return await op(clients)
        except XuiRouteNotFoundError:
            self._clients = self._generation = None
            return await op(await self._detect())

    # --- PanelBackend ----------------------------------------------------------------------------
    async def ping(self) -> str:
        """Check connectivity and credentials; returns the panel version (e.g. ``"3.11.0"``)."""
        if self._clients is None:
            await self._detect()
        else:
            await self._status()
        return self.version or "unknown"

    async def list_inbounds(self) -> list[InboundInfo]:
        path = f"{_API}/inbounds/list"
        items = _obj(path, await self._t.request("GET", path)) or []
        return [parse_inbound(item) for item in items if isinstance(item, dict)]

    async def list_accounts(self, target: int | None) -> list[RemoteAccount]:
        inbound = _require_target(target)
        return await self._run(lambda c: c.list(inbound))

    async def list_accounts_by_target(self, targets: list[int]) -> dict[int, list[RemoteAccount]]:
        """:meth:`list_accounts` of several inbounds with one panel request (provisioning's panel scan)."""
        inbounds = [_require_target(target) for target in targets]
        return await self._run(lambda c: c.list_many(inbounds))

    async def get_account(self, target: int | None, email: str) -> RemoteAccount | None:
        inbound = _require_target(target)
        return await self._run(lambda c: c.get(inbound, email))

    async def get_used_bytes(self, target: int | None, email: str) -> int | None:
        """Traffic counter only: on legacy panels this skips downloading the whole inbound."""
        inbound = _require_target(target)
        return await self._run(lambda c: c.used_bytes(inbound, email))

    async def upsert_account(self, target: int | None, spec: AccountSpec) -> RemoteAccount:
        inbound = _require_target(target)
        return await self._run(lambda c: c.upsert(inbound, spec))

    async def delete_account(self, target: int | None, email: str, uuid: str) -> None:
        """Delete the client with ``email`` from the inbound (located by email; ``uuid`` is informational)."""
        inbound = _require_target(target)
        await self._run(lambda c: c.delete(inbound, email))

    async def get_links(self, account: RemoteAccount, client_headers: Mapping[str, str] | None = None) -> list[str]:
        return []  # 3x-ui links are built locally from cached inbound params

    async def reset_devices(self, account: RemoteAccount) -> None:
        """Clear the client's recorded IPs (the 3x-ui ``limitIp`` device counter)."""
        await self._run(lambda c: c.clear_ips(account.email))

    async def reset_traffic(self, target: int | None, email: str) -> None:
        """Zero the client's up/down counters (3x-ui re-enables a client disabled for depleted traffic)."""
        inbound = _require_target(target)
        await self._run(lambda c: c.reset_traffic(inbound, email))

    # --- optional capabilities: devices, server status, core restart, inbound creation --------------
    async def list_devices(self, account: RemoteAccount) -> list[DeviceInfo]:
        """IPs the panel recorded for the client (the ``limitIp`` counter); the device id is the IP.

        3x-ui cannot forget a single IP, so there is no ``delete_device``: :meth:`reset_devices` clears them all.
        """
        from app.services.devices import DeviceInfo

        clients = self._clients or await self._detect()
        email = _q(account.email)
        paths = [f"{_API}/inbounds/clientIps/{email}"]
        if isinstance(clients, _ModernClients):
            paths.insert(0, f"{_API}/clients/ips/{email}")
        for path in paths:
            envelope = await self._t.request("POST", path, missing_ok=True)
            if envelope is None:
                continue
            obj = envelope.get("obj") if envelope.get("success") else None
            return [DeviceInfo(id=ip, ip=ip, created_at=seen) for ip, seen in parse_client_ips(obj)]
        raise PanelUnsupportedError(f"3x-ui {self._t.base}: this panel version does not report client IPs")

    async def server_status(self) -> ServerStatus:
        """CPU, memory, disk, network and Xray state of the panel's server."""
        return parse_server_status(await self._status())

    async def restart_core(self) -> None:
        """Restart Xray on the panel's server (active connections drop for a moment)."""
        path = f"{_API}/server/restartXrayService"
        _obj(path, await self._t.request("POST", path))

    # --- optional capability: ad / tracker blocking through the Xray config template -----------------
    async def _xray_template(self) -> tuple[dict[str, Any], str]:
        """The Xray template and the route prefix that served it (v3: ``/panel/api/xray``, v2: ``/panel/xray``)."""
        for prefix in _XRAY_PREFIXES if self._xray_prefix is None else (self._xray_prefix,):
            envelope = await self._t.request("POST", f"{prefix}/", missing_ok=True)
            if envelope is None:
                continue
            obj = _obj(f"{prefix}/", envelope)
            try:
                template = parse_template(obj)
            except TemplateError as exc:
                raise PanelError(f"3x-ui {self._t.base}: {exc}") from exc
            self._xray_prefix = prefix
            return template, prefix
        raise PanelUnsupportedError(f"3x-ui {self._t.base}: the Xray template API was not found")

    async def get_ad_blocking(self) -> bool:
        """Whether the panel's Xray template blocks ad / tracker domains."""
        template, _ = await self._xray_template()
        try:
            return ad_blocking_enabled(template)
        except TemplateError as exc:
            raise PanelError(f"3x-ui {self._t.base}: {exc}") from exc

    async def _edit_template(self, editor: Callable[[dict[str, Any]], tuple[dict[str, Any], bool]]) -> bool:
        """Read the template, apply ``editor`` and save when it changed (one write, one Xray restart)."""
        template, prefix = await self._xray_template()
        try:
            updated, changed = editor(template)
        except TemplateError as exc:
            raise PanelError(f"3x-ui {self._t.base}: {exc}") from exc
        if not changed:
            return False
        path = f"{prefix}/update"
        form = {"xraySetting": json.dumps(updated, ensure_ascii=False, indent=2)}
        _obj(path, await self._t.request("POST", path, form=form))
        clients = self._clients or await self._detect()
        if not isinstance(clients, _ModernClients):
            await self.restart_core()  # v3.1+ restarts a running Xray itself when the template is saved
        return True

    async def set_ad_blocking(self, on: bool) -> bool:
        """Add / remove the ad-blocking routing rule. Xray restarts only if the template changed; returns that."""
        changed = await self._edit_template(lambda t: set_ad_blocking(t, on))
        if changed:
            logger.info("3x-ui %s: ad blocking %s", self._t.base, "enabled" if on else "disabled")
        return changed

    async def get_youtube_exit(self, wanted: YoutubeExit | None) -> bool:
        """Whether the template already matches ``wanted`` (``None``: none of our YouTube entries)."""
        template, _ = await self._xray_template()
        try:
            return youtube_exit_enabled(template, wanted)
        except TemplateError as exc:
            raise PanelError(f"3x-ui {self._t.base}: {exc}") from exc

    async def apply_template_extras(self, *, ads: bool, youtube: YoutubeExit | None) -> bool:
        """Bring ad blocking and the YouTube exit to the wanted state in one write and at most one restart."""
        changed = await self._edit_template(lambda t: apply_extras(t, ads=ads, youtube=youtube))
        if changed:
            logger.info("3x-ui %s: xray template updated (ads=%s, youtube exit=%s)", self._t.base, ads, bool(youtube))
        return changed

    async def apply_tg_only(self, emails: Sequence[str]) -> bool:
        """Confine the clients ``emails`` to Telegram (none: remove the rules); one write, at most one restart."""
        changed = await self._edit_template(lambda t: set_tg_only(t, emails))
        if changed:
            logger.info("3x-ui %s: Telegram-only rules for %d client(s) updated", self._t.base, len(emails))
        return changed

    async def get_tg_only(self, emails: Sequence[str]) -> bool:
        """Whether the template already holds the Telegram-only rules for ``emails``."""
        template, _ = await self._xray_template()
        try:
            return tg_only_enabled(template, emails)
        except TemplateError as exc:
            raise PanelError(f"3x-ui {self._t.base}: {exc}") from exc

    async def ensure_sniffing(self) -> int:
        """Make VLESS inbounds sniff http / tls / quic (domain routing needs it); returns how many were changed.

        Only the ``sniffing`` field is edited; the rest of each inbound is written back as read.
        """
        path = f"{_API}/inbounds/list"
        items = _obj(path, await self._t.request("GET", path)) or []
        updated = 0
        for raw in items:
            if not isinstance(raw, dict) or _str(raw.get("protocol")) != "vless" or not isinstance(raw.get("id"), int):
                continue
            sniffing = sniffing_update(raw.get("sniffing"))
            if sniffing is None:
                continue
            body = {key: value for key, value in raw.items() if key != "clientStats"}
            for key in ("settings", "streamSettings"):
                if isinstance(body.get(key), dict):
                    body[key] = json.dumps(body[key])
            body["sniffing"] = json.dumps(sniffing)
            update_path = f"{_API}/inbounds/update/{raw['id']}"
            _obj(update_path, await self._t.request("POST", update_path, json_body=body))
            updated += 1
        return updated

    async def create_reality_inbound(
        self,
        port: int,
        sni: str,
        dest: str | None = None,
        remark: str = "",
        flow: str = VISION_FLOW,
    ) -> InboundInfo:
        """Create a VLESS + Reality inbound with locally generated X25519 keys and short id.

        ``dest`` defaults to ``<sni>:443``. ``flow`` is the client flow to use on it (returned as
        ``params["flow"]``; 3x-ui stores flows per client). Raises :class:`XuiApiError` if the panel refuses
        (e.g. the port is taken).
        """
        body = build_reality_inbound(port, sni, dest=dest, remark=remark)
        path = f"{_API}/inbounds/add"
        obj = _obj(path, await self._t.request("POST", path, json_body=body))
        info = parse_inbound(obj if isinstance(obj, dict) else {**body, "id": 0})
        if not info.remote_id:
            # Some panels answer without the created object: find it by port.
            info = next((i for i in await self.list_inbounds() if i.port == port), info)
        if flow and info.params.get("flow_allowed"):
            info.params["flow"] = flow
        logger.info("3x-ui %s: created Reality inbound %s on port %s", self._t.base, info.remote_id, port)
        return info

    async def create_mixed_inbound(self, port: int, remark: str = "") -> InboundInfo:
        """Create a password-protected ``mixed`` (SOCKS5) inbound for the Telegram SOCKS5 proxy."""
        body = build_mixed_inbound(port, remark=remark)
        path = f"{_API}/inbounds/add"
        obj = _obj(path, await self._t.request("POST", path, json_body=body))
        info = parse_inbound(obj if isinstance(obj, dict) else {**body, "id": 0})
        if not info.remote_id:
            info = next((i for i in await self.list_inbounds() if i.port == port), info)
        logger.info("3x-ui %s: created SOCKS5 inbound %s on port %s", self._t.base, info.remote_id, port)
        return info

    async def create_shadowsocks_inbound(
        self, port: int, method: str = shadowsocks.DEFAULT_METHOD, remark: str = ""
    ) -> InboundInfo:
        """Create a multi-user Shadowsocks-2022 inbound with a locally generated server key."""
        body = build_shadowsocks_inbound(port, method=method, remark=remark)
        path = f"{_API}/inbounds/add"
        obj = _obj(path, await self._t.request("POST", path, json_body=body))
        info = parse_inbound(obj if isinstance(obj, dict) else {**body, "id": 0})
        if not info.remote_id:
            info = next((i for i in await self.list_inbounds() if i.port == port), info)
        logger.info("3x-ui %s: created Shadowsocks inbound %s on port %s", self._t.base, info.remote_id, port)
        return info

    # --- MtprotoProxyBackend (Telegram proxy on ``mtproto`` inbounds, v3.5+) -------------------------
    async def _mtproto[T](self, op: Callable[[_ModernClients], Awaitable[T]]) -> T:
        async def run(clients: _ModernClients | _LegacyClients) -> T:
            if not isinstance(clients, _ModernClients):
                raise PanelUnsupportedError(f"3x-ui {self._t.base}: MTProto inbounds need 3x-ui v3.5+")
            version = _version_tuple(self.version)
            if version is not None and version < MTPROTO_MIN_VERSION:
                raise PanelUnsupportedError(f"3x-ui {self._t.base} {self.version}: MTProto clients need v3.5+")
            return await op(clients)

        return await self._run(run)

    async def upsert_proxy_user(self, target: int | None, spec: ProxyUserSpec) -> ProxyAccount:
        """Create/update the mtproto client ``spec.name`` (its own email, never shared with VLESS clients).

        The secret is ``ee<spec.key><hex(inbound domain)>``; without a key the panel generates one. The stored
        client is read back, so the returned account carries the effective secret.
        """
        inbound = _require_target(target)
        return await self._mtproto(lambda c: c.upsert_mtproto(inbound, spec))

    async def get_proxy_user(self, target: int | None, name: str) -> ProxyAccount | None:
        inbound = _require_target(target)
        return await self._mtproto(lambda c: c.get_mtproto(inbound, name))

    async def list_proxy_users(self, target: int | None) -> list[ProxyAccount]:
        inbound = _require_target(target)
        return await self._mtproto(lambda c: c.list_mtproto(inbound))

    async def delete_proxy_user(self, target: int | None, name: str) -> None:
        inbound = _require_target(target)
        await self._mtproto(lambda c: c.delete(inbound, name))

    # --- SocksProxyBackend (Telegram SOCKS5 proxy on ``mixed`` inbounds) -----------------------------
    async def _read_inbound(self, inbound: int) -> dict[str, Any]:
        path = f"{_API}/inbounds/get/{inbound}"
        obj = _obj(path, await self._t.request("GET", path))
        if not isinstance(obj, dict):
            raise PanelError(f"3x-ui inbound {inbound}: unexpected response")
        if _str(obj.get("protocol")) not in SOCKS_PROTOCOLS:
            raise PanelError(f"3x-ui inbound {inbound} is not a mixed (SOCKS5) inbound")
        return obj

    async def list_socks_users(self, target: int | None) -> list[SocksAccount]:
        raw = await self._read_inbound(_require_target(target))
        return parse_accounts(_as_dict(raw.get("settings")))

    async def apply_socks_users(
        self, target: int | None, put: Mapping[str, str], remove: Collection[str] = ()
    ) -> list[SocksAccount]:
        """Edit the inbound's ``settings.accounts`` list: add/replace ``put`` (name -> password), drop ``remove``.

        Accounts are not panel clients, so the whole inbound is read, changed and written back. Writers in this
        process are serialized per inbound; a concurrent editor elsewhere (another bot process, an admin in the
        panel) is handled by reading the inbound back and repeating the round if our change is missing. Other
        accounts and every other inbound field are preserved; ``auth`` is forced to ``password`` so the proxy can
        never be open.
        """
        inbound = _require_target(target)
        lock = self._socks_locks.setdefault(inbound, asyncio.Lock())
        async with lock:
            for attempt in range(SOCKS_WRITE_ATTEMPTS):
                raw = await self._read_inbound(inbound)
                body = _socks_update_body(raw, put, remove)
                if body is not None:
                    path = f"{_API}/inbounds/update/{inbound}"
                    _obj(path, await self._t.request("POST", path, json_body=body))
                    raw = await self._read_inbound(inbound)
                accounts = parse_accounts(_as_dict(raw.get("settings")))
                if _socks_applied(accounts, put, remove) and _as_dict(raw.get("settings")).get("auth") == "password":
                    return accounts
                if attempt + 1 < SOCKS_WRITE_ATTEMPTS:
                    await asyncio.sleep(SOCKS_RETRY_DELAY * (attempt + 1))
            raise PanelError(f"3x-ui inbound {inbound}: SOCKS5 accounts were not stored (concurrent edits?)")

    async def delete_socks_user(self, target: int | None, name: str) -> None:
        await self.apply_socks_users(target, {}, [name])


def _socks_update_body(
    raw: Mapping[str, Any], put: Mapping[str, str], remove: Collection[str]
) -> dict[str, Any] | None:
    """Body of ``inbounds/update`` for the wanted account changes; None when the inbound is already as wanted."""
    settings = _as_dict(raw.get("settings"))
    entries = [e for e in settings.get("accounts") or [] if isinstance(e, dict)]
    drop = set(remove)
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    changed = settings.get("auth") != "password"
    for entry in entries:
        user = entry.get("user")
        if not isinstance(user, str):
            kept.append(entry)
        elif user in drop and user not in put:
            changed = True
        elif user in put:
            if user in seen:  # a duplicate of an account we manage: keep one
                changed = True
                continue
            seen.add(user)
            if entry.get("pass") != put[user]:
                entry = {**entry, "pass": put[user]}
                changed = True
            kept.append(entry)
        else:
            kept.append(entry)
    for user, password in put.items():
        if user not in seen:
            kept.append({"user": user, "pass": password})
            changed = True
    if not changed:
        return None
    settings = {**settings, "auth": "password", "accounts": kept}
    body = {key: value for key, value in raw.items() if key != "clientStats"}
    body["settings"] = json.dumps(settings)
    for key in ("streamSettings", "sniffing"):  # string-encoded like ``settings``: the form every version accepts
        if isinstance(body.get(key), dict):
            body[key] = json.dumps(body[key])
    return body


def _socks_applied(accounts: list[SocksAccount], put: Mapping[str, str], remove: Collection[str]) -> bool:
    by_name = {a.user: a.password for a in accounts}
    return all(by_name.get(user) == password for user, password in put.items()) and not any(
        user in by_name for user in remove if user not in put
    )


def _require_target(target: int | None) -> int:
    if target is None or isinstance(target, bool) or not isinstance(target, int):
        raise PanelError("3x-ui operations need an inbound id as target")
    return target
