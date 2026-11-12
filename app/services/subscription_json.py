"""Xray JSON subscription: a JSON array of complete Xray client configs (Happ, v2RayTun).

The configs are built from the same share links the base64 subscription carries, so both formats always agree
(servers, remarks, Reality parameters). Links that cannot be expressed exactly as an Xray outbound are left out.

Layout of one config: local socks/http inbounds with sniffing, the proxy outbound(s), ``direct`` and ``block``,
routing, and a split DNS. With two or more servers an extra "Auto" config comes first: a ``leastPing`` balancer
over every proxy outbound, fed by a burst observatory.

Rule order (first match wins): DNS queries, operator "proxy" overrides, private ranges direct, Russian domains
direct (when enabled), everything else through the VPN.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from collections.abc import Sequence
from typing import Any, Final
from urllib.parse import unquote

from app.config import Settings
from app.services import subscription_json_data as data
from app.utils.xray_outbound import LinkError, outbound_from_link

logger = logging.getLogger(__name__)

MEDIA_TYPE: Final = "application/json; charset=utf-8"
SOCKS_PORT: Final = 10808
HTTP_PORT: Final = 10809
AUTO_TAG: Final = "auto"
_DNS_TAG: Final = "dns-query"
_TAGGED_PREFIX: Final = re.compile(r"^(domain|full|keyword|regexp|geosite|ext):", re.I)
_IPV4: Final = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def client_wants_json(settings: Settings, client: str) -> bool:
    """Whether ``client`` (see ``subscription_page.detect_client``) gets the JSON format."""
    return settings.sub_json_enabled and client in ("happ", "v2raytun")


def normalize_domain(entry: str) -> str:
    """``example.com`` -> ``domain:example.com``; entries that carry an Xray matcher prefix stay as they are."""
    entry = entry.strip()
    return entry if _TAGGED_PREFIX.match(entry) else f"domain:{entry.lower()}"


def build_configs(links: Sequence[str], settings: Settings, language: str | None = "ru") -> list[dict[str, Any]]:
    """Per-server configs for ``links`` (``vless://`` / ``ss://``), the Auto config first when there are 2 or more."""
    servers: list[tuple[str, dict[str, Any], str | None]] = []
    for link in links:
        remark, description = _remark(link)
        try:
            outbound = outbound_from_link(link, "proxy")
        except LinkError as exc:
            logger.info("Entry %r is left out of the JSON subscription: %s", remark, exc)
            continue
        servers.append((remark, outbound, description))
    configs: list[dict[str, Any]] = []
    if settings.sub_json_auto and len(servers) >= 2:
        outbounds = [{**ob, "tag": f"proxy-{i}"} for i, (_, ob, _) in enumerate(servers, 1)]
        configs.append(_config(_auto_remark(settings, language), outbounds, settings, auto=True))
    for remark, outbound, description in servers:
        config = _config(remark, [outbound], settings, auto=False)
        if description:
            config["meta"] = {"serverDescription": description}
        configs.append(config)
    return configs


def render_body(configs: Sequence[dict[str, Any]]) -> str:
    return json.dumps(list(configs), ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------------------------
def _remark(link: str) -> tuple[str, str | None]:
    """Entry name and optional description from the link's fragment (``name?serverDescription=<base64>``)."""
    fragment = link.split("#", 1)[1] if "#" in link else ""
    name, _, tail = fragment.partition("?serverDescription=")
    description: str | None = None
    if tail:
        try:
            description = base64.b64decode(tail, validate=True).decode() or None
        except (binascii.Error, UnicodeDecodeError, ValueError):
            description = None
    return unquote(name), description


def _auto_remark(settings: Settings, language: str | None) -> str:
    brand = settings.brand_name.strip() or "VPN"
    return f"{brand} · {'Авто' if (language or 'ru') == 'ru' else 'Auto'}"


def _target(auto: bool) -> dict[str, str]:
    return {"balancerTag": AUTO_TAG} if auto else {"outboundTag": "proxy"}


def _server_hosts(outbounds: Sequence[dict[str, Any]]) -> list[str]:
    hosts: list[str] = []
    for ob in outbounds:
        ob_settings = ob.get("settings", {})
        for entry in ob_settings.get("vnext") or ob_settings.get("servers") or []:
            host = str(entry.get("address", ""))
            if host and ":" not in host and not _IPV4.match(host) and host not in hosts:
                hosts.append(host)
    return hosts


def _config(remark: str, outbounds: list[dict[str, Any]], settings: Settings, *, auto: bool) -> dict[str, Any]:
    direct = [normalize_domain(d) for d in settings.sub_json_direct_domains]
    if settings.sub_json_direct_ru:
        direct = [*data.RU_TLD_RULES, *data.RU_SERVICE_RULES, *direct]
    direct = list(dict.fromkeys(direct))
    # Overrides only matter when something is routed directly.
    proxy = list(dict.fromkeys(normalize_domain(d) for d in settings.sub_json_proxy_domains)) if direct else []

    sniffing = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}
    rules: list[dict[str, Any]] = []
    if direct:
        rules.append({"type": "field", "inboundTag": [_DNS_TAG], "ip": [data.DOMESTIC_DNS], "outboundTag": "direct"})
    rules.append({"type": "field", "inboundTag": [_DNS_TAG], **_target(auto)})
    if proxy:
        rules.append({"type": "field", "domain": proxy, **_target(auto)})
    rules.append({"type": "field", "ip": list(data.PRIVATE_RANGES), "outboundTag": "direct"})
    if direct:
        rules.append({"type": "field", "domain": direct, "outboundTag": "direct"})
    rules.append({"type": "field", "network": "tcp,udp", **_target(auto)})

    dns_servers: list[Any] = []
    if hosts := _server_hosts(outbounds):
        # The VPN server's own name cannot be resolved through the VPN: use the system resolver.
        dns_servers.append({"address": "localhost", "domains": [f"full:{h}" for h in hosts]})
    if direct:
        if proxy:
            dns_servers.append({"address": data.REMOTE_DNS, "domains": proxy})
        dns_servers.append({"address": data.DOMESTIC_DNS, "domains": direct, "skipFallback": True})
    dns_servers.append(data.REMOTE_DNS)

    config: dict[str, Any] = {
        "remarks": remark,
        "log": {"loglevel": "warning"},
        "dns": {"servers": dns_servers, "queryStrategy": "UseIPv4", "tag": _DNS_TAG},
        "inbounds": [
            {
                "tag": "socks",
                "listen": "127.0.0.1",
                "port": SOCKS_PORT,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": sniffing,
            },
            {
                "tag": "http",
                "listen": "127.0.0.1",
                "port": HTTP_PORT,
                "protocol": "http",
                "settings": {},
                "sniffing": sniffing,
            },
        ],
        "outbounds": [*outbounds, {"tag": "direct", "protocol": "freedom"}, {"tag": "block", "protocol": "blackhole"}],
        "routing": {"domainStrategy": "IPIfNonMatch", "rules": rules},
    }
    if auto:
        config["routing"]["balancers"] = [
            {
                "tag": AUTO_TAG,
                "selector": ["proxy-"],
                "strategy": {"type": "leastPing"},
                "fallbackTag": outbounds[0]["tag"],
            }
        ]
        config["burstObservatory"] = {
            "subjectSelector": ["proxy-"],
            "pingConfig": {"destination": data.PING_URL, "interval": "10m", "sampling": 3, "timeout": "5s"},
        }
    return config
