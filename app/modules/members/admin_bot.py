"""Admin screens of a private VPN: members list and card with extend, pause, limits, devices, new link, remove."""

from __future__ import annotations

import logging

from aiogram import F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.permissions import HasPermission, Permission
from app.core.ui import Screen, answer_callback, render, send_screen
from app.keyboards import Button, Nav, kb
from app.modules.admin.common import admin_router, first_time
from app.modules.members import service
from app.modules.members.callbacks import NAV, UNLIMITED, MemCb
from app.modules.members.views import card_screen, confirm_screen, extend_screen, limits_screen, list_screen
from app.services import devices, invites
from app.services.subscriptions import SubscriptionError
from app.utils.time import utcnow

log = logging.getLogger(__name__)

PAGE_SIZE = 8
CONNECT_NAV = "connect"

router = admin_router("members_admin")
for _observer in (router.message, router.callback_query):
    _observer.filter(HasPermission(Permission.USERS_EDIT))


async def _list(ctx: AppContext, _: Translator, page: int) -> Screen:
    page = max(0, page)
    members, total = await service.list_members(ctx.session_factory, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    pages = max(1, -(-total // PAGE_SIZE))
    if page >= pages and total:
        return await _list(ctx, _, pages - 1)
    return list_screen(_, members, page, pages, total, utcnow())


async def _card(ctx: AppContext, _: Translator, uid: int, page: int) -> Screen | None:
    member = await service.get_member(ctx.session_factory, uid)
    return card_screen(_, member, utcnow(), page) if member is not None else None


async def _tell_member(
    ctx: AppContext, uid: int, key: str, markup: InlineKeyboardMarkup | None = None, **kw: str
) -> None:
    """Best-effort notice to the member in their language."""
    member = await service.get_member(ctx.session_factory, uid)
    if member is None or member.user.bot_blocked:
        return
    i18n = ctx.i18n
    lang = i18n.normalize(member.user.language)
    try:
        await send_screen(ctx.bot, uid, Screen(i18n.t(key, lang, **kw), markup))
    except Exception as exc:
        log.info("members: could not notify %s: %s", uid, exc)


def _connect_kb(ctx: AppContext, lang: str) -> InlineKeyboardMarkup:
    return kb([[Button.cb(ctx.i18n.t("members.btn.connect", lang), Nav(to=CONNECT_NAV))]])


@router.callback_query(Nav.filter(F.to == NAV))
async def open_list(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await _list(ctx, _, 0))


@router.callback_query(MemCb.filter(F.action == "list"))
async def list_page(callback: CallbackQuery, callback_data: MemCb, ctx: AppContext, _: Translator) -> None:
    await render(callback, await _list(ctx, _, callback_data.page))


@router.callback_query(MemCb.filter(F.action.in_({"card", "ext", "lim", "rot", "del"})))
async def member_screens(callback: CallbackQuery, callback_data: MemCb, ctx: AppContext, _: Translator) -> None:
    member = await service.get_member(ctx.session_factory, callback_data.uid)
    page = callback_data.page
    if member is None:
        await render(callback, await _list(ctx, _, page), answer=_("members.gone"))
        return
    action = callback_data.action
    if action == "ext":
        screen = extend_screen(_, member, utcnow(), page)
    elif action == "lim":
        screen = limits_screen(_, member, page)
    elif action in ("rot", "del"):
        screen = confirm_screen(_, member, action, page)
    else:
        screen = card_screen(_, member, utcnow(), page)
    await render(callback, screen)


async def _show_card(callback: CallbackQuery, ctx: AppContext, _: Translator, data: MemCb, answer: str | None) -> None:
    screen = await _card(ctx, _, data.uid, data.page)
    if screen is None:
        await render(callback, await _list(ctx, _, data.page), answer=answer)
        return
    await render(callback, screen, answer=answer)


@router.callback_query(MemCb.filter(F.action == "ext_ok"))
async def extend(callback: CallbackQuery, callback_data: MemCb, ctx: AppContext, _: Translator) -> None:
    days = None if callback_data.value == UNLIMITED else callback_data.value
    try:
        sub = await service.extend(
            ctx.session_factory, ctx.panels, ctx.settings, callback_data.uid, days, callback.from_user.id
        )
    except (service.MemberNotFound, ValueError, SubscriptionError):
        await render(callback, await _list(ctx, _, callback_data.page), answer=_("members.gone"))
        return
    member = await service.get_member(ctx.session_factory, callback_data.uid)
    if member is not None:
        lang = ctx.i18n.normalize(member.user.language)
        if invites.is_unlimited(sub):
            until = ctx.i18n.t("members.notice.forever", lang)
        else:
            until = ctx.i18n.t(
                "members.notice.until", lang, date=ctx.i18n.date(sub.expires_at, lang, ctx.settings.zone)
            )
        await _tell_member(ctx, callback_data.uid, "members.notice.extended", _connect_kb(ctx, lang), until=until)
    await _show_card(callback, ctx, _, callback_data, _("members.extended"))


@router.callback_query(MemCb.filter(F.action.in_({"pause", "resume"})))
async def pause(callback: CallbackQuery, callback_data: MemCb, ctx: AppContext, _: Translator) -> None:
    paused = callback_data.action == "pause"
    try:
        await service.set_paused(
            ctx.session_factory, ctx.panels, ctx.settings, callback_data.uid, paused, callback.from_user.id
        )
    except service.MemberNotFound:
        await render(callback, await _list(ctx, _, callback_data.page), answer=_("members.gone"))
        return
    await _tell_member(ctx, callback_data.uid, "members.notice.paused" if paused else "members.notice.resumed")
    await _show_card(callback, ctx, _, callback_data, _("members.paused" if paused else "members.resumed"))


@router.callback_query(MemCb.filter(F.action.in_({"dev_ok", "gb_ok"})))
async def limits(callback: CallbackQuery, callback_data: MemCb, ctx: AppContext, _: Translator) -> None:
    value = 0 if callback_data.value == UNLIMITED else max(0, callback_data.value)
    devices_changed = callback_data.action == "dev_ok"
    try:
        await service.set_limits(
            ctx.session_factory,
            ctx.panels,
            ctx.settings,
            callback_data.uid,
            callback.from_user.id,
            device_limit=value if devices_changed else None,
            traffic_limit_gb=None if devices_changed else value,
        )
    except service.MemberNotFound:
        await render(callback, await _list(ctx, _, callback_data.page), answer=_("members.gone"))
        return
    member = await service.get_member(ctx.session_factory, callback_data.uid)
    if member is None:
        await render(callback, await _list(ctx, _, callback_data.page))
        return
    await render(callback, limits_screen(_, member, callback_data.page), answer=_("common.saved"))


@router.callback_query(MemCb.filter(F.action == "reset"))
async def reset_devices(callback: CallbackQuery, callback_data: MemCb, ctx: AppContext, _: Translator) -> None:
    try:
        result = await service.reset_devices(ctx.session_factory, ctx.panels, callback_data.uid, callback.from_user.id)
    except (service.MemberNotFound, devices.DeviceError):
        await answer_callback(callback, _("members.reset_failed"), alert=True)
        return
    if result.reset == 0:
        key = "members.reset_failed" if result.failed else "members.reset_nothing"
        await answer_callback(callback, _(key), alert=True)
        return
    await answer_callback(callback, _("members.reset_done"))


@router.callback_query(MemCb.filter(F.action == "rot_ok"))
async def rotate(callback: CallbackQuery, callback_data: MemCb, ctx: AppContext, _: Translator) -> None:
    try:
        await service.rotate_link(ctx.session_factory, callback_data.uid, callback.from_user.id)
    except service.MemberNotFound:
        await render(callback, await _list(ctx, _, callback_data.page), answer=_("members.gone"))
        return
    member = await service.get_member(ctx.session_factory, callback_data.uid)
    if member is not None:
        lang = ctx.i18n.normalize(member.user.language)
        await _tell_member(ctx, callback_data.uid, "members.notice.rotated", _connect_kb(ctx, lang))
    await _show_card(callback, ctx, _, callback_data, _("members.rotated"))


@router.callback_query(MemCb.filter(F.action == "del_ok"))
async def remove(callback: CallbackQuery, callback_data: MemCb, ctx: AppContext, _: Translator) -> None:
    uid = callback_data.uid
    if not first_time(f"members:del:{uid}:{callback.message.message_id if callback.message else 0}"):
        await answer_callback(callback)
        return
    member = await service.get_member(ctx.session_factory, uid)
    if member is None:
        await render(callback, await _list(ctx, _, callback_data.page), answer=_("members.gone"))
        return
    await _tell_member(ctx, uid, "members.notice.removed")
    try:
        complete = await service.remove(ctx.session_factory, ctx.panels, ctx.settings, uid, callback.from_user.id)
    except service.MemberNotFound:
        complete = True
    answer = _("members.removed") if complete else _("members.removed_pending")
    await render(callback, await _list(ctx, _, callback_data.page), answer=answer)
