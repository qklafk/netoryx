"""Maintenance controls: close the bot with a message and an ETA, testers allowlist, partial shutdown of
features, planned windows."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta

from aiogram import F
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.permissions import HasPermission, Permission
from app.core.ui import Screen, answer_callback, render
from app.keyboards import Button, back, grid, kb
from app.modules.admin.common import admin_input_group, admin_router
from app.modules.admin_maintenance.timeinput import parse_moment, parse_range
from app.services.broadcasts import parse_user_ids
from app.services.maintenance import MAX_MESSAGE_LENGTH, MaintenanceState
from app.utils.time import utcnow

log = logging.getLogger(__name__)

router = admin_router("maintenance_admin")
router.message.filter(HasPermission(Permission.SETTINGS))
router.callback_query.filter(HasPermission(Permission.SETTINGS))

ETA_PRESETS_MIN = (30, 60, 120, 240)
MAX_ALLOWLIST = 200


class MntCb(CallbackData, prefix="mnt"):
    a: str
    v: str = ""


@admin_input_group
class MntStates(StatesGroup):
    eta = State()
    message = State()
    allowlist = State()
    window = State()


def _cb(action: str, value: str = "") -> MntCb:
    return MntCb(a=action, v=value)


def _when(_: Translator, value: datetime | None) -> str:
    return _.datetime(value) if value else "—"


def status_text(ctx: AppContext, _: Translator, state: MaintenanceState, now: datetime) -> str:
    if state.enabled:
        status = _("admin_maintenance.status.on", since=_when(_, state.started_at), eta=_when(_, state.eta))
    elif state.active_window(now) is not None:
        window = state.active_window(now)
        assert window is not None
        status = _("admin_maintenance.status.window", eta=_when(_, window.ends_at))
    else:
        status = _("admin_maintenance.status.off")
    custom = [lang.upper() for lang, text in (("ru", state.message_ru), ("en", state.message_en)) if text]
    message = (
        _("admin_maintenance.message_custom", langs=", ".join(custom))
        if custom
        else _("admin_maintenance.message_default")
    )
    modules = ", ".join(_module_title(ctx, _, name) for name in state.disabled_modules) or _("admin_maintenance.none")
    windows = "\n".join(
        _("admin_maintenance.window_line", start=_.datetime(w.starts_at), end=_.datetime(w.ends_at))
        for w in state.windows
    ) or _("admin_maintenance.none")
    return _(
        "admin_maintenance.home",
        status=status,
        message=message,
        testers=_.number(len(state.allowlist)),
        modules=modules,
        windows=windows,
    )


def _module_title(ctx: AppContext, _: Translator, name: str) -> str:
    module = ctx.modules.get(name)
    return _.esc(module.title(_.lang)) if module is not None else _.esc(name)


async def home_screen(ctx: AppContext, _: Translator) -> Screen:
    state = await ctx.maintenance.state(fresh=True)
    now = utcnow()
    rows: list[list[InlineKeyboardButton | None]] = []
    if state.enabled:
        rows.append([Button.cb(_("admin_maintenance.btn.off"), _cb("off"), style="success")])
        rows.append([Button.cb(_("admin_maintenance.btn.eta"), _cb("eta"))])
    else:
        rows.append([Button.cb(_("admin_maintenance.btn.on"), _cb("eta"), style="danger")])
    rows.append(
        [
            Button.cb(_("admin_maintenance.btn.text_ru"), _cb("msg", "ru")),
            Button.cb(_("admin_maintenance.btn.text_en"), _cb("msg", "en")),
        ]
    )
    if state.message_ru or state.message_en:
        rows.append([Button.cb(_("admin_maintenance.btn.text_reset"), _cb("msgreset"))])
    rows.append([Button.cb(_("admin_maintenance.btn.testers"), _cb("allow"))])
    rows.append([Button.cb(_("admin_maintenance.btn.modules"), _cb("mods"))])
    rows.append([Button.cb(_("admin_maintenance.btn.plan"), _cb("win"))])
    for window in state.windows:
        label = _(
            "admin_maintenance.btn.cancel_window",
            start=window.starts_at.astimezone(ctx.settings.zone).strftime("%d.%m %H:%M"),
        )
        rows.append([Button.cb(label, _cb("wdel", window.id))])
    rows.append([Button.cb(_("nav.refresh"), _cb("home")), back("admin", _)])
    return Screen(status_text(ctx, _, state, now), kb(rows))


def eta_screen(_: Translator, enabled: bool) -> Screen:
    presets = [
        Button.cb(
            _.plural(m // 60, "plural.hours") if m >= 60 else _.plural(m, "plural.minutes"), _cb("seteta", str(m))
        )
        for m in ETA_PRESETS_MIN
    ]
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        *grid(presets, 2),
        [Button.cb(_("admin_maintenance.btn.eta_custom"), _cb("etain"))],
        [Button.cb(_("admin_maintenance.btn.no_eta"), _cb("seteta", "0"))],
        [Button.cb(_("nav.back"), _cb("home"))],
    ]
    key = "admin_maintenance.ask_eta_change" if enabled else "admin_maintenance.ask_eta"
    return Screen(_(key), kb(rows))


async def modules_screen(ctx: AppContext, _: Translator) -> Screen:
    state = await ctx.maintenance.state(fresh=True)
    blocked = set(state.disabled_modules)
    buttons = [
        Button.cb(("⛔️ " if module.name in blocked else "✅ ") + module.title(_.lang), _cb("mod", module.name))
        for module in ctx.modules.enabled(ctx.settings)
        if not module.required
    ]
    rows: list[Sequence[InlineKeyboardButton | None]] = [*grid(buttons, 2), [Button.cb(_("nav.back"), _cb("home"))]]
    return Screen(_("admin_maintenance.modules"), kb(rows))


async def _apply_eta(ctx: AppContext, actor_id: int, eta: datetime | None) -> None:
    current = await ctx.maintenance.state(fresh=True)
    if current.enabled:
        await ctx.maintenance.set_message(
            actor_id, message_ru=current.message_ru, message_en=current.message_en, eta=eta
        )
    else:
        await ctx.maintenance.enable(actor_id, message_ru=current.message_ru, message_en=current.message_en, eta=eta)


# --- home / on / off --------------------------------------------------------------------------------------------------
@router.message(Command("maintenance"))
async def maintenance_command(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(message, await home_screen(ctx, _))


@router.callback_query(MntCb.filter(F.a == "home"))
async def open_home(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, await home_screen(ctx, _))


@router.callback_query(MntCb.filter(F.a == "eta"))
async def ask_eta(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    current = await ctx.maintenance.state(fresh=True)
    await render(callback, eta_screen(_, current.enabled))


@router.callback_query(MntCb.filter(F.a == "seteta"))
async def set_eta(callback: CallbackQuery, callback_data: MntCb, ctx: AppContext, _: Translator) -> None:
    minutes = int(callback_data.v) if callback_data.v.isdigit() else 0
    eta = utcnow() + timedelta(minutes=minutes) if minutes > 0 else None
    await _apply_eta(ctx, callback.from_user.id, eta)
    await render(callback, await home_screen(ctx, _), answer=_("admin_maintenance.saved"))


@router.callback_query(MntCb.filter(F.a == "etain"))
async def ask_eta_input(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.set_state(MntStates.eta)
    text = _("admin_maintenance.ask_eta_input", tz=_.esc(ctx.settings.tz))
    await render(callback, Screen(text, kb([[Button.cb(_("nav.back"), _cb("eta"))]])))


@router.message(MntStates.eta, F.text)
async def got_eta(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    now = utcnow()
    eta = parse_moment(message.text or "", ctx.settings.zone, now)
    if eta is None or eta <= now:
        await render(message, Screen(_("admin_maintenance.error.time"), kb([[Button.cb(_("nav.back"), _cb("eta"))]])))
        return
    await state.clear()
    await _apply_eta(ctx, message.chat.id, eta)
    await render(message, await home_screen(ctx, _))


@router.callback_query(MntCb.filter(F.a == "off"))
async def turn_off(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await ctx.maintenance.disable(callback.from_user.id)
    await render(callback, await home_screen(ctx, _), answer=_("admin_maintenance.turned_off"))


# --- message ----------------------------------------------------------------------------------------------------------
@router.callback_query(MntCb.filter(F.a == "msg"))
async def ask_message(callback: CallbackQuery, callback_data: MntCb, _: Translator, state: FSMContext) -> None:
    lang = "en" if callback_data.v == "en" else "ru"
    await state.set_state(MntStates.message)
    await state.update_data(lang=lang)
    text = _("admin_maintenance.ask_message", lang=lang.upper(), limit=MAX_MESSAGE_LENGTH)
    await render(callback, Screen(text, kb([[Button.cb(_("nav.back"), _cb("home"))]])))


@router.message(MntStates.message, F.text)
async def got_message(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    lang = (await state.get_data()).get("lang", "ru")
    text = (message.text or "").strip()
    if len(text) > MAX_MESSAGE_LENGTH:
        await render(message, Screen(_("admin_maintenance.error.too_long", limit=MAX_MESSAGE_LENGTH)))
        return
    current = await ctx.maintenance.state(fresh=True)
    message_ru = text if lang == "ru" else current.message_ru
    message_en = text if lang == "en" else current.message_en
    await ctx.maintenance.set_message(message.chat.id, message_ru=message_ru, message_en=message_en, eta=current.eta)
    await state.clear()
    await render(message, await home_screen(ctx, _))


@router.callback_query(MntCb.filter(F.a == "msgreset"))
async def reset_message(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    current = await ctx.maintenance.state(fresh=True)
    await ctx.maintenance.set_message(callback.from_user.id, message_ru=None, message_en=None, eta=current.eta)
    await render(callback, await home_screen(ctx, _), answer=_("admin_maintenance.saved"))


# --- testers ----------------------------------------------------------------------------------------------------------
@router.callback_query(MntCb.filter(F.a == "allow"))
async def ask_allowlist(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    current = await ctx.maintenance.state(fresh=True)
    await state.set_state(MntStates.allowlist)
    ids = ", ".join(str(x) for x in current.allowlist) or _("admin_maintenance.none")
    rows: list[list[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_maintenance.btn.testers_clear"), _cb("allowclr")) if current.allowlist else None],
        [Button.cb(_("nav.back"), _cb("home"))],
    ]
    await render(callback, Screen(_("admin_maintenance.ask_testers", ids=ids), kb(rows)))


@router.message(MntStates.allowlist, F.text)
async def got_allowlist(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    ids = [x for x in parse_user_ids(message.text or "") if x > 0]
    if not ids or len(ids) > MAX_ALLOWLIST:
        await render(message, Screen(_("admin_maintenance.error.ids", limit=MAX_ALLOWLIST)))
        return
    await ctx.maintenance.set_allowlist(message.chat.id, ids)
    await state.clear()
    await render(message, await home_screen(ctx, _))


@router.callback_query(MntCb.filter(F.a == "allowclr"))
async def clear_allowlist(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await ctx.maintenance.set_allowlist(callback.from_user.id, [])
    await render(callback, await home_screen(ctx, _), answer=_("admin_maintenance.saved"))


# --- partial maintenance ----------------------------------------------------------------------------------------------
@router.callback_query(MntCb.filter(F.a == "mods"))
async def open_modules(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await modules_screen(ctx, _))


@router.callback_query(MntCb.filter(F.a == "mod"))
async def toggle_module(callback: CallbackQuery, callback_data: MntCb, ctx: AppContext, _: Translator) -> None:
    module = ctx.modules.get(callback_data.v)
    if module is None or module.required:
        await answer_callback(callback, _("error.expired_button"))
        return
    current = await ctx.maintenance.state(fresh=True)
    names = set(current.disabled_modules)
    names.symmetric_difference_update({module.name})
    await ctx.maintenance.set_disabled_modules(callback.from_user.id, sorted(names))
    await render(callback, await modules_screen(ctx, _))


# --- planned windows --------------------------------------------------------------------------------------------------
@router.callback_query(MntCb.filter(F.a == "win"))
async def ask_window(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.set_state(MntStates.window)
    text = _(
        "admin_maintenance.ask_window",
        tz=_.esc(ctx.settings.tz),
        minutes=ctx.settings.maintenance_announce_minutes,
    )
    await render(callback, Screen(text, kb([[Button.cb(_("nav.back"), _cb("home"))]])))


@router.message(MntStates.window, F.text)
async def got_window(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    now = utcnow()
    parsed = parse_range(message.text or "", ctx.settings.zone, now)
    back_kb = kb([[Button.cb(_("nav.back"), _cb("home"))]])
    if parsed is None or parsed[1] <= now:
        await render(message, Screen(_("admin_maintenance.error.window"), back_kb))
        return
    try:
        await ctx.maintenance.schedule(
            message.chat.id,
            parsed[0],
            parsed[1],
            announce_minutes=ctx.settings.maintenance_announce_minutes,
        )
    except ValueError as exc:
        await render(message, Screen(_("admin_maintenance.error.window_rejected", reason=_.esc(exc)), back_kb))
        return
    await state.clear()
    await render(message, await home_screen(ctx, _))


@router.callback_query(MntCb.filter(F.a == "wdel"))
async def cancel_window(callback: CallbackQuery, callback_data: MntCb, ctx: AppContext, _: Translator) -> None:
    removed = await ctx.maintenance.cancel_window(callback.from_user.id, callback_data.v)
    note = _("admin_maintenance.window_canceled" if removed else "admin_maintenance.window_gone")
    await render(callback, await home_screen(ctx, _), answer=note)
