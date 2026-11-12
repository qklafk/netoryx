"""User screens: "My subscription" card, trial, connection guide, QR, devices, link rotation and auto-renewal."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.config import Settings
from app.core.context import AppContext
from app.core.filters import IsPrivate
from app.core.i18n import Translator
from app.core.ui import Screen, answer_callback, render
from app.keyboards import Nav
from app.modules.subscription import views
from app.modules.subscription.callbacks import CONNECT, DEVICES, LINKS, QR, SUB, TRIAL, ConnCb, DevCb, ServerCb, SubAct
from app.modules.subscription.stars_renewal import cancel_stars_renewal
from app.modules.subscription.state import Status, SubState, has_lte_endpoints, load_state, select_subscription
from app.modules.subscription.urls import subscription_url
from app.services import connection_links, deeplinks, devices, events, notifications, provisioning, subscriptions, users
from app.services.qr import make_qr_png_async
from app.services.subscription_page import usage
from app.services.subscriptions import SubErrorCode, SubscriptionError

log = logging.getLogger(__name__)

router = Router(name="subscription")
# User screens live in the private chat only: the access gates do not look at group chats.
router.message.filter(IsPrivate())
router.callback_query.filter(IsPrivate())

HAPP_API_TIMEOUT_SEC = 4.0  # the user is waiting for the screen; the link is cached after the first success
Event = Message | CallbackQuery


# --- helpers ----------------------------------------------------------------------------------------------------------
async def current_state(ctx: AppContext, user_id: int, subscription_id: int | None = None) -> SubState | None:
    return await load_state(ctx.session_factory, user_id, subscription_id=subscription_id)


async def used_bytes(ctx: AppContext, settings: Settings, state: SubState) -> int | None:
    """Traffic of the current period from the panels (cached; never raises)."""
    if state.sub is None or state.status != Status.ACTIVE:
        return None
    try:
        _upload, download = await usage(ctx.session_factory, ctx.panels, state.sub, settings)
    except Exception:
        log.warning("traffic of subscription %s is unavailable", state.sub.id, exc_info=True)
        return None
    return download


async def sync_now(ctx: AppContext, settings: Settings, sub_id: int) -> bool:
    """Push a changed subscription to the panels right away; the reconciler retries whatever fails."""
    try:
        result = await provisioning.sync_subscription(ctx.session_factory, ctx.panels, settings, sub_id)
    except Exception:
        log.exception("sync of subscription %s failed; the reconciler will retry", sub_id)
        return False
    return result.ok


async def show_card(
    event: Event,
    ctx: AppContext,
    settings: Settings,
    _: Translator,
    *,
    header: str | None = None,
    answer: str | None = None,
    subscription_id: int | None = None,
) -> None:
    state = await require_state(event, ctx, _, subscription_id)
    if state is None:
        return
    traffic = await used_bytes(ctx, settings, state)
    await render(event, views.sub_card(_, settings, state, used_bytes=traffic, header=header), answer=answer)


async def require_state(
    event: Event, ctx: AppContext, _: Translator, subscription_id: int | None = None
) -> SubState | None:
    """The sender's state, or ``None`` after telling them the screen is unavailable (unregistered sender)."""
    state = await current_state(ctx, event.from_user.id, subscription_id) if event.from_user else None
    if state is None or (subscription_id is not None and state.sub is None):
        state = None
        await alert(event, _("error.unavailable"))
    return state


async def alert(event: Event, text: str) -> None:
    """A short notice: a popup for buttons, a message for commands."""
    if isinstance(event, CallbackQuery):
        await answer_callback(event, text, alert=True)
    else:
        await event.answer(text)


# --- subscription card ------------------------------------------------------------------------------------------------
@router.message(Command("sub"))
@router.callback_query(Nav.filter(F.to == SUB))
async def open_card(
    event: Event, ctx: AppContext, settings: Settings, _: Translator, callback_data: Nav | None = None
) -> None:
    arg = callback_data.arg if callback_data is not None else ""
    sid = int(arg) if arg.isdecimal() else None
    state = await require_state(event, ctx, _, sid)
    if state is None:
        return
    if state.total_subscriptions > 1 and sid is None:
        async with ctx.session_factory() as session:
            items = await subscriptions.list_for_user(session, state.user.tg_id)
        await render(event, views.subscription_list(_, items, shop=settings.app_mode == "shop"))
        return
    if sid is not None:
        await select_subscription(ctx.session_factory, state.user.tg_id, sid)
    await show_card(event, ctx, settings, _, subscription_id=sid)


@router.callback_query(SubAct.filter(F.act.in_({"ar0", "ar1"})))
async def toggle_auto_renew(
    callback: CallbackQuery, callback_data: SubAct, ctx: AppContext, settings: Settings, _: Translator
) -> None:
    state = await require_state(callback, ctx, _, callback_data.s or None)
    if state is None or state.sub is None:
        return
    sid = state.sub.id
    on = callback_data.act == "ar1"
    if not on and not await cancel_stars_renewal(ctx.bot, ctx.session_factory, callback.from_user.id, sid):
        # Telegram would keep charging Stars: keep auto-renewal on until the cancellation goes through.
        await alert(callback, _("subscription.auto_renew.stars_failed"))
        return
    async with ctx.session_factory.begin() as session:
        sub = await subscriptions.lock_for_user(session, callback.from_user.id, sid)
        if sub is not None:
            await subscriptions.set_auto_renew(session, sub, on)
            await events.track(session, "subscription.auto_renew", user_id=callback.from_user.id, on=on)
    popup = _("subscription.auto_renew.enabled" if on else "subscription.auto_renew.disabled")
    await show_card(callback, ctx, settings, _, answer=popup, subscription_id=sid)


@router.callback_query(SubAct.filter(F.act == "rot"))
async def ask_rotate(callback: CallbackQuery, callback_data: SubAct, ctx: AppContext, _: Translator) -> None:
    state = await require_state(callback, ctx, _, callback_data.s or None)
    if state is not None and state.sub is not None:
        await render(callback, views.rotate_confirm(_, state.sub.id))


@router.callback_query(SubAct.filter(F.act == "rot!"))
async def rotate_link(
    callback: CallbackQuery, callback_data: SubAct, ctx: AppContext, settings: Settings, _: Translator
) -> None:
    state = await require_state(callback, ctx, _, callback_data.s or None)
    if state is None or state.sub is None:
        return
    async with ctx.session_factory.begin() as session:
        user = await users.get(session, callback.from_user.id)
        if user is None:
            token = None
        else:
            sub = await subscriptions.lock_for_user(session, user.tg_id, state.sub.id)
            token = await subscriptions.rotate_token(session, user, sub) if sub is not None else None
            await events.track(session, "subscription.link_rotated", user_id=user.tg_id)
    url = subscription_url(settings, token) if token else None
    await render(callback, views.rotated(_, url, state.sub.id), answer=_("subscription.rotate.popup"))


# --- trial ------------------------------------------------------------------------------------------------------------
@router.callback_query(Nav.filter(F.to == TRIAL))
async def start_trial(callback: CallbackQuery, ctx: AppContext, settings: Settings, _: Translator) -> None:
    user_id = callback.from_user.id
    if settings.app_mode != "shop":
        await alert(callback, _("subscription.trial.unavailable"))
        return
    try:
        async with ctx.session_factory.begin() as session:
            user = await users.get(session, user_id)
            if user is None:
                raise SubscriptionError(SubErrorCode.TRIAL_USED)
            sub = await subscriptions.start_trial(session, settings, user)
            sub_id = sub.id
    except SubscriptionError as exc:
        key = {
            SubErrorCode.TRIAL_DISABLED: "subscription.trial.unavailable",
            SubErrorCode.BANNED: "subscription.error.banned",
        }.get(exc.code, "subscription.trial.used")
        await alert(callback, _(key))
        await show_card(callback, ctx, settings, _)
        return
    await sync_now(ctx, settings, sub_id)
    state = await current_state(ctx, user_id)
    if state is None:
        return
    await render(callback, views.trial_started(_, settings, state))
    who = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.full_name
    lang = ctx.i18n.default_lang
    text = ctx.i18n.t("subscription.admin.trial", lang, user=ctx.i18n.esc(who), user_id=user_id)
    notifications.notify(ctx, notifications.AdminEvent.USERS, text)


# --- connection -------------------------------------------------------------------------------------------------------
@router.message(Command("connect"))
@router.callback_query(Nav.filter(F.to == CONNECT))
async def open_connect(
    event: Event, ctx: AppContext, settings: Settings, _: Translator, callback_data: Nav | None = None
) -> None:
    sid = int(callback_data.arg) if callback_data and callback_data.arg.isdecimal() else None
    await show_connect(event, ctx, settings, _, subscription_id=sid)


async def show_connect(
    event: Event, ctx: AppContext, settings: Settings, _: Translator, *, subscription_id: int | None = None
) -> None:
    state = await require_state(event, ctx, _, subscription_id)
    if state is None:
        return
    lte = state.active and await has_lte_endpoints(ctx.session_factory)
    await render(event, views.connect_screen(_, settings, state, lte=lte))


@router.callback_query(ConnCb.filter())
async def open_platform_or_app(
    callback: CallbackQuery, callback_data: ConnCb, ctx: AppContext, settings: Settings, _: Translator
) -> None:
    platform = views.known_platform(callback_data.p)
    if platform is None:
        await show_connect(callback, ctx, settings, _, subscription_id=callback_data.s or None)
        return
    state = await require_state(callback, ctx, _, callback_data.s or None)
    if state is None:
        return
    sid = state.sub.id if state.sub else 0
    app = views.known_app(callback_data.a) if callback_data.a else None
    if app is None:
        await render(callback, views.platform_screen(_, platform, sid))
        return
    state = await require_state(callback, ctx, _, sid or None)
    if state is None:
        return
    crypt = await crypt_link(ctx, settings, state, app.key) if state.active else None
    await render(callback, views.app_screen(_, settings, state, platform, app, crypt_link=crypt))


async def crypt_link(ctx: AppContext, settings: Settings, state: SubState, app_key: str) -> str | None:
    """Encrypted import link for Happ / INCY when enabled (hides the subscription URL from chat scanners)."""
    url = subscription_url(settings, state.token)
    if url is None:
        return None
    try:
        if app_key == "happ" and settings.sub_happ_crypto_links:
            return await deeplinks.happ_crypt_link(ctx.session_factory, ctx.http, url, timeout_sec=HAPP_API_TIMEOUT_SEC)
        if app_key == "incy" and settings.sub_incy_crypto_links:
            return deeplinks.incy_crypt_link(url, settings.profile_title)
    except Exception:
        log.warning("crypt link for %s is unavailable", app_key, exc_info=True)
    return None


@router.callback_query(Nav.filter(F.to == QR))
async def send_qr(
    callback: CallbackQuery, ctx: AppContext, settings: Settings, _: Translator, callback_data: Nav | None = None
) -> None:
    sid = int(callback_data.arg) if callback_data and callback_data.arg.isdecimal() else None
    state = await require_state(callback, ctx, _, sid)
    if state is None:
        return
    url = subscription_url(settings, state.token) if state.active else None
    if url is None:
        await show_connect(callback, ctx, settings, _)
        return
    png = await make_qr_png_async(url)
    await render(callback, views.qr_screen(_, png, state.sub.id if state.sub else 0))


# --- devices ----------------------------------------------------------------------------------------------------------
@router.callback_query(Nav.filter(F.to == DEVICES))
async def open_devices(
    callback: CallbackQuery, ctx: AppContext, settings: Settings, _: Translator, callback_data: Nav | None = None
) -> None:
    sid = int(callback_data.arg) if callback_data and callback_data.arg.isdecimal() else None
    await show_devices(callback, ctx, settings, _, subscription_id=sid)


async def show_devices(
    callback: CallbackQuery,
    ctx: AppContext,
    settings: Settings,
    _: Translator,
    *,
    answer: str | None = None,
    subscription_id: int | None = None,
) -> None:
    state = await require_state(callback, ctx, _, subscription_id)
    if state is None:
        return
    panels: list[devices.PanelDevices] = []
    if state.active and state.sub is not None:
        panels = await devices.list_devices(ctx.session_factory, ctx.panels, state.sub.id)
    await render(callback, views.devices_screen(_, settings, state, panels), answer=answer)


@router.callback_query(DevCb.filter(F.act == "all"))
async def ask_reset_devices(
    callback: CallbackQuery, callback_data: DevCb, ctx: AppContext, settings: Settings, _: Translator
) -> None:
    state = await require_state(callback, ctx, _, callback_data.s or None)
    if state is None:
        return
    if not state.active:
        await render(callback, views.inactive_screen(_, settings, state))
        return
    await render(callback, views.reset_confirm(_, state))


@router.callback_query(DevCb.filter(F.act.in_({"all!", "del"})))
async def reset_devices(
    callback: CallbackQuery, callback_data: DevCb, ctx: AppContext, settings: Settings, _: Translator
) -> None:
    state = await require_state(callback, ctx, _, callback_data.s or None)
    if state is None:
        return
    if not state.active or state.sub is None:
        await render(callback, views.inactive_screen(_, settings, state))
        return
    sub_id = state.sub.id
    device_id: str | None = None
    panel_id: int | None = None
    if callback_data.act == "del":
        device_id = await _device_id(ctx, sub_id, callback_data.panel, callback_data.i)
        if device_id is None:
            await show_devices(
                callback, ctx, settings, _, answer=_("subscription.devices.gone"), subscription_id=sub_id
            )
            return
        panel_id = callback_data.panel
    try:
        result = await devices.reset(
            ctx.session_factory,
            ctx.panels,
            sub_id,
            device_id=device_id,
            panel_id=panel_id,
            cooldown=devices.cooldown_from(settings),
        )
    except devices.ResetCooldown as exc:
        await alert(callback, views.cooldown_text(_, exc.retry_after))
        return
    except devices.DeviceNotSupported:
        await alert(callback, _("subscription.devices.not_supported"))
        return
    if result.ok:
        key = "subscription.devices.deleted" if device_id else "subscription.devices.reset_done"
    elif result.reset:
        key = "subscription.devices.reset_partial"
    else:
        key = "subscription.devices.reset_failed"
    await show_devices(callback, ctx, settings, _, answer=_(key), subscription_id=sub_id)


async def _device_id(ctx: AppContext, sub_id: int, panel_id: int, index: int) -> str | None:
    """Resolve a device by its position in a fresh list (ids are too long for callback data)."""
    for entry in await devices.list_devices(ctx.session_factory, ctx.panels, sub_id):
        if entry.panel_id == panel_id and entry.can_delete_one and 0 <= index < len(entry.devices):
            return entry.devices[index].id
    return None


@router.callback_query(Nav.filter(F.to == LINKS))
async def my_links(
    callback: CallbackQuery, callback_data: Nav, ctx: AppContext, settings: Settings, _: Translator
) -> None:
    sid = int(callback_data.arg) if callback_data.arg.isdecimal() else None
    state = await require_state(callback, ctx, _, sid)
    if state is None:
        return
    if not state.active:
        await render(callback, views.inactive_screen(_, settings, state))
        return
    groups = await connection_links.connection_groups(ctx, state.user, state.sub)
    await render(callback, views.server_list(_, state, groups))


@router.callback_query(ServerCb.filter())
async def server_link(
    callback: CallbackQuery, callback_data: ServerCb, ctx: AppContext, settings: Settings, _: Translator
) -> None:
    state = await require_state(callback, ctx, _, callback_data.s)
    if state is None:
        return
    if not state.active:
        await render(callback, views.inactive_screen(_, settings, state))
        return
    groups = await connection_links.connection_groups(ctx, state.user, state.sub)
    group = next((item for item in groups if item["id"] == callback_data.server), None)
    if group is None:
        await render(callback, views.server_list(_, state, groups))
        return
    if callback_data.i < 0:
        if callback_data.q and group.get("subscription_url"):
            subscription_qr = await make_qr_png_async(group["subscription_url"])
            screen = views.server_detail(_, state, group)
            await render(callback, Screen(_("subscription.connect.qr"), screen.kb, photo=subscription_qr))
        else:
            await render(callback, views.server_detail(_, state, group))
        return
    if callback_data.i >= len(group["profiles"]):
        await render(callback, views.server_detail(_, state, group))
        return
    uri = str(group["profiles"][callback_data.i]["uri"])
    png: bytes | None = None
    if callback_data.q:
        try:
            png = await make_qr_png_async(uri)
        except ValueError:
            await alert(callback, _("subscription.multi.qr_unavailable"))
            return
    elif len(uri) >= 3500 and isinstance(callback.message, Message):
        await callback.message.answer_document(BufferedInputFile(uri.encode(), filename="vpn-profile.txt"))
    await render(callback, views.server_profile(_, state, group, callback_data.i, qr=png))
