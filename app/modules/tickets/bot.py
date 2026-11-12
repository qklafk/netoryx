"""Support for users: open a ticket, read the conversation, reply, close."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import Settings
from app.core.context import AppContext
from app.core.filters import IsPrivate
from app.core.i18n import Translator
from app.core.ui import Screen, answer_callback, render
from app.db.models import TicketStatus
from app.db.session import read_session
from app.keyboards import Button, Nav, kb, to_menu
from app.modules.tickets.callbacks import TicketAdminCb, TicketCb
from app.modules.tickets.common import content_of, conversation, subject_line
from app.services import notifications, tickets
from app.services.notifications import AdminEvent
from app.services.tickets import TicketError, TicketErrorCode

log = logging.getLogger(__name__)

router = Router(name="tickets_user")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(IsPrivate())

NOTIFY_PREVIEW = 1500
SUPPORT_NAV = "tickets"  # Nav target of the main menu "Support" button


class TicketStates(StatesGroup):
    new = State()
    reply = State()


def _cb(action: str, ticket_id: int = 0) -> TicketCb:
    return TicketCb(a=action, id=ticket_id)


def _back_to_list(_: Translator) -> InlineKeyboardButton:
    return Button.cb(_("tickets.btn.my"), _cb("list"))


async def support_screen(ctx: AppContext, _: Translator, settings: Settings, user_id: int) -> Screen:
    async with read_session(ctx.session_factory) as session:
        unread = await tickets.unread_for_user(session, user_id)
        has_tickets = bool(await tickets.list_for_user(session, user_id, limit=1))
    my_label = _("tickets.btn.my_unread", count=unread) if unread else _("tickets.btn.my")
    rows: list[list[InlineKeyboardButton | None]] = [
        [Button.cb(_("tickets.btn.new"), _cb("new"), style="primary")],
        [Button.cb(my_label, _cb("list")) if has_tickets else None],
        [Button.url(_("tickets.btn.chat"), settings.support_url) if settings.support_url else None],
        [to_menu(_)],
    ]
    return Screen(_("tickets.home", brand=_.esc(settings.brand_name)), kb(rows))


async def list_screen(ctx: AppContext, _: Translator, user_id: int) -> Screen:
    async with read_session(ctx.session_factory) as session:
        items = await tickets.list_for_user(session, user_id, limit=20)
    if not items:
        return Screen(_("tickets.list_empty"), kb([[Button.cb(_("nav.back"), _cb("home"))]]))
    rows = [[Button.cb(subject_line(ticket), _cb("view", ticket.id))] for ticket in items]
    rows.append([Button.cb(_("nav.back"), _cb("home"))])
    return Screen(_("tickets.list"), kb(rows))


async def ticket_screen(ctx: AppContext, _: Translator, user_id: int, ticket_id: int) -> Screen:
    try:
        async with ctx.session_factory.begin() as session:
            view = await tickets.view(session, ticket_id, user_id=user_id, limit=30)
            await tickets.mark_seen(session, user_id)
    except TicketError:
        return Screen(_("tickets.not_found"), kb([[_back_to_list(_)]]))
    ticket = view.ticket
    text = _(
        "tickets.view",
        id=ticket.id,
        subject=_.esc(ticket.subject),
        status=_(f"tickets.status.{ticket.status.value}"),
        messages=conversation(_, view.messages, admin_view=False),
    )
    closed = ticket.status == TicketStatus.CLOSED
    rows: list[list[InlineKeyboardButton | None]] = [
        [Button.cb(_("tickets.btn.write_again" if closed else "tickets.btn.reply"), _cb("reply", ticket.id))],
        [Button.cb(_("tickets.btn.close"), _cb("close", ticket.id)) if not closed else None],
        [_back_to_list(_)],
    ]
    return Screen(text, kb(rows))


def _admin_kb(_: Translator, ticket_id: int) -> InlineKeyboardMarkup:
    return kb([[Button.cb(_("tickets.btn.open_admin"), TicketAdminCb(a="nview", id=ticket_id))]])


def notify_support(ctx: AppContext, key: str, ticket_id: int, user: object, text: str | None, has_file: bool) -> None:
    """Queue a notice for the support chat (new ticket / new message from the user)."""
    lang = ctx.settings.default_language
    _ = ctx.i18n.translator(lang, ctx.settings.zone)
    username = getattr(user, "username", None)
    name = getattr(user, "full_name", None) or getattr(user, "first_name", None) or ""
    who = f"@{username}" if username else _.esc(name)
    body = _.esc(text[:NOTIFY_PREVIEW]) if text else ""
    if has_file:
        body = (body + "\n" if body else "") + _("tickets.attachment")
    message = _(key, id=ticket_id, user=who, user_id=getattr(user, "id", 0), text=body)
    notifications.notify(ctx, AdminEvent.TICKETS, message, reply_markup=_admin_kb(_, ticket_id))


# --- entry points -----------------------------------------------------------------------------------------------------
@router.message(Command("support"))
async def support_command(
    message: Message, ctx: AppContext, _: Translator, settings: Settings, state: FSMContext
) -> None:
    await state.clear()
    user_id = message.from_user.id if message.from_user else message.chat.id
    await render(message, await support_screen(ctx, _, settings, user_id))


@router.callback_query(Nav.filter(F.to.in_({SUPPORT_NAV, "support"})))
@router.callback_query(TicketCb.filter(F.a == "home"))
async def support_home(
    callback: CallbackQuery, ctx: AppContext, _: Translator, settings: Settings, state: FSMContext
) -> None:
    await state.clear()
    await render(callback, await support_screen(ctx, _, settings, callback.from_user.id))


@router.callback_query(TicketCb.filter(F.a == "list"))
async def my_tickets(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, await list_screen(ctx, _, callback.from_user.id))


@router.callback_query(TicketCb.filter(F.a == "view"))
async def open_ticket(
    callback: CallbackQuery, callback_data: TicketCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    await render(callback, await ticket_screen(ctx, _, callback.from_user.id, callback_data.id))


# --- new ticket -------------------------------------------------------------------------------------------------------
@router.callback_query(TicketCb.filter(F.a == "new"))
async def new_ticket(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    async with read_session(ctx.session_factory) as session:
        open_count = await tickets.count_open(session, callback.from_user.id)
    if open_count >= tickets.MAX_OPEN_TICKETS_PER_USER:
        await answer_callback(callback, _("tickets.error.too_many_open"), alert=True)
        return
    await state.set_state(TicketStates.new)
    await render(callback, Screen(_("tickets.ask_new"), kb([[Button.cb(_("nav.cancel"), _cb("home"))]])))


@router.message(TicketStates.new, F.text | F.photo | F.document)
async def got_new_ticket(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    if message.text is not None and message.text.startswith("/"):
        await state.clear()
        await render(message, Screen(_("common.canceled"), kb([[to_menu(_)]])))
        return
    user = message.from_user
    if user is None:
        return
    content = content_of(message)
    try:
        async with ctx.session_factory.begin() as session:
            ticket = await tickets.create(session, user.id, content.text, file_id=content.file_id)
            ticket_id = ticket.id
    except TicketError as exc:
        await render(
            message, Screen(_(f"tickets.error.{exc.code.value}"), kb([[Button.cb(_("nav.back"), _cb("home"))]]))
        )
        if exc.code != TicketErrorCode.EMPTY:
            await state.clear()
        return
    await state.clear()
    notify_support(ctx, "tickets.notify.new", ticket_id, user, content.text, content.file_id is not None)
    rows = [[Button.cb(_("tickets.btn.open"), _cb("view", ticket_id))], [to_menu(_)]]
    await render(message, Screen(_("tickets.created", id=ticket_id), kb(rows)))


# --- reply / close ----------------------------------------------------------------------------------------------------
@router.callback_query(TicketCb.filter(F.a == "reply"))
async def ask_reply(
    callback: CallbackQuery, callback_data: TicketCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    try:
        async with read_session(ctx.session_factory) as session:
            await tickets.get(session, callback_data.id, user_id=callback.from_user.id)
    except TicketError:
        await answer_callback(callback, _("tickets.not_found"), alert=True)
        return
    await state.set_state(TicketStates.reply)
    await state.update_data(ticket_id=callback_data.id)
    keyboard = kb([[Button.cb(_("nav.cancel"), _cb("view", callback_data.id))]])
    await render(callback, Screen(_("tickets.ask_reply", id=callback_data.id), keyboard))


@router.message(TicketStates.reply, F.text | F.photo | F.document)
async def got_reply(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    if message.text is not None and message.text.startswith("/"):
        await state.clear()
        await render(message, Screen(_("common.canceled"), kb([[to_menu(_)]])))
        return
    ticket_id = int((await state.get_data()).get("ticket_id") or 0)
    content = content_of(message)
    try:
        async with ctx.session_factory.begin() as session:
            await tickets.reply(session, ticket_id, user.id, content.text, is_admin=False, file_id=content.file_id)
    except TicketError as exc:
        await render(message, Screen(_(f"tickets.error.{exc.code.value}"), kb([[_back_to_list(_)]])))
        if exc.code != TicketErrorCode.EMPTY:
            await state.clear()
        return
    await state.clear()
    notify_support(ctx, "tickets.notify.reply", ticket_id, user, content.text, content.file_id is not None)
    screen = await ticket_screen(ctx, _, user.id, ticket_id)
    await render(message, Screen(_("tickets.sent") + "\n\n" + screen.text, screen.kb))


@router.callback_query(TicketCb.filter(F.a == "close"))
async def close_ticket(callback: CallbackQuery, callback_data: TicketCb, ctx: AppContext, _: Translator) -> None:
    try:
        async with ctx.session_factory.begin() as session:
            await tickets.close(session, callback_data.id, callback.from_user.id, is_admin=False)
    except TicketError:
        await answer_callback(callback, _("tickets.not_found"), alert=True)
        return
    screen = await ticket_screen(ctx, _, callback.from_user.id, callback_data.id)
    await render(callback, screen, answer=_("tickets.closed"))


# Registered last: anything else sent while a ticket message is expected.
@router.message(TicketStates.new)
@router.message(TicketStates.reply)
async def unsupported_content(message: Message, _: Translator) -> None:
    await message.answer(_("tickets.error.unsupported"))
