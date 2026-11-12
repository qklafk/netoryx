"""User screens of the Telegram proxy: connect, QR, copy, web proxy, new link, FAQ (/proxy)."""

from __future__ import annotations

import logging
import math
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.core.context import AppContext
from app.core.filters import IsPrivate
from app.core.i18n import Translator
from app.core.ui import Screen, answer_callback, render
from app.db.models import User
from app.keyboards import Nav
from app.modules.tg_proxy.callbacks import NAV_TARGETS, ProxyCb
from app.modules.tg_proxy.views import (
    faq_screen,
    primary_link,
    proxy_screen,
    qr_screen,
    rotate_confirm_screen,
    socks_qr_screen,
)
from app.services import tg_proxy, tg_socks
from app.services.qr import make_qr_png_async
from app.utils.time import utcnow

log = logging.getLogger(__name__)

router = Router(name="tg_proxy")
router.message.filter(IsPrivate())
router.callback_query.filter(IsPrivate())


def wait_text(_: Translator, delta: timedelta) -> str:
    """``"45 минут"`` / ``"2 часа"`` (rounded up)."""
    minutes = max(1, math.ceil(delta.total_seconds() / 60))
    if minutes >= 120:
        return _.plural(math.ceil(minutes / 60), "plural.hours")
    return _.plural(minutes, "plural.minutes")


async def open_screen(ctx: AppContext, user: User, _: Translator) -> Screen:
    """Create/push the user's proxy accounts if needed and build the screen (network I/O outside transactions)."""
    settings = ctx.settings
    ensured = await tg_proxy.ensure_access(
        ctx.session_factory, settings, ctx.secret_box, user.tg_id, max_targets=settings.tg_proxy_max_servers
    )
    if ensured.policy.allowed:
        report = await tg_proxy.sync_user(ctx.session_factory, ctx.panels, settings, ctx.secret_box, user.tg_id)
        if report.failed:
            log.info("tg proxy: push for user %s failed: %s", user.tg_id, "; ".join(report.errors[:3]))
    view = await tg_proxy.user_view(ctx.session_factory, settings, ctx.secret_box, user.tg_id)
    socks = await _socks_view(ctx, user.tg_id)
    return proxy_screen(view, settings, _, no_targets=ensured.no_targets, socks=socks)


async def _socks_view(ctx: AppContext, user_id: int) -> tg_socks.SocksView:
    """Create/push the user's SOCKS5 accounts (when allowed) and read them back."""
    settings = ctx.settings
    ensured = await tg_socks.ensure_access(
        ctx.session_factory, settings, ctx.secret_box, user_id, max_targets=settings.tg_proxy_max_servers
    )
    if ensured.policy.allowed:
        report = await tg_socks.sync_user(ctx.session_factory, ctx.panels, settings, ctx.secret_box, user_id)
        if report.failed:
            log.info("tg socks: push for user %s failed: %s", user_id, "; ".join(report.errors[:3]))
    return await tg_socks.user_view(ctx.session_factory, settings, ctx.secret_box, user_id)


async def show_proxy(event: Message | CallbackQuery, ctx: AppContext, db_user: User, _: Translator) -> None:
    """Render the proxy screen for ``event`` (also used by the ``/start proxy`` deep link)."""
    await render(event, await open_screen(ctx, db_user, _))


@router.message(Command("proxy"))
async def proxy_command(message: Message, ctx: AppContext, db_user: User | None, _: Translator) -> None:
    if db_user is None:
        return
    await show_proxy(message, ctx, db_user, _)


@router.callback_query(Nav.filter(F.to.in_(NAV_TARGETS)))
async def proxy_nav(callback: CallbackQuery, ctx: AppContext, db_user: User | None, _: Translator) -> None:
    if db_user is None:
        await answer_callback(callback)
        return
    await show_proxy(callback, ctx, db_user, _)


@router.callback_query(ProxyCb.filter(F.action == "qr"))
async def proxy_qr(
    callback: CallbackQuery, callback_data: ProxyCb, ctx: AppContext, db_user: User | None, _: Translator
) -> None:
    if db_user is None:
        await answer_callback(callback)
        return
    view = await tg_proxy.user_view(ctx.session_factory, ctx.settings, ctx.secret_box, db_user.tg_id)
    entries = view.entries
    entry = entries[callback_data.arg] if 0 <= callback_data.arg < len(entries) else None
    link = primary_link(entry) if entry is not None and entry.ready else None
    if entry is None or link is None:
        await render(callback, proxy_screen(view, ctx.settings, _), answer=_("tg_proxy.link_gone"))
        return
    png = await make_qr_png_async(link.tme_url)
    await render(callback, qr_screen(entry, png, _))


@router.callback_query(ProxyCb.filter(F.action == "qrs"))
async def proxy_socks_qr(
    callback: CallbackQuery, callback_data: ProxyCb, ctx: AppContext, db_user: User | None, _: Translator
) -> None:
    if db_user is None:
        await answer_callback(callback)
        return
    socks = await tg_socks.user_view(ctx.session_factory, ctx.settings, ctx.secret_box, db_user.tg_id)
    entries = socks.entries
    entry = entries[callback_data.arg] if 0 <= callback_data.arg < len(entries) else None
    if entry is None or entry.link is None:
        view = await tg_proxy.user_view(ctx.session_factory, ctx.settings, ctx.secret_box, db_user.tg_id)
        await render(callback, proxy_screen(view, ctx.settings, _, socks=socks), answer=_("tg_proxy.link_gone"))
        return
    png = await make_qr_png_async(entry.link.tme_url)
    await render(callback, socks_qr_screen(entry, png, _))


@router.callback_query(ProxyCb.filter(F.action == "faq"))
async def proxy_faq(callback: CallbackQuery, ctx: AppContext, db_user: User | None, _: Translator) -> None:
    has_socks = False
    if db_user is not None:
        socks = await tg_socks.user_view(ctx.session_factory, ctx.settings, ctx.secret_box, db_user.tg_id)
        has_socks = bool(socks.ready)
    await render(callback, faq_screen(_, socks=has_socks))


@router.callback_query(ProxyCb.filter(F.action == "rotate"))
async def proxy_rotate_ask(callback: CallbackQuery, _: Translator) -> None:
    await render(callback, rotate_confirm_screen(_))


@router.callback_query(ProxyCb.filter(F.action == "rotate_ok"))
async def proxy_rotate(callback: CallbackQuery, ctx: AppContext, db_user: User | None, _: Translator) -> None:
    if db_user is None:
        await answer_callback(callback)
        return
    settings = ctx.settings
    cooldown = settings.tg_proxy_rotate_cooldown_min
    min_interval = timedelta(minutes=cooldown) if cooldown > 0 else None
    try:
        # The SOCKS5 password is changed together with the MTProto secret (checked first: nothing changes if too soon).
        rotated = await tg_socks.rotate(ctx.session_factory, ctx.secret_box, db_user.tg_id, min_interval=min_interval)
        rotated += await tg_proxy.rotate(ctx.session_factory, ctx.secret_box, db_user.tg_id, min_interval=min_interval)
    except tg_proxy.RotationTooSoonError as exc:
        text = _("tg_proxy.rotate_too_soon", wait=wait_text(_, exc.retry_at - utcnow()))
        await answer_callback(callback, text, alert=True)
        return
    screen = await open_screen(ctx, db_user, _)
    await render(callback, screen, answer=_("tg_proxy.rotated") if rotated else None)
