"""Xray client config of the Telegram-only subscription (Happ, v2RayTun).

One config: Telegram's addresses and domains go through the VPN, everything else connects directly. With two or more
servers a ``leastPing`` balancer picks the fastest one; its latency probe targets a Telegram host because the servers
let this client reach nothing else.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

from app.services import subscription_json as sj
from app.services.links import truncate
from app.services.tg_only_data import PING_URL, TELEGRAM_CIDRS, TELEGRAM_DOMAINS

if TYPE_CHECKING:
    from app.config import Settings

BALANCER_TAG: Final = "telegram"
SELECTOR: Final = "proxy-"


def remark(settings: Settings, language: str | None = None) -> str:
    """``"<brand> · Telegram"``."""
    brand = settings.brand_name.strip() or "VPN"
    return f"{brand} · Telegram"


def build_config(
    outbounds: Sequence[dict[str, Any]], settings: Settings, language: str | None = None
) -> dict[str, Any]:
    """The config for ``outbounds`` (tagged ``proxy-1`` ...); a lone server is used directly, no balancer."""
    balanced = len(outbounds) >= 2
    proxies = [dict(ob) for ob in outbounds]
    if not balanced:
        proxies[0]["tag"] = "proxy"
    target: dict[str, str] = {"balancerTag": BALANCER_TAG} if balanced else {"outboundTag": "proxy"}
    sniffing = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}
    config: dict[str, Any] = {
        "remarks": remark(settings, language),
        "log": {"loglevel": "warning"},
        # Only Telegram uses the VPN: names are resolved by the device itself.
        "dns": {"servers": ["localhost"], "queryStrategy": "UseIP"},
        "inbounds": [
            {
                "tag": "socks",
                "listen": "127.0.0.1",
                "port": sj.SOCKS_PORT,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": sniffing,
            },
            {
                "tag": "http",
                "listen": "127.0.0.1",
                "port": sj.HTTP_PORT,
                "protocol": "http",
                "settings": {},
                "sniffing": sniffing,
            },
        ],
        "outbounds": [*proxies, {"tag": "direct", "protocol": "freedom"}, {"tag": "block", "protocol": "blackhole"}],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {"type": "field", "domain": list(TELEGRAM_DOMAINS), **target},
                {"type": "field", "ip": list(TELEGRAM_CIDRS), **target},
                {"type": "field", "network": "tcp,udp", "outboundTag": "direct"},
            ],
        },
    }
    if balanced:
        config["routing"]["balancers"] = [
            {
                "tag": BALANCER_TAG,
                "selector": [SELECTOR],
                "strategy": {"type": "leastPing"},
                "fallbackTag": proxies[0]["tag"],
            }
        ]
        config["burstObservatory"] = {
            "subjectSelector": [SELECTOR],
            "pingConfig": {"destination": PING_URL, "interval": "10m", "sampling": 3, "timeout": "5s"},
        }
    return config


PROFILE_TITLE_MAX_LEN: Final = 25


def response_headers(settings: Settings) -> dict[str, str]:
    """Metadata headers the apps understand (name and refresh interval of the subscription)."""
    from app.services.subscription_page import b64_header, content_disposition

    title = truncate(remark(settings), PROFILE_TITLE_MAX_LEN)
    return {
        "profile-title": b64_header(title),
        "profile-update-interval": str(max(settings.sub_update_interval_hours, 1)),
        "content-disposition": content_disposition(title),
    }
