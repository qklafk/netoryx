"""Connection profiles grouped by the client-facing server address."""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import unquote, urlsplit

from app.db.models import Subscription, User
from app.services import subscription_page, subscriptions
from app.services.qr import make_qr_png_async


async def connection_groups(
    ctx: Any, user: User, sub: Subscription | None, client_headers: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    if (
        sub is None
        or subscription_page.access_state(user, sub, subscriptions.utcnow()) != subscription_page.AccessState.ACTIVE
    ):
        return []
    if not (ctx.settings.sub_show_server_profiles or ctx.settings.sub_show_server_subscriptions):
        return []
    token = subscriptions.token_for(sub, user)
    rendered = await subscription_page.render(
        ctx.session_factory,
        ctx.panels,
        ctx.settings,
        token,
        client_headers=client_headers,
        secret_box=ctx.secret_box,
    )
    if rendered is None or rendered.state != subscription_page.AccessState.ACTIVE:
        return []
    groups: dict[str, dict[str, Any]] = {}
    base = (ctx.settings.public_base_url or "").rstrip("/")
    for uri in rendered.links:
        parsed = urlsplit(uri)
        if parsed.scheme not in ("vless", "ss") or not parsed.hostname:
            continue
        key = subscription_page.server_key(uri)
        if key is None:
            continue
        group = groups.setdefault(
            key,
            {
                "id": key,
                "name": unquote(parsed.fragment) or parsed.hostname,
                "subscription_url": f"{base}/sub/{token}/server/{key}"
                if ctx.settings.sub_show_server_subscriptions
                else "",
                "profiles": [],
            },
        )
        if ctx.settings.sub_show_server_profiles:
            try:
                png = await make_qr_png_async(uri)
                qr = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            except ValueError:
                qr = None
            group["profiles"].append({"name": unquote(parsed.fragment) or parsed.hostname, "uri": uri, "qr": qr})
    return list(groups.values())
