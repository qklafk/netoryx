"""Static data of the Telegram-only subscription: the networks and domains Telegram uses.

The address ranges are Telegram's published list (https://core.telegram.org/resources/cidr.txt, fetched on
2026-11-13). They change rarely; update this file from that page when they do (no runtime fetching on purpose:
the server-side rules and the client config must always agree).
"""

from __future__ import annotations

from typing import Final

TELEGRAM_IPV4: Final = (
    "91.108.56.0/22",
    "91.108.4.0/22",
    "91.108.8.0/22",
    "91.108.16.0/22",
    "91.108.12.0/22",
    "149.154.160.0/20",
    "91.105.192.0/23",
    "91.108.20.0/22",
    "185.76.151.0/24",
)

TELEGRAM_IPV6: Final = (
    "2001:b28:f23d::/48",
    "2001:b28:f23f::/48",
    "2001:67c:4e8::/48",
    "2001:b28:f23c::/48",
    "2a0a:f280::/32",
)

TELEGRAM_CIDRS: Final = (*TELEGRAM_IPV4, *TELEGRAM_IPV6)

# Domains the Telegram apps and web clients use (``domain:`` also matches every subdomain).
TELEGRAM_DOMAINS: Final = (
    "domain:telegram.org",
    "domain:t.me",
    "domain:telegram.me",
    "domain:telegram.dog",
    "domain:telesco.pe",
    "domain:tg.dev",
    "domain:telegra.ph",
    "domain:graph.org",
    "domain:cdn-telegram.org",
    "domain:telegram-cdn.org",
    "domain:tdesktop.com",
)

# Reachable for the Telegram-only client, so the client's own latency probe works.
PING_URL: Final = "https://telegram.org/"
