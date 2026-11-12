"""Administrators and roles (owner only): add by id / @username / forwarded message, change role, note, revoke web
sessions, remove. Owners from ADMIN_IDS are read-only here."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram import F
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    Message,
    MessageOriginUser,
)

from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.permissions import HasPermission, Permission
from app.core.ui import Screen, answer_callback, render
from app.db.models import AdminRole, User
from app.db.session import read_session
from app.keyboards import Button, back, kb
from app.modules.admin.common import admin_input_group, admin_router
from app.services import admins, audit, users
from app.services.admins import ROLE_ORDER, AdminEntry, AdminError

log = logging.getLogger(__name__)

router = admin_router("admins_admin")
router.message.filter(HasPermission(Permission.ADMINS))
router.callback_query.filter(HasPermission(Permission.ADMINS))

ROLE_ICONS = {AdminRole.OWNER: "👑", AdminRole.ADMIN: "🛠", AdminRole.SUPPORT: "🛟", AdminRole.VIEWER: "👀"}


class AdmCb(CallbackData, prefix="aadm"):
    a: str
    id: int = 0
    v: str = ""


@admin_input_group
class AdminStates(StatesGroup):
    who = State()
    note = State()


def _cb(action: str, id_: int = 0, value: str = "") -> AdmCb:
    return AdmCb(a=action, id=id_, v=value)


async def _users(ctx: AppContext, ids: list[int]) -> dict[int, User]:
    if not ids:
        return {}
    async with read_session(ctx.session_factory) as session:
        rows = [await session.get(User, tg_id) for tg_id in ids]
    return {user.tg_id: user for user in rows if user is not None}


def _who(_: Translator, tg_id: int, user: User | None) -> str:
    if user is None:
        return f"<code>{tg_id}</code>"
    name = f"@{user.username}" if user.username else (user.first_name or str(tg_id))
    return f"{_.esc(name)} · <code>{tg_id}</code>"


async def _entries(ctx: AppContext) -> list[AdminEntry]:
    async with read_session(ctx.session_factory) as session:
        return await admins.list_admins(session, ctx.settings)


async def home_screen(ctx: AppContext, _: Translator, note: str | None = None) -> Screen:
    entries = await _entries(ctx)
    people = await _users(ctx, [e.tg_id for e in entries])
    rows: list[Sequence[InlineKeyboardButton | None]] = []
    for entry in entries:
        user = people.get(entry.tg_id)
        name = (f"@{user.username}" if user and user.username else (user.first_name if user else None)) or str(
            entry.tg_id
        )
        lock = " 🔒" if entry.from_env else ""
        label = f"{ROLE_ICONS[entry.role]} {name}{lock}"[:60]
        rows.append([Button.cb(label, _cb("card", entry.tg_id))])
    rows.append([Button.cb(_("admin_admins.btn.add"), _cb("add"), style="primary")])
    rows.append([back("admin", _)])
    text = _("admin_admins.home", count=len(entries))
    if note:
        text = note + "\n\n" + text
    return Screen(text, kb(rows))


async def card_screen(ctx: AppContext, _: Translator, tg_id: int, note: str | None = None) -> Screen:
    entry = next((e for e in await _entries(ctx) if e.tg_id == tg_id), None)
    if entry is None:
        return Screen(_("admin_admins.not_found"), kb([[Button.cb(_("nav.back"), _cb("home"))]]))
    people = await _users(ctx, [tg_id] + ([entry.added_by] if entry.added_by else []))
    text = _(
        "admin_admins.card",
        who=_who(_, tg_id, people.get(tg_id)),
        role=f"{ROLE_ICONS[entry.role]} {_(f'admin_admins.role.{entry.role.value}')}",
        about=_(f"admin_admins.about.{entry.role.value}"),
        note=_.esc(entry.note or "—"),
        added_by=_who(_, entry.added_by, people.get(entry.added_by)) if entry.added_by else "—",
        added=_.datetime(entry.created_at) if entry.created_at else "—",
    )
    if entry.from_env:
        text += "\n\n" + _("admin_admins.env_owner")
    if note:
        text += "\n\n" + note
    rows: list[Sequence[InlineKeyboardButton | None]] = []
    if not entry.from_env:
        rows.append(
            [
                Button.cb(("• " if role == entry.role else "") + ROLE_ICONS[role], _cb("role", tg_id, role.value))
                for role in ROLE_ORDER
            ]
        )
        rows.append([Button.cb(_("admin_admins.btn.note"), _cb("note", tg_id))])
        rows.append([Button.cb(_("admin_admins.btn.sessions"), _cb("sess", tg_id))])
        rows.append([Button.cb(_("admin_admins.btn.remove"), _cb("rm", tg_id), style="danger")])
    rows.append([Button.cb(_("nav.back"), _cb("home"))])
    return Screen(text, kb(rows))


def _error(_: Translator, exc: AdminError) -> str:
    return _(f"admin_admins.error.{exc.code.value}")


async def _sync_commands(ctx: AppContext, tg_id: int, *, admin: bool) -> None:
    """Show (or hide) the admin commands in this person's command menu. Best effort."""
    try:
        if admin:
            commands = ctx.modules.commands(ctx.settings, admin=True)
            lang = ctx.settings.default_language
            await ctx.bot.set_my_commands(
                [BotCommand(command=c.command, description=c.description(lang)) for c in commands],
                scope=BotCommandScopeChat(chat_id=tg_id),
            )
        else:
            await ctx.bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=tg_id))
    except Exception as exc:
        log.info("could not update the command menu of %s: %s", tg_id, exc)


# --- list / card ------------------------------------------------------------------------------------------------------
@router.callback_query(AdmCb.filter(F.a == "home"))
async def open_home(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, await home_screen(ctx, _))


@router.callback_query(AdmCb.filter(F.a == "card"))
async def open_card(
    callback: CallbackQuery, callback_data: AdmCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    await render(callback, await card_screen(ctx, _, callback_data.id))


# --- add --------------------------------------------------------------------------------------------------------------
@router.callback_query(AdmCb.filter(F.a == "add"))
async def ask_who(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    await state.set_state(AdminStates.who)
    await render(callback, Screen(_("admin_admins.ask_who"), kb([[Button.cb(_("nav.cancel"), _cb("home"))]])))


@router.message(AdminStates.who)
async def got_who(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    tg_id: int | None = None
    origin = message.forward_origin
    if isinstance(origin, MessageOriginUser):
        tg_id = origin.sender_user.id
    elif message.text:
        key = users.parse_query(message.text)
        if isinstance(key, int):
            tg_id = key
        elif key is not None:
            async with read_session(ctx.session_factory) as session:
                found = await users.find(session, message.text)
            tg_id = found.tg_id if found else None
    if tg_id is None or tg_id <= 0:
        await render(message, Screen(_("admin_admins.who_unknown")))
        return
    await state.clear()
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(f"{ROLE_ICONS[role]} {_(f'admin_admins.role.{role.value}')}", _cb("addrole", tg_id, role.value))]
        for role in ROLE_ORDER
    ]
    rows.append([Button.cb(_("nav.cancel"), _cb("home"))])
    people = await _users(ctx, [tg_id])
    text = _("admin_admins.pick_role", who=_who(_, tg_id, people.get(tg_id)), roles=_roles_help(_))
    await render(message, Screen(text, kb(rows)))


def _roles_help(_: Translator) -> str:
    return "\n".join(
        f"{ROLE_ICONS[role]} <b>{_(f'admin_admins.role.{role.value}')}</b> — {_(f'admin_admins.about.{role.value}')}"
        for role in ROLE_ORDER
    )


@router.callback_query(AdmCb.filter(F.a == "addrole"))
async def add_admin(callback: CallbackQuery, callback_data: AdmCb, ctx: AppContext, _: Translator) -> None:
    try:
        async with ctx.session_factory.begin() as session:
            await admins.add(session, ctx.settings, callback.from_user.id, callback_data.id, callback_data.v)
    except AdminError as exc:
        await answer_callback(callback, _error(_, exc), alert=True)
        return
    await _sync_commands(ctx, callback_data.id, admin=True)
    await render(callback, await card_screen(ctx, _, callback_data.id, _("admin_admins.added")))


# --- change -----------------------------------------------------------------------------------------------------------
@router.callback_query(AdmCb.filter(F.a == "role"))
async def change_role(callback: CallbackQuery, callback_data: AdmCb, ctx: AppContext, _: Translator) -> None:
    try:
        async with ctx.session_factory.begin() as session:
            await admins.update(session, ctx.settings, callback.from_user.id, callback_data.id, role=callback_data.v)
    except AdminError as exc:
        await answer_callback(callback, _error(_, exc), alert=True)
        return
    await render(callback, await card_screen(ctx, _, callback_data.id, _("admin_admins.saved")))


@router.callback_query(AdmCb.filter(F.a == "note"))
async def ask_note(callback: CallbackQuery, callback_data: AdmCb, _: Translator, state: FSMContext) -> None:
    await state.set_state(AdminStates.note)
    await state.update_data(tg_id=callback_data.id)
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_admins.btn.clear_note"), _cb("noteclr", callback_data.id))],
        [Button.cb(_("nav.back"), _cb("card", callback_data.id))],
    ]
    await render(callback, Screen(_("admin_admins.ask_note", limit=admins.MAX_NOTE_LENGTH), kb(rows)))


@router.message(AdminStates.note, F.text)
async def got_note(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    tg_id = int((await state.get_data()).get("tg_id") or 0)
    await state.clear()
    await _set_note(message, ctx, _, tg_id, message.chat.id, message.text)


@router.callback_query(AdmCb.filter(F.a == "noteclr"))
async def clear_note(
    callback: CallbackQuery, callback_data: AdmCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    await _set_note(callback, ctx, _, callback_data.id, callback.from_user.id, None)


async def _set_note(
    event: Message | CallbackQuery, ctx: AppContext, _: Translator, tg_id: int, actor_id: int, note: str | None
) -> None:
    try:
        async with ctx.session_factory.begin() as session:
            await admins.update(session, ctx.settings, actor_id, tg_id, note=note)
    except AdminError as exc:
        await render(event, await card_screen(ctx, _, tg_id, _error(_, exc)))
        return
    await render(event, await card_screen(ctx, _, tg_id, _("admin_admins.saved")))


@router.callback_query(AdmCb.filter(F.a == "sess"))
async def revoke_sessions(callback: CallbackQuery, callback_data: AdmCb, ctx: AppContext, _: Translator) -> None:
    async with ctx.session_factory.begin() as session:
        count = await admins.revoke_sessions(session, callback_data.id)
        await audit.log(session, callback.from_user.id, "admin.sessions_revoke", "admin", callback_data.id, count=count)
    await render(callback, await card_screen(ctx, _, callback_data.id, _("admin_admins.sessions_revoked", count=count)))


# --- remove -----------------------------------------------------------------------------------------------------------
@router.callback_query(AdmCb.filter(F.a == "rm"))
async def ask_remove(callback: CallbackQuery, callback_data: AdmCb, _: Translator) -> None:
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_admins.btn.remove_yes"), _cb("rmy", callback_data.id), style="danger")],
        [Button.cb(_("nav.back"), _cb("card", callback_data.id))],
    ]
    await render(callback, Screen(_("admin_admins.confirm_remove"), kb(rows)))


@router.callback_query(AdmCb.filter(F.a == "rmy"))
async def remove(callback: CallbackQuery, callback_data: AdmCb, ctx: AppContext, _: Translator) -> None:
    try:
        async with ctx.session_factory.begin() as session:
            removed = await admins.remove(session, ctx.settings, callback.from_user.id, callback_data.id)
    except AdminError as exc:
        await answer_callback(callback, _error(_, exc), alert=True)
        return
    if removed:
        await _sync_commands(ctx, callback_data.id, admin=False)
    await render(
        callback, await home_screen(ctx, _, _("admin_admins.removed" if removed else "admin_admins.not_found"))
    )
