"""VLESS share links and subscription entry names.

``build_vless`` reproduces the links 3x-ui (v3.11, ``internal/sub/service.go``) generates, byte for byte:

* query keys sorted alphabetically, values escaped like Go's ``url.QueryEscape`` (space -> ``+``);
* ``encryption`` always present (``none`` unless VLESS encryption is configured);
* reality links carry ``support-x25519mlkem768=true``;
* ``flow`` only where Vision can run: raw TCP with TLS/REALITY, or inside VLESS encryption;
* ``extra`` (XHTTP) is compact JSON with sorted keys, like Go's ``json.Marshal`` of a map;
* the remark is escaped like ``encodeURIComponent``; ``?serverDescription=<base64>`` (Happ subtitle)
  is appended after it unescaped.

``build_ss`` does the same for the ``ss://`` links of Shadowsocks-2022 inbounds (userinfo not base64-encoded,
``method:server key:client key`` percent-escaped like ``url.QueryEscape``).

``effective_params`` applies an :class:`~app.db.models.Endpoint` (direct / L4 relay / CDN) on top of the
inbound's cached link parameters, and ``entry_name`` builds the user-facing entry name
("🇩🇪 Германия", "🇩🇪 Германия · LTE").
"""

from __future__ import annotations

import base64
import copy
import ipaddress
import json
import unicodedata
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import quote, quote_plus

from app.config import Settings
from app.db.models import Endpoint, EndpointKind, EndpointProfile, Location
from app.services.panels import shadowsocks

# Happ shows at most 30 characters of a server title and of its description.
REMARK_MAX_LEN = 30
DESCRIPTION_MAX_LEN = 30
ELLIPSIS = "…"
LTE_SUFFIX = " · LTE"
SS_SUFFIX = " · SS"  # entries of Shadowsocks inbounds
# Happ and INCY (premium) hide entries containing this marker on Wi-Fi; v2RayTun matches "LTE" itself.
MOBILE_ONLY_TAG = "only Mobile"

# Keys in the cached inbound params that describe the inbound itself and never become query params.
INBOUND_META_KEYS = frozenset(
    {
        "port",
        "protocol",
        "remark",
        "address",
        "listen",
        shadowsocks.PARAM_METHOD,
        shadowsocks.PARAM_PASSWORD,
        shadowsocks.PARAM_UNSUPPORTED,
    }
)
# Internal hints consumed by build_vless; never emitted.
INTERNAL_KEYS = frozenset({"flow_allowed", "flow"})
# VLESS-only fields an ss:// link never carries.
SS_EXCLUDED_KEYS = frozenset({"encryption", "flow_allowed", "flow"})

REALITY_ONLY_KEYS = ("pbk", "sid", "spx", "pqv", "support-x25519mlkem768")
TLS_ONLY_KEYS = ("alpn", "ech", "vcn", "pcs", "cs")
HANDSHAKE_KEYS = ("sni", "fp")

SECURITY_NONE = "none"
SECURITY_TLS = "tls"
SECURITY_REALITY = "reality"

# Characters 3x-ui escapes in the userinfo part (WHATWG userinfo percent-encode set).
_USERINFO_ESCAPED = frozenset(' "#<>?`{}/:;=@[\\]^|')
# Characters 3x-ui escapes in the host part, besides whitespace and non-printables.
_HOST_ESCAPED = frozenset('"#/<>?@[\\]^`{|}')
# Go's json.Marshal HTML-escapes these; mirror it so `extra` matches the panel's own links.
_GO_JSON_ESCAPES = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", " ": "\\u2028", " ": "\\u2029"}


# ---------------------------------------------------------------------------------------------
# build_vless
# ---------------------------------------------------------------------------------------------
def build_vless(
    uuid: str,
    address: str,
    port: int,
    params: Mapping[str, Any],
    remark: str,
    description: str | None = None,
    flow: str = "",
) -> str:
    """Build a ``vless://`` share link exactly like 3x-ui does.

    ``params`` are normalized link params (``type``, ``security``, ``sni``, ``pbk``, ``extra`` as a dict or
    JSON string, ...). ``None`` values are skipped; booleans become ``true``/``false``; lists are joined with
    ``,``. Fields that do not belong to the effective ``security`` are dropped, so a stale reality key can
    never leak into a TLS link. ``flow`` is emitted only where Vision is allowed and ``params`` does not set
    ``flow_allowed: false``.
    """
    query = _link_query(params, flow)
    authority = f"{_escape_userinfo(uuid)}@{_join_host_port(address, port)}"
    link = f"vless://{authority}?{_encode_query(query)}"
    fragment = _fragment(remark, description)
    return f"{link}#{fragment}" if fragment else link


def build_ss(
    method: str,
    server_key: str,
    client_key: str,
    address: str,
    port: int,
    params: Mapping[str, Any],
    remark: str,
    description: str | None = None,
) -> str:
    """Build an ``ss://`` share link for a Shadowsocks-2022 multi-user inbound exactly like 3x-ui does.

    ``params`` are the inbound's normalized link params (see :func:`ss_link_query`).
    """
    userinfo = ":".join(quote_plus(part, safe="") for part in (method, server_key, client_key))
    link = f"ss://{userinfo}@{_join_host_port(address, port)}"
    if query := ss_link_query(params):
        link += f"?{_encode_query(query)}"
    fragment = _fragment(remark, description)
    return f"{link}#{fragment}" if fragment else link


def preview_ss(
    inbound_params: Mapping[str, Any], endpoint: Endpoint, name: str, placeholder_uuid: str
) -> tuple[str, str, int, dict[str, Any]]:
    """``(link, address, port, params)`` an admin sees for a Shadowsocks endpoint (placeholder keys)."""
    address, port, params = effective_ss(inbound_params, endpoint)
    method = str(inbound_params.get(shadowsocks.PARAM_METHOD) or "")
    if not shadowsocks.key_bytes(method):
        raise ValueError("the inbound's Shadowsocks method is not a 2022 method")
    server_key = str(inbound_params.get(shadowsocks.PARAM_PASSWORD) or "")
    client_key = shadowsocks.derive_client_key(placeholder_uuid, 0, method)
    link = build_ss(method, server_key, client_key, address, port, params, name, endpoint.description)
    return link, address, port, params


def ss_link_query(params: Mapping[str, Any]) -> dict[str, str]:
    """Query of an ``ss://`` link: the transport (``type``, ``path``, ...) and TLS fields; nothing VLESS-specific."""
    query: dict[str, str] = {}
    for key, value in params.items():
        if key in SS_EXCLUDED_KEYS or key in INBOUND_META_KEYS or key.startswith("_") or value is None:
            continue
        query[key] = _param_str(key, value)
    security = query.pop("security", SECURITY_NONE)
    if security == SECURITY_TLS:
        query["security"] = security
        _drop_foreign_security_keys(query, security)
    else:
        for key in (*TLS_ONLY_KEYS, *HANDSHAKE_KEYS):
            query.pop(key, None)
    # SIP002 clients ignore the xray-native http header fields and read ``plugin`` (3x-ui does the same).
    if query.get("type") == "tcp" and query.get("headerType") == "http":
        host = query.get("host", "")
        for key in ("type", "headerType", "host", "path"):
            query.pop(key, None)
        query["plugin"] = f"obfs-local;obfs=http;obfs-host={host}"
    return query


def _link_query(params: Mapping[str, Any], flow: str) -> dict[str, str]:
    query: dict[str, str] = {}
    for key, value in params.items():
        if key in INTERNAL_KEYS or key in INBOUND_META_KEYS or key.startswith("_") or value is None:
            continue
        query[key] = _param_str(key, value)

    query["encryption"] = query.get("encryption") or "none"
    security = query.get("security") or SECURITY_NONE
    query["security"] = security
    _drop_foreign_security_keys(query, security)
    if security == SECURITY_REALITY:
        query.setdefault("support-x25519mlkem768", "true")
    if query.get("cs") and not query.get("fp"):
        query["fp"] = "unsafe"  # xray ignores cipher suites under any uTLS fingerprint
    if flow and params.get("flow_allowed", True) is not False and flow_allowed(params):
        query["flow"] = flow
    return query


def flow_allowed(params: Mapping[str, Any]) -> bool:
    """Whether XTLS Vision can run: raw TCP with TLS/REALITY, or any transport inside VLESS encryption."""
    network = str(params.get("type") or "tcp")
    if network == "xdrive":
        return False
    security = str(params.get("security") or SECURITY_NONE)
    if network in ("tcp", "raw") and security in (SECURITY_TLS, SECURITY_REALITY):
        return True
    return str(params.get("encryption") or "none") not in ("", "none")


def _drop_foreign_security_keys(query: dict[str, str], security: str) -> None:
    if security != SECURITY_REALITY:
        for key in REALITY_ONLY_KEYS:
            query.pop(key, None)
    if security != SECURITY_TLS:
        for key in TLS_ONLY_KEYS:
            query.pop(key, None)
    if security == SECURITY_NONE:
        for key in HANDSHAKE_KEYS:
            query.pop(key, None)


def _param_str(key: str, value: Any) -> str:
    if key == "extra" and isinstance(value, Mapping):
        return go_json(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return ",".join(str(v) for v in value)
    if isinstance(value, Mapping):
        return go_json(value)
    return str(value)


def go_json(value: Any) -> str:
    """Serialize like Go's ``json.Marshal``: compact, sorted keys, UTF-8, HTML characters escaped."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    for char, escaped in _GO_JSON_ESCAPES.items():
        text = text.replace(char, escaped)
    return text


def _encode_query(query: Mapping[str, str]) -> str:
    """Go's ``url.Values.Encode``: keys sorted, ``QueryEscape`` for keys and values."""
    return "&".join(f"{quote_plus(k, safe='')}={quote_plus(query[k], safe='')}" for k in sorted(query))


def _fragment(remark: str, description: str | None) -> str:
    """``encodeURIComponent(remark)`` plus a literal ``?serverDescription=<base64>`` tail (Happ)."""
    fragment = quote(remark, safe="")
    if description:
        encoded = base64.b64encode(description.encode()).decode("ascii")
        fragment += f"?serverDescription={encoded}"
    return fragment


def _escape_userinfo(value: str) -> str:
    out: list[str] = []
    for byte in value.encode():
        char = chr(byte)
        if byte < 0x20 or byte > 0x7E or char in _USERINFO_ESCAPED:
            out.append(f"%{byte:02X}")
        else:
            out.append(char)
    return "".join(out)


def _join_host_port(host: str, port: int) -> str:
    host = host.strip("[]")
    escaped = "".join(
        quote(ch, safe="") if ch.isspace() or not ch.isprintable() or ch in _HOST_ESCAPED else ch for ch in host
    )
    if ":" in escaped:  # IPv6 literal
        return f"[{escaped}]:{port}"
    return f"{escaped}:{port}"


# ---------------------------------------------------------------------------------------------
# Client hello profile
# ---------------------------------------------------------------------------------------------
COMPACT_FINGERPRINT = "firefox"


def compact_reality_hello(params: Mapping[str, Any]) -> dict[str, Any]:
    """REALITY link params that keep the client's TLS ClientHello within a single TCP segment.

    A Chrome fingerprint with the post-quantum X25519MLKEM768 key share makes the hello about 1.8 KB, so it is split
    across two segments; some networks drop the second one and the connection stalls before the handshake. A Firefox
    fingerprint without the post-quantum share stays well below one segment. Other security types are unchanged;
    endpoint overrides are applied afterwards, so an explicit per-endpoint choice still wins.
    """
    result = dict(params)
    if str(result.get("security") or "") == SECURITY_REALITY:
        result["fp"] = COMPACT_FINGERPRINT
        result["support-x25519mlkem768"] = "false"
    return result


# ---------------------------------------------------------------------------------------------
# effective_params
# ---------------------------------------------------------------------------------------------
def effective_params(inbound_params: Mapping[str, Any], endpoint: Endpoint) -> tuple[str, int, dict[str, Any]]:
    """Resolve where and how clients connect for ``endpoint``: ``(address, port, link params)``.

    ``inbound_params`` is the inbound's cached params JSON (must contain ``port`` unless the endpoint sets
    one). ``endpoint.overrides`` (JSON object) is applied on top; a ``null`` value removes a param.

    * ``direct`` / ``relay``: address and port come from the endpoint; overrides apply verbatim
      (a relay normally has none: L4 forwarding keeps every TLS/REALITY parameter).
    * ``cdn``: REALITY cannot pass a CDN, so security defaults to ``tls``; ``sni`` and ``host`` default to the
      endpoint address when it is a hostname.

    When security changes, parameters of the old security are dropped first (as 3x-ui does for hosts):
    leaving REALITY drops ``pbk/sid/spx/pqv/support-x25519mlkem768`` and the REALITY ``sni``.

    Raises ``ValueError`` on malformed overrides or a missing/invalid port.
    """
    params: dict[str, Any] = {k: copy.deepcopy(v) for k, v in inbound_params.items() if k not in INBOUND_META_KEYS}
    overrides = parse_overrides(endpoint.overrides)
    port = _resolve_port(endpoint.port, inbound_params.get("port"))
    address = endpoint.address.strip()
    if not address:
        raise ValueError(f"endpoint {endpoint.id}: empty address")

    base_security = str(params.get("security") or SECURITY_NONE)
    target_security = _target_security(endpoint.kind, base_security, overrides)
    _switch_security(params, base_security, target_security)
    params["security"] = target_security
    if endpoint.kind == EndpointKind.CDN:
        _apply_cdn_defaults(params, address, overrides)
    _apply_overrides(params, overrides)
    return address, port, params


def effective_ss(inbound_params: Mapping[str, Any], endpoint: Endpoint) -> tuple[str, int, dict[str, Any]]:
    """``(address, port, link params)`` of a Shadowsocks inbound behind ``endpoint``.

    Shadowsocks has no SNI or Reality, so only the endpoint's address and port apply; its kind and overrides do not.
    """
    params: dict[str, Any] = {k: copy.deepcopy(v) for k, v in inbound_params.items() if k not in INBOUND_META_KEYS}
    address = endpoint.address.strip()
    if not address:
        raise ValueError(f"endpoint {endpoint.id}: empty address")
    return address, _resolve_port(endpoint.port, inbound_params.get("port")), params


def parse_overrides(raw: str | None) -> dict[str, Any]:
    """Parse ``Endpoint.overrides``; empty means no overrides. Raises ``ValueError`` if not a JSON object."""
    if not raw or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"endpoint overrides are not valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("endpoint overrides must be a JSON object")
    return value


def _resolve_port(endpoint_port: int | None, inbound_port: Any) -> int:
    raw = endpoint_port if endpoint_port is not None else inbound_port
    if isinstance(raw, bool) or raw is None:
        raise ValueError("no port: neither the endpoint nor the inbound params define one")
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid port {raw!r}") from exc
    if not 0 < port < 65536:
        raise ValueError(f"port out of range: {port}")
    return port


def _target_security(kind: EndpointKind, base: str, overrides: Mapping[str, Any]) -> str:
    if overrides.get("security"):
        return str(overrides["security"])
    if kind == EndpointKind.CDN and base == SECURITY_REALITY:
        return SECURITY_TLS
    return base


def _switch_security(params: dict[str, Any], base: str, target: str) -> None:
    if base == target:
        return
    if base == SECURITY_REALITY:
        # The REALITY sni names the camouflage dest, not this endpoint's certificate.
        for key in (*REALITY_ONLY_KEYS, "sni"):
            params.pop(key, None)
    if target != SECURITY_TLS:
        for key in TLS_ONLY_KEYS:
            params.pop(key, None)
    if target == SECURITY_NONE:
        for key in HANDSHAKE_KEYS:
            params.pop(key, None)


def _apply_cdn_defaults(params: dict[str, Any], address: str, overrides: Mapping[str, Any]) -> None:
    if is_ip_address(address):
        return
    # The CDN terminates TLS with a certificate for its own hostname.
    if "sni" not in overrides and params.get("security") != SECURITY_NONE:
        params["sni"] = address
    if "host" not in overrides:
        params["host"] = address


def _apply_overrides(params: dict[str, Any], overrides: Mapping[str, Any]) -> None:
    for key, value in overrides.items():
        if key in INBOUND_META_KEYS or key == "security":  # security is resolved by _target_security
            continue
        if value is None:
            params.pop(key, None)
        else:
            params[key] = copy.deepcopy(value)
    if "extra" in overrides and "x_padding_bytes" not in overrides:
        _sync_padding_param(params)


def _sync_padding_param(params: dict[str, Any]) -> None:
    """Keep the flat sing-box ``x_padding_bytes`` param consistent with an overridden ``extra``."""
    if "x_padding_bytes" not in params:
        return
    extra = params.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except json.JSONDecodeError:
            extra = None
    padding = extra.get("xPaddingBytes") if isinstance(extra, Mapping) else None
    if padding:
        params["x_padding_bytes"] = padding
    else:
        params.pop("x_padding_bytes", None)


def is_ip_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------------------------
def entry_name(
    location: Location | None, endpoint: Endpoint, settings: Settings, *, ordinal: int = 1, protocol_tag: str = ""
) -> str:
    """User-facing name of one subscription entry, at most ``REMARK_MAX_LEN`` characters.

    ``endpoint.remark`` wins; otherwise ``"<flag> <location>"`` (brand name without a location) plus
    ``" · LTE"`` for whitelist endpoints. ``ordinal > 1`` appends a number to tell apart several entries of
    one location. ``protocol_tag`` (e.g. ``" · SS"``) marks a non-default protocol. With ``SUB_HAPP_NETWORK_TAGS``
    whitelist entries end with ``"only Mobile"``. Only the base name is truncated: the protocol tag, the
    ordinal and the network tag always survive.
    """
    custom = (endpoint.remark or "").strip()
    is_whitelist = endpoint.profile == EndpointProfile.WHITELIST
    if custom:
        base = custom
    else:
        base = location_title(location, settings)
        if is_whitelist:
            base += LTE_SUFFIX
    # Both the ordinal (tells entries apart) and the tag (drives client filtering) survive truncation.
    suffix = protocol_tag + (f" {ordinal}" if ordinal > 1 else "")
    if settings.sub_happ_network_tags and is_whitelist and MOBILE_ONLY_TAG not in base:
        suffix += f" {MOBILE_ONLY_TAG}"
    return truncate(base, REMARK_MAX_LEN - len(suffix)) + suffix


def location_title(location: Location | None, settings: Settings) -> str:
    """``"🇩🇪 Германия"``; the brand name when the entry has no location."""
    if location is None:
        return settings.brand_name.strip() or "VPN"
    return f"{location.flag.strip()} {location.name.strip()}".strip()


def truncate(text: str, limit: int) -> str:
    """Shorten ``text`` to at most ``limit`` code points, never splitting a flag or emoji sequence.

    A trailing ``…`` marks the cut. Whitespace before the ellipsis is removed.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    budget = limit - len(ELLIPSIS)
    out = ""
    for cluster in graphemes(text):
        if len(out) + len(cluster) > budget:
            break
        out += cluster
    return (out.rstrip() + ELLIPSIS) if budget > 0 else ELLIPSIS[:limit]


def graphemes(text: str) -> Iterator[str]:
    """Approximate extended grapheme clusters (flags, ZWJ emoji, skin tones, combining marks).

    Good enough for names; avoids a dependency on the ``regex`` package.
    """
    cluster = ""
    for char in text:
        if cluster and _extends(cluster, char):
            cluster += char
            continue
        if cluster:
            yield cluster
        cluster = char
    if cluster:
        yield cluster


def _extends(cluster: str, char: str) -> bool:
    code = ord(char)
    if unicodedata.combining(char) or 0xFE00 <= code <= 0xFE0F or 0x1F3FB <= code <= 0x1F3FF:
        return True  # combining mark, variation selector, skin tone
    if 0xE0020 <= code <= 0xE007F or code == 0x200D or cluster.endswith("‍"):
        return True  # tag sequence (subdivision flags), zero-width joiner
    if _is_regional(char) and _is_regional(cluster[-1]):
        regional = sum(1 for c in cluster if _is_regional(c))
        return regional % 2 == 1  # pair regional indicators into one flag
    return False


def _is_regional(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF
