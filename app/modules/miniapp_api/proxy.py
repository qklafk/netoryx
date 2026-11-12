"""Personal MTProto proxy for Telegram."""

from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from app.config import Settings
from app.modules.miniapp_api import deps
from app.modules.miniapp_api.deps import TG_PROXY, error, needs
from app.modules.miniapp_api.schemas import ProxyLinkOut, ProxyOut, ProxyServerOut, SocksLinkOut, SocksServerOut
from app.services import tg_proxy, tg_socks
from app.web.auth import MiniAppUser
from app.web.deps import Ctx

log = logging.getLogger(__name__)

router = APIRouter(tags=["Mini App: Telegram proxy"], dependencies=[needs(TG_PROXY)])

OPEN_LIMIT = (20, 600)  # pushes to the proxy servers per user per 10 minutes
ROTATE_LIMIT = (5, 3600)


def max_servers(settings: Settings) -> int:
    return settings.tg_proxy_max_servers


def rotate_cooldown(settings: Settings) -> timedelta:
    return timedelta(minutes=settings.tg_proxy_rotate_cooldown_min)


async def _out(view: tg_proxy.UserProxyView, socks_view: tg_socks.SocksView) -> ProxyOut:
    servers: list[ProxyServerOut] = []
    for entry in view.entries:
        links = [
            ProxyLinkOut(server=link.server, port=link.port, tme_url=link.tme_url, tg_url=link.tg_url)
            for link in entry.links
        ]
        servers.append(
            ProxyServerOut(
                title=entry.title,
                ready=entry.ready,
                links=links,
                web_url=entry.web_url,
                qr=await deps.qr_data_url(links[0].tme_url) if entry.ready and links else None,
            )
        )
    socks: list[SocksServerOut] = []
    for socks_entry in socks_view.entries:
        link = socks_entry.link
        socks.append(
            SocksServerOut(
                title=socks_entry.title,
                ready=socks_entry.ready,
                link=SocksLinkOut(
                    server=link.server,
                    port=link.port,
                    user=link.user,
                    password=link.password,
                    tme_url=link.tme_url,
                    tg_url=link.tg_url,
                )
                if link is not None
                else None,
                qr=await deps.qr_data_url(link.tme_url) if link is not None else None,
            )
        )
    policy = view.policy
    return ProxyOut(
        allowed=policy.allowed, reason=policy.reason.value, limited=policy.limited, servers=servers, socks=socks
    )


async def _open(ctx: Any, user_id: int) -> tuple[tg_proxy.UserProxyView, tg_socks.SocksView]:
    settings: Settings = ctx.settings
    view = await tg_proxy.open_proxy(
        ctx.session_factory, ctx.panels, settings, ctx.secret_box, user_id, max_targets=max_servers(settings)
    )
    socks = await tg_socks.open_socks(
        ctx.session_factory, ctx.panels, settings, ctx.secret_box, user_id, max_targets=max_servers(settings)
    )
    return view, socks


@router.get("/tg-proxy", response_model=ProxyOut, summary="My Telegram proxy links")
async def proxy(
    request: Request,
    ctx: Ctx,
    user: MiniAppUser,
    refresh: Annotated[bool, Query(description="Push the accounts to the servers again")] = False,
) -> ProxyOut:
    view = await tg_proxy.user_view(ctx.session_factory, ctx.settings, ctx.secret_box, user.tg_id)
    socks = await tg_socks.user_view(ctx.session_factory, ctx.settings, ctx.secret_box, user.tg_id)
    if view.policy.allowed and (refresh or not (view.ready or socks.ready)):
        # First visit (or links not pushed yet): create the accounts and push them now.
        await deps.rate_limit(request, ctx, user, "tg_proxy_open", *OPEN_LIMIT)
        view, socks = await _open(ctx, user.tg_id)
    return await _out(view, socks)


@router.post("/tg-proxy/rotate", response_model=ProxyOut, summary="New proxy links (the old ones stop working)")
async def rotate(request: Request, ctx: Ctx, user: MiniAppUser) -> ProxyOut:
    await deps.rate_limit(request, ctx, user, "tg_proxy_rotate", *ROTATE_LIMIT)
    try:
        cooldown = rotate_cooldown(ctx.settings)
        rotated = await tg_socks.rotate(ctx.session_factory, ctx.secret_box, user.tg_id, min_interval=cooldown)
        rotated += await tg_proxy.rotate(ctx.session_factory, ctx.secret_box, user.tg_id, min_interval=cooldown)
    except tg_proxy.RotationTooSoonError as exc:
        retry = max(1, math.ceil((exc.retry_at - deps.now_utc()).total_seconds()))
        raise error(
            ctx,
            user,
            429,
            "proxy_rotate_cooldown",
            headers={"Retry-After": str(retry)},
            extra={"retry_at": exc.retry_at.isoformat()},
            time=deps.translator(ctx, user).datetime(exc.retry_at),
        ) from None
    if rotated == 0:
        raise error(ctx, user, 404, "proxy_not_ready")
    log.info("mini app: user %s rotated the proxy links", user.tg_id)
    return await _out(*await _open(ctx, user.tg_id))
