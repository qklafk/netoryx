"""Static routing data for the Xray JSON subscription (explicit rules only, no geo asset files needed)."""

from __future__ import annotations

from typing import Final

# Top-level domains of Russia: every host below them goes direct.
RU_TLD_RULES: Final = ("domain:ru", "domain:su", "domain:xn--p1ai")  # xn--p1ai is the .rf domain

# Large Russian services that live on other top-level domains.
RU_SERVICE_RULES: Final = (
    "domain:yandex.com",
    "domain:yandex.net",
    "domain:yandex.by",
    "domain:yandex.kz",
    "domain:yastatic.net",
    "domain:vk.com",
    "domain:vk.me",
    "domain:vk-cdn.net",
    "domain:vkuser.net",
    "domain:userapi.com",
    "domain:mycdn.me",
    "domain:sberbank.com",
    "domain:2gis.com",
)

# Local and private networks always connect directly.
PRIVATE_RANGES: Final = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "224.0.0.0/4",
    "255.255.255.255/32",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)

DOMESTIC_DNS: Final = "77.88.8.8"
REMOTE_DNS: Final = "1.1.1.1"
PING_URL: Final = "https://www.google.com/generate_204"
