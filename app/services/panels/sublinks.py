"""Client links from a panel's own subscription endpoint (PasarGuard, Marzneshin).

These panels expose a user's configuration only through the subscription URL; its plain "links" format lists one
client URI per line. Only the path is taken from the panel's answer (it may carry a public prefix or none at all),
the request always goes to the configured panel URL.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

import aiohttp

from app.services.panels.base import PanelError

LINK_SCHEMES = ("vless://", "vmess://", "trojan://", "ss://", "hysteria2://", "hy2://", "tuic://")
MAX_BYTES = 4 * 1024 * 1024


async def fetch_links(
    session: aiohttp.ClientSession,
    panel_url: str,
    subscription_url: str,
    *,
    fmt_suffix: str = "/links",
    headers: Mapping[str, str] | None = None,
    tls_verify: bool = True,
    name: str = "panel",
) -> list[str]:
    """Client URIs of one subscription; ``[]`` when the panel gave no usable subscription path."""
    path = urlsplit(subscription_url).path.rstrip("/")
    if not path.startswith("/") or ".." in path.split("/"):
        return []
    try:
        async with session.get(
            panel_url.rstrip("/") + path + fmt_suffix,
            headers={**(headers or {}), "Accept": "text/plain"},
            ssl=tls_verify,
            allow_redirects=False,
        ) as response:
            if response.status >= 300:
                raise PanelError(f"{name} subscription request failed (HTTP {response.status})")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise PanelError(f"{name} subscription is too large")
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise PanelError(f"{name} connection failed: {type(exc).__name__}") from exc
    text = body.decode("utf-8", errors="replace")
    return [line.strip() for line in text.splitlines() if line.strip().startswith(LINK_SCHEMES)]
