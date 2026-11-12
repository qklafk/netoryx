"""Settings from the bot: quick toggles (features, trial, referral mode, Telegram proxy...) and every runtime
setting of the registry, grouped, with validation. Secrets are write-only."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import time
from typing import Any

from aiogram import F
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from pydantic import SecretStr

from app.config import Settings
from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.modules import SHOP_ONLY_MODULES, module_enabled
from app.core.permissions import HasPermission, Permission, has_permission
from app.core.settings_provider import SettingValidationError
from app.core.settings_registry import GROUPS, REGISTRY, SettingSpec, by_group
from app.core.ui import Screen, answer_callback, delete_message, render
from app.db.models import AdminRole
from app.keyboards import Button, back, grid, kb, paginate
from app.modules.admin.common import admin_input_group, admin_router

log = logging.getLogger(__name__)

router = admin_router("settings_admin")
router.message.filter(HasPermission(Permission.SETTINGS))
router.callback_query.filter(HasPermission(Permission.SETTINGS))

# Shown on the first screen: the switches owners reach for most often.
QUICK_KEYS: tuple[str, ...] = (
    "trial_days",
    "referral_enabled",
    "referral_reward_mode",
    "tg_proxy_access",
    "gifts_enabled",
    "donations_enabled",
    "legal_consent_required",
    "miniapp_enabled",
)
TRIAL_PRESETS = (0, 1, 3, 7)
SPECS_PER_PAGE = 10
MAX_VALUE_PREVIEW = 300


class SetCb(CallbackData, prefix="aset"):
    """``a`` = action, ``k`` = setting key / group / module, ``v`` = value (choice index, page...),
    ``o`` = the screen to return to ("q" = quick settings, else a group)."""

    a: str
    k: str = ""
    v: str = ""
    o: str = ""


@admin_input_group
class SettingStates(StatesGroup):
    value = State()


def _cb(action: str, key: str = "", value: str = "", origin: str = "") -> SetCb:
    return SetCb(a=action, k=key, v=value, o=origin)


def can_edit(spec: SettingSpec, role: AdminRole | None) -> bool:
    return has_permission(role, spec.permission)


def format_value(_: Translator, spec: SettingSpec, value: Any) -> str:
    """Display form of a setting value (HTML-escaped; secrets are never shown)."""
    if spec.secret or isinstance(value, SecretStr):
        return _("admin_settings.secret_set") if value else _("admin_settings.secret_unset")
    if value is None or value == "" or value == []:
        return _("admin_settings.empty")
    if isinstance(value, bool):
        return _("common.on") if value else _("common.off")
    if isinstance(value, time):
        return value.strftime("%H:%M")
    text = ", ".join(str(item) for item in value) if isinstance(value, list | tuple) else str(value)
    if len(text) > MAX_VALUE_PREVIEW:
        text = text[: MAX_VALUE_PREVIEW - 1] + "…"
    return _.esc(text)


def _value(ctx: AppContext, key: str) -> tuple[Any, str]:
    value, origin = ctx.settings_provider.effective(key)
    return value, origin


def _quick_label(ctx: AppContext, _: Translator, key: str) -> str:
    spec = REGISTRY[key]
    value, _origin = _value(ctx, key)
    return f"{spec.label(_.lang)}: {format_value(_, spec, value)}"


# --- screens ----------------------------------------------------------------------------------------------------------
def home_screen(ctx: AppContext, _: Translator) -> Screen:
    settings = ctx.settings
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_settings.btn.modules"), _cb("mods"))],
    ]
    for key in QUICK_KEYS:
        if key in REGISTRY:
            rows.append([Button.cb(_quick_label(ctx, _, key)[:60], _cb("spec", key, origin="q"))])
    rows.append([Button.cb(_("admin_settings.btn.all"), _cb("groups"))])
    rows.append([back("admin", _)])
    text = _("admin_settings.home", mode=_(f"admin_settings.mode.{settings.app_mode}"))
    return Screen(text, kb(rows))


def modules_screen(ctx: AppContext, _: Translator, role: AdminRole | None) -> Screen:
    settings = ctx.settings
    editable = can_edit(REGISTRY["disabled_modules"], role) if "disabled_modules" in REGISTRY else False
    buttons: list[InlineKeyboardButton] = []
    for module in ctx.modules.all():
        if module.required:
            continue
        if settings.app_mode == "personal" and (module.shop_only or module.name in SHOP_ONLY_MODULES):
            icon = "🔒"
        elif module_enabled(module, settings):
            icon = "✅"
        elif module.name in settings.disabled_modules:
            icon = "⛔️"
        else:
            icon = "⚪️"
        buttons.append(Button.cb(f"{icon} {module.title(_.lang)}", _cb("mod", module.name)))
    rows: list[Sequence[InlineKeyboardButton | None]] = [*grid(buttons, 2), [Button.cb(_("nav.back"), _cb("home"))]]
    key = "admin_settings.modules" if editable else "admin_settings.modules_readonly"
    return Screen(_(key), kb(rows))


def groups_screen(_: Translator) -> Screen:
    buttons = [Button.cb(GROUPS[group][1 if _.lang == "en" else 0], _cb("group", group)) for group in by_group()]
    rows: list[Sequence[InlineKeyboardButton | None]] = [*grid(buttons, 2), [Button.cb(_("nav.back"), _cb("home"))]]
    return Screen(_("admin_settings.groups"), kb(rows))


def group_screen(ctx: AppContext, _: Translator, group: str, page: int) -> Screen:
    specs = by_group().get(group, [])
    if not specs:
        return Screen(_("admin_settings.not_found"), kb([[Button.cb(_("nav.back"), _cb("groups"))]]))

    def button(spec: SettingSpec) -> InlineKeyboardButton:
        value, _origin = _value(ctx, spec.key)
        return Button.cb(
            f"{spec.label(_.lang)}: {format_value(_, spec, value)}"[:60], _cb("spec", spec.key, origin=group)
        )

    page_data = paginate(specs, page, SPECS_PER_PAGE, button, lambda p: _cb("group", group, str(p)))
    rows: list[Sequence[InlineKeyboardButton | None]] = [*page_data.rows, [Button.cb(_("nav.back"), _cb("groups"))]]
    title = GROUPS[group][1 if _.lang == "en" else 0]
    return Screen(_("admin_settings.group", title=_.esc(title)), kb(rows))


def spec_screen(
    ctx: AppContext, _: Translator, key: str, origin_screen: str, role: AdminRole | None, note: str | None = None
) -> Screen:
    spec = REGISTRY.get(key)
    if spec is None:
        return Screen(_("admin_settings.not_found"), kb([[Button.cb(_("nav.back"), _cb("home"))]]))
    value, origin = _value(ctx, key)
    editable = can_edit(spec, role)
    shown = format_value(_, spec, value) if editable or not spec.secret else _("admin_settings.hidden")
    lines = [
        _(
            "admin_settings.spec",
            label=_.esc(spec.label(_.lang)),
            value=shown,
            origin=_(f"admin_settings.origin.{origin}"),
        )
    ]
    if spec.help(_.lang):
        lines.append(_.esc(spec.help(_.lang)))
    if spec.restart_required:
        lines.append(_("admin_settings.restart"))
    if not editable:
        lines.append(_("admin_settings.owner_only"))
    if note:
        lines.append(note)
    back_to = _cb("home") if origin_screen == "q" else _cb("group", origin_screen)
    rows: list[Sequence[InlineKeyboardButton | None]] = []
    if editable:
        rows.extend(_edit_rows(_, spec, value, origin_screen))
        if origin == "db":
            rows.append([Button.cb(_("admin_settings.btn.reset"), _cb("reset", key, origin=origin_screen))])
    rows.append([Button.cb(_("nav.back"), back_to)])
    return Screen("\n\n".join(lines), kb(rows))


def _edit_rows(
    _: Translator, spec: SettingSpec, value: Any, origin_screen: str
) -> list[Sequence[InlineKeyboardButton | None]]:
    key = spec.key
    if spec.type == "bool":
        label = _("admin_settings.btn.turn_off") if value else _("admin_settings.btn.turn_on")
        return [[Button.cb(label, _cb("set", key, "0" if value else "1", origin_screen))]]
    if spec.type == "choice" and spec.choices:
        buttons = [
            Button.cb(("• " if str(value) == choice else "") + choice, _cb("set", key, f"c{index}", origin_screen))
            for index, choice in enumerate(spec.choices)
        ]
        return list(grid(buttons, 2))
    if spec.type == "json":
        return [[Button.cb(_("admin_settings.btn.web_only"), _cb("webonly", key))]]
    rows: list[Sequence[InlineKeyboardButton | None]] = []
    if key == "trial_days":
        rows.append(
            [
                Button.cb(
                    _.plural(days, "plural.days") if days else _("common.off"),
                    _cb("set", key, f"i{days}", origin_screen),
                )
                for days in TRIAL_PRESETS
            ]
        )
    rows.append([Button.cb(_("admin_settings.btn.edit"), _cb("ask", key, origin=origin_screen))])
    if spec.nullable and value not in (None, ""):
        rows.append([Button.cb(_("admin_settings.btn.clear"), _cb("set", key, "n", origin_screen))])
    return rows


def _decode_button_value(spec: SettingSpec, raw: str) -> Any:
    if spec.type == "bool":
        return raw == "1"
    if raw.startswith("c") and spec.choices:
        index = int(raw[1:])
        return spec.choices[index]
    if raw.startswith("i"):
        return int(raw[1:])
    if raw == "n":
        return ""
    raise ValueError(raw)


async def _store(ctx: AppContext, _: Translator, key: str, value: Any, actor_id: int) -> str | None:
    """Save a value; returns an error text for the admin or None on success."""
    try:
        await ctx.settings_provider.set(key, value, actor_id)
    except SettingValidationError as exc:
        return _("admin_settings.invalid", error=_.esc(str(exc)))
    except KeyError:
        return _("admin_settings.not_editable")
    return None


# --- handlers ---------------------------------------------------------------------------------------------------------
@router.callback_query(SetCb.filter(F.a == "home"))
async def open_home(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, home_screen(ctx, _))


@router.callback_query(SetCb.filter(F.a == "mods"))
async def open_modules(
    callback: CallbackQuery, ctx: AppContext, _: Translator, admin_role: AdminRole | None = None
) -> None:
    await render(callback, modules_screen(ctx, _, admin_role))


@router.callback_query(SetCb.filter(F.a == "mod"))
async def toggle_module(
    callback: CallbackQuery, callback_data: SetCb, ctx: AppContext, _: Translator, admin_role: AdminRole | None = None
) -> None:
    spec = REGISTRY.get("disabled_modules")
    module = ctx.modules.get(callback_data.k)
    if spec is None or not can_edit(spec, admin_role):
        await answer_callback(callback, _("error.not_allowed"), alert=True)
        return
    settings: Settings = ctx.settings
    if module is None or module.required:
        await answer_callback(callback, _("error.expired_button"))
        return
    if settings.app_mode == "personal" and (module.shop_only or module.name in SHOP_ONLY_MODULES):
        await answer_callback(callback, _("admin_settings.shop_only"), alert=True)
        return
    disabled = set(settings.disabled_modules)
    disabled.symmetric_difference_update({module.name})
    error = await _store(ctx, _, "disabled_modules", sorted(disabled), callback.from_user.id)
    if error:
        await answer_callback(callback, error, alert=True)
        return
    turned_on = module.name not in disabled
    note = _("admin_settings.module_on" if turned_on else "admin_settings.module_off", module=module.title(_.lang))
    await render(callback, modules_screen(ctx, _, admin_role), answer=note)


@router.callback_query(SetCb.filter(F.a == "groups"))
async def open_groups(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, groups_screen(_))


@router.callback_query(SetCb.filter(F.a == "group"))
async def open_group(
    callback: CallbackQuery, callback_data: SetCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    page = int(callback_data.v) if callback_data.v.isdigit() else 0
    await render(callback, group_screen(ctx, _, callback_data.k, page))


@router.callback_query(SetCb.filter(F.a == "spec"))
async def open_spec(
    callback: CallbackQuery,
    callback_data: SetCb,
    ctx: AppContext,
    _: Translator,
    state: FSMContext,
    admin_role: AdminRole | None = None,
) -> None:
    await state.clear()
    await render(callback, spec_screen(ctx, _, callback_data.k, callback_data.o or "q", admin_role))


@router.callback_query(SetCb.filter(F.a == "set"))
async def set_from_button(
    callback: CallbackQuery,
    callback_data: SetCb,
    ctx: AppContext,
    _: Translator,
    state: FSMContext,
    admin_role: AdminRole | None = None,
) -> None:
    spec = REGISTRY.get(callback_data.k)
    if spec is None or not can_edit(spec, admin_role):
        await answer_callback(callback, _("error.not_allowed"), alert=True)
        return
    try:
        value = _decode_button_value(spec, callback_data.v)
    except (ValueError, IndexError):
        await answer_callback(callback, _("error.expired_button"))
        return
    origin_screen = callback_data.o or "q"
    await state.clear()
    error = await _store(ctx, _, spec.key, value, callback.from_user.id)
    note = error or _("admin_settings.saved")
    await render(callback, spec_screen(ctx, _, spec.key, origin_screen, admin_role, note))


@router.callback_query(SetCb.filter(F.a == "reset"))
async def reset(
    callback: CallbackQuery,
    callback_data: SetCb,
    ctx: AppContext,
    _: Translator,
    admin_role: AdminRole | None = None,
) -> None:
    spec = REGISTRY.get(callback_data.k)
    if spec is None or not can_edit(spec, admin_role):
        await answer_callback(callback, _("error.not_allowed"), alert=True)
        return
    try:
        await ctx.settings_provider.reset(spec.key, callback.from_user.id)
        note = _("admin_settings.reset_done")
    except SettingValidationError as exc:
        note = _("admin_settings.invalid", error=_.esc(str(exc)))
    await render(callback, spec_screen(ctx, _, spec.key, callback_data.o or "q", admin_role, note))


@router.callback_query(SetCb.filter(F.a == "webonly"))
async def web_only(callback: CallbackQuery, _: Translator) -> None:
    await answer_callback(callback, _("admin_settings.web_only"), alert=True)


@router.callback_query(SetCb.filter(F.a == "ask"))
async def ask_value(
    callback: CallbackQuery,
    callback_data: SetCb,
    ctx: AppContext,
    _: Translator,
    state: FSMContext,
    admin_role: AdminRole | None = None,
) -> None:
    spec = REGISTRY.get(callback_data.k)
    if spec is None or not can_edit(spec, admin_role):
        await answer_callback(callback, _("error.not_allowed"), alert=True)
        return
    await state.set_state(SettingStates.value)
    await state.update_data(key=spec.key, origin=callback_data.o or "q")
    hint = _(f"admin_settings.hint.{spec.type.replace('[', '_').replace(']', '')}")
    limits = []
    if spec.min is not None:
        limits.append(_("admin_settings.min", value=f"{spec.min:g}"))
    if spec.max is not None:
        limits.append(_("admin_settings.max", value=f"{spec.max:g}"))
    text = _("admin_settings.ask", label=_.esc(spec.label(_.lang)), hint=hint)
    if limits:
        text += "\n" + ", ".join(limits)
    back_kb = kb([[Button.cb(_("nav.back"), _cb("spec", spec.key, origin=callback_data.o or "q"))]])
    await render(callback, Screen(text, back_kb))


@router.message(SettingStates.value, F.text)
async def got_value(
    message: Message, ctx: AppContext, _: Translator, state: FSMContext, admin_role: AdminRole | None = None
) -> None:
    data = await state.get_data()
    spec = REGISTRY.get(str(data.get("key") or ""))
    origin_screen = str(data.get("origin") or "q")
    if spec is None or not can_edit(spec, admin_role):
        await state.clear()
        await render(message, Screen(_("error.not_allowed")))
        return
    raw = message.text or ""
    if spec.secret:
        await delete_message(message)  # secrets must not stay in the chat history
    error = await _store(ctx, _, spec.key, raw, message.chat.id)
    if error:
        await render(
            message, Screen(error, kb([[Button.cb(_("nav.back"), _cb("spec", spec.key, origin=origin_screen))]]))
        )
        return
    await state.clear()
    await render(message, spec_screen(ctx, _, spec.key, origin_screen, admin_role, _("admin_settings.saved")))
