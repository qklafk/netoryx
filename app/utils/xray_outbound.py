"""Turn a share link (vless / socks / shadowsocks) into an Xray outbound object.

Error messages never contain the link or any part of it: links carry credentials.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Final
from urllib.parse import parse_qs, unquote, urlsplit

SUPPORTED_SCHEMES: Final = ("vless", "socks", "socks5", "ss")
_NETWORKS: Final = {
    "tcp": "tcp",
    "raw": "tcp",
    "ws": "ws",
    "grpc": "grpc",
    "httpupgrade": "httpupgrade",
    "xhttp": "xhttp",
    "splithttp": "xhttp",
}
_DEFAULT_FINGERPRINT: Final = "chrome"
_MAX_LINK_LEN: Final = 4096


class LinkError(ValueError):
    """The share link cannot be used as an outbound (the message is safe to show)."""


def outbound_from_link(link: str, tag: str) -> dict[str, Any]:
    """Parse ``link`` into an Xray outbound tagged ``tag``. Raises :class:`LinkError`."""
    text = (link or "").strip()
    if not text or len(text) > _MAX_LINK_LEN:
        raise LinkError("the link is empty or too long")
    scheme = text.split("://", 1)[0].lower() if "://" in text else ""
    if scheme == "vless":
        outbound = _vless(text)
    elif scheme in ("socks", "socks5"):
        outbound = _socks(text)
    elif scheme == "ss":
        outbound = _shadowsocks(text)
    else:
        raise LinkError("unsupported link: use vless://, socks5:// or ss://")
    return {"tag": tag, **outbound}


# ---------------------------------------------------------------------------------------------
def _split(text: str) -> tuple[str, int, Any]:
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise LinkError("the address or port is invalid") from None
    if not host:
        raise LinkError("the link has no server address")
    if port is None or not 0 < port < 65536:
        raise LinkError("the link has no valid port")
    return host, port, parts


def _query(parts: Any) -> dict[str, str]:
    return {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=False).items() if v}


def _vless(text: str) -> dict[str, Any]:
    host, port, parts = _split(text)
    uuid = unquote(parts.username or "")
    if not uuid:
        raise LinkError("the link has no user id")
    q = _query(parts)
    user: dict[str, Any] = {"id": uuid, "encryption": q.get("encryption") or "none"}
    if q.get("flow"):
        user["flow"] = q["flow"]
    return {
        "protocol": "vless",
        "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
        "streamSettings": _stream(q, host),
    }


def _stream(q: dict[str, str], host: str) -> dict[str, Any]:
    network = _NETWORKS.get((q.get("type") or "tcp").lower())
    if network is None:
        raise LinkError("unsupported transport (use tcp, ws, grpc, httpupgrade or xhttp)")
    stream: dict[str, Any] = {"network": network}
    security = (q.get("security") or "none").lower()
    sni = q.get("sni") or ""
    if security == "tls":
        tls: dict[str, Any] = {}
        if sni:
            tls["serverName"] = sni
        if q.get("fp"):
            tls["fingerprint"] = q["fp"]
        if q.get("alpn"):
            tls["alpn"] = [a for a in q["alpn"].split(",") if a]
        stream["security"] = "tls"
        stream["tlsSettings"] = tls
    elif security == "reality":
        if not q.get("pbk"):
            raise LinkError("a Reality link needs a public key (pbk)")
        stream["security"] = "reality"
        stream["realitySettings"] = {
            "serverName": sni or host,
            "fingerprint": q.get("fp") or _DEFAULT_FINGERPRINT,
            "publicKey": q["pbk"],
            "shortId": q.get("sid", ""),
            "spiderX": q.get("spx", ""),
        }
    elif security != "none":
        raise LinkError("unsupported security (use none, tls or reality)")

    path, header_host = q.get("path") or "/", q.get("host") or ""
    if network == "tcp":
        if (q.get("headerType") or "none") != "none":
            raise LinkError("unsupported tcp header type")
    elif network == "ws":
        ws: dict[str, Any] = {"path": path}
        if header_host:
            ws["headers"] = {"Host": header_host}
        stream["wsSettings"] = ws
    elif network == "grpc":
        grpc: dict[str, Any] = {"serviceName": q.get("serviceName", ""), "multiMode": q.get("mode") == "multi"}
        if q.get("authority"):
            grpc["authority"] = q["authority"]
        stream["grpcSettings"] = grpc
    elif network == "httpupgrade":
        upgrade: dict[str, Any] = {"path": path}
        if header_host:
            upgrade["host"] = header_host
        stream["httpupgradeSettings"] = upgrade
    else:  # xhttp
        xhttp: dict[str, Any] = {"path": path, "mode": q.get("mode") or "auto"}
        if header_host:
            xhttp["host"] = header_host
        if q.get("extra"):
            try:
                extra = json.loads(q["extra"])
            except ValueError:
                raise LinkError("the 'extra' parameter is not valid JSON") from None
            if not isinstance(extra, dict):
                raise LinkError("the 'extra' parameter must be a JSON object")
            xhttp["extra"] = extra
        stream["xhttpSettings"] = xhttp
    return stream


def _b64decode(value: str) -> str | None:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def _socks(text: str) -> dict[str, Any]:
    host, port, parts = _split(text)
    user, password = unquote(parts.username or ""), unquote(parts.password or "")
    if user and not password:  # v2rayN style: socks://base64(user:pass)@host:port
        decoded = _b64decode(user)
        if decoded and ":" in decoded:
            user, password = decoded.split(":", 1)
    server: dict[str, Any] = {"address": host, "port": port}
    if user or password:
        server["users"] = [{"user": user, "pass": password}]
    return {"protocol": "socks", "settings": {"servers": [server]}}


def _shadowsocks(text: str) -> dict[str, Any]:
    body = text.split("://", 1)[1].split("#", 1)[0]
    if "@" not in body.split("?", 1)[0]:  # legacy form: ss://base64(method:password@host:port)
        decoded = _b64decode(body.split("?", 1)[0])
        if decoded is None or "@" not in decoded:
            raise LinkError("the Shadowsocks link is malformed")
        text = "ss://" + decoded
    host, port, parts = _split(text)
    userinfo = parts.username or ""
    if parts.password is not None:  # plain "method:password" (percent-encoded)
        method, password = unquote(userinfo), unquote(parts.password)
    else:
        decoded = _b64decode(unquote(userinfo))
        if decoded is None or ":" not in decoded:
            raise LinkError("the Shadowsocks credentials are malformed")
        method, password = decoded.split(":", 1)
    if not method or not password:
        raise LinkError("the Shadowsocks link lacks a method or password")
    if "plugin" in _query(parts):
        raise LinkError("Shadowsocks plugins are not supported")
    server = {"address": host, "port": port, "method": method, "password": password}
    return {"protocol": "shadowsocks", "settings": {"servers": [server]}}


def validate_domains(domains: list[str]) -> None:
    """Routing domain entries: non-empty, no whitespace."""
    for item in domains:
        if not item or any(ch.isspace() for ch in item):
            raise ValueError("YOUTUBE_EXIT_DOMAINS: entries must be non-empty and contain no spaces")
