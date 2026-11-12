"""Support inbox for admins: tickets by status, conversation, reply (text or attachment), close."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.permissions import HasPermission, Permission
from app.core.ui import Screen, answer_callback, render, send_screen
from app.db.models import TicketStatus, User
from app.db.session import read_session
from app.keyboards import Button, back, kb, paginate
from app.modules.admin.common import admin_input_group, admin_router
from app.modules.tickets.callbacks import TicketAdminCb, TicketCb
from app.modules.tickets.common import content_of, conversation, deliver, send_attachment, subject_line
from app.services import tickets
from app.services.tickets import TicketError, TicketErrorCode

log = logging.getLogger(__name__)

router = admin_router("tickets_admin")
router.message.filter(HasPermission(Permission.TICKETS))
router.callback_query.filter(HasPermission(Permission.TICKETS))
# Buttons under support notifications, which may be posted in a group chat.
notice_router = Router(name="tickets_admin_notices")
notice_router.callback_query.filter(HasPermission(Permission.TICKETS))

PAGE_SIZE = 8
FILTERS: dict[str, tuple[TicketStatus, ...]] = {
    "open": (TicketStatus.OPEN,),
    "answered": (TicketStatus.ANSWERED,),
    "closed": (TicketStatus.CLOSED,),
}


@admin_input_group
class TicketAdminStates(StatesGroup):
    reply = State()


def _cb(action: str, ticket_id: int = 0, flt: str = "", page: int = 0) -> TicketAdminCb:
    return TicketAdminCb(a=action, id=ticket_id, v=flt, p=page)


async def inbox_screen(ctx: AppContext, _: Translator, flt: str, page: int) -> Screen:
    flt = flt if flt in FILTERS else "open"
    async with read_session(ctx.session_factory) as session:
        items = await tickets.list_inbox(session, statuses=FILTERS[flt], limit=200)
        waiting = await tickets.inbox_count(session)
    tabs = [
        Button.cb(("• " if name == flt else "") + _(f"tickets.admin.tab.{name}"), _cb("inbox", flt=name))
        for name in FILTERS
    ]
    page_data = paginate(
        items,
        page,
        PAGE_SIZE,
        lambda t: Button.cb(subject_line(t), _cb("view", t.id)),
        lambda p: _cb("inbox", flt=flt, page=p),
    )
    key = "tickets.admin.inbox" if items else "tickets.admin.inbox_empty"
    text = _(key, waiting=_.number(waiting), filter=_(f"tickets.admin.tab.{flt}"))
    rows = [tabs, *page_data.rows, [back("admin", _)]]
    return Screen(text, kb(rows))


async def ticket_screen(ctx: AppContext, _: Translator, ticket_id: int) -> Screen:
    try:
        async with read_session(ctx.session_factory) as session:
            view = await tickets.view(session, ticket_id, limit=30)
            user = await session.get(User, view.ticket.user_id)
    except TicketError:
        return Screen(_("tickets.not_found"), kb([[Button.cb(_("nav.back"), _cb("inbox"))]]))
    ticket = view.ticket
    who = "—"
    if user is not None:
        who = f"@{user.username}" if user.username else _.esc(user.first_name or "—")
    text = _(
        "tickets.admin.view",
        id=ticket.id,
        subject=_.esc(ticket.subject),
        status=_(f"tickets.status.{ticket.status.value}"),
        user=who,
        user_id=ticket.user_id,
        created=_.datetime(ticket.created_at),
        messages=conversation(_, view.messages, admin_view=True),
    )
    files = sum(1 for m in view.messages if m.file_id)
    closed = ticket.status == TicketStatus.CLOSED
    rows: list[list[InlineKeyboardButton | None]] = [
        [Button.cb(_("tickets.btn.reply"), _cb("reply", ticket.id), style="primary")],
        [Button.cb(_("tickets.admin.btn.files", count=files), _cb("files", ticket.id)) if files else None],
        [Button.cb(_("tickets.admin.btn.close"), _cb("close", ticket.id)) if not closed else None],
        [Button.cb(_("tickets.admin.btn.inbox"), _cb("inbox"))],
    ]
    return Screen(text, kb(rows))


def _user_kb(_: Translator, ticket_id: int) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                Button.cb(_("tickets.btn.reply"), TicketCb(a="reply", id=ticket_id)),
                Button.cb(_("tickets.btn.open"), TicketCb(a="view", id=ticket_id)),
            ]
        ]
    )


async def _user_translator(ctx: AppContext, user_id: int) -> Translator:
    async with read_session(ctx.session_factory) as session:
        user = await session.get(User, user_id)
    return ctx.i18n.translator(user.language if user else None, ctx.settings.zone)


# --- inbox ------------------------------------------------------------------------------------------------------------
@router.message(Command("tickets"))
async def tickets_command(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(message, await inbox_screen(ctx, _, "open", 0))


@router.callback_query(TicketAdminCb.filter(F.a == "inbox"))
async def inbox(
    callback: CallbackQuery, callback_data: TicketAdminCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    await render(callback, await inbox_screen(ctx, _, callback_data.v, callback_data.p))


@router.callback_query(TicketAdminCb.filter(F.a == "view"))
async def view_ticket(
    callback: CallbackQuery, callback_data: TicketAdminCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    await render(callback, await ticket_screen(ctx, _, callback_data.id))


@notice_router.callback_query(TicketAdminCb.filter(F.a == "nview"))
async def view_from_notification(
    callback: CallbackQuery, callback_data: TicketAdminCb, ctx: AppContext, _: Translator
) -> None:
    """A button under a support notification (maybe in a group): open the ticket in the admin's private chat."""
    screen = await ticket_screen(ctx, _, callback_data.id)
    try:
        await send_screen(ctx.bot, callback.from_user.id, screen)
    except Exception as exc:  # the admin has not started a private chat with the bot
        log.info("cannot open ticket %s for %s: %s", callback_data.id, callback.from_user.id, exc)
        await answer_callback(callback, _("tickets.admin.start_private"), alert=True)
        return
    await answer_callback(callback, _("tickets.admin.opened_private"))


@router.callback_query(TicketAdminCb.filter(F.a == "files"))
async def send_files(callback: CallbackQuery, callback_data: TicketAdminCb, ctx: AppContext, _: Translator) -> None:
    try:
        async with read_session(ctx.session_factory) as session:
            view = await tickets.view(session, callback_data.id, limit=100)
    except TicketError:
        await answer_callback(callback, _("tickets.not_found"), alert=True)
        return
    await answer_callback(callback)
    for item in view.messages:
        if item.file_id:
            author = _("tickets.author.support" if item.is_admin else "tickets.author.user")
            await send_attachment(ctx.bot, callback.from_user.id, item.file_id, caption=f"#{view.ticket.id} · {author}")


# --- reply / close ----------------------------------------------------------------------------------------------------
@router.callback_query(TicketAdminCb.filter(F.a == "reply"))
async def ask_reply(callback: CallbackQuery, callback_data: TicketAdminCb, _: Translator, state: FSMContext) -> None:
    await state.set_state(TicketAdminStates.reply)
    await state.update_data(ticket_id=callback_data.id)
    keyboard = kb([[Button.cb(_("nav.cancel"), _cb("view", callback_data.id))]])
    await render(callback, Screen(_("tickets.admin.ask_reply", id=callback_data.id), keyboard))


@router.message(TicketAdminStates.reply, F.text | F.photo | F.document)
async def got_reply(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    admin = message.from_user
    if admin is None:
        return
    if message.text is not None and message.text.startswith("/"):
        await state.clear()
        await render(
            message, Screen(_("common.canceled"), kb([[Button.cb(_("tickets.admin.btn.inbox"), _cb("inbox"))]]))
        )
        return
    ticket_id = int((await state.get_data()).get("ticket_id") or 0)
    content = content_of(message)
    try:
        async with ctx.session_factory.begin() as session:
            await tickets.reply(session, ticket_id, admin.id, content.text, is_admin=True, file_id=content.file_id)
            ticket = await tickets.get(session, ticket_id)
            user_id = ticket.user_id
    except TicketError as exc:
        await render(message, Screen(_(f"tickets.error.{exc.code.value}")))
        if exc.code != TicketErrorCode.EMPTY:
            await state.clear()
        return
    await state.clear()
    user_ = await _user_translator(ctx, user_id)
    body = user_.esc(content.text) if content.text else ""
    text = user_("tickets.answer", id=ticket_id, text=body)
    delivered = await deliver(ctx.bot, user_id, text, content.file_id, _user_kb(user_, ticket_id))
    note = _("tickets.admin.delivered" if delivered else "tickets.admin.not_delivered")
    screen = await ticket_screen(ctx, _, ticket_id)
    await render(message, Screen(note + "\n\n" + screen.text, screen.kb))


@router.message(TicketAdminStates.reply)
async def unsupported_reply(message: Message, _: Translator) -> None:
    await message.answer(_("tickets.error.unsupported"))


@router.callback_query(TicketAdminCb.filter(F.a == "close"))
async def close_ticket(callback: CallbackQuery, callback_data: TicketAdminCb, ctx: AppContext, _: Translator) -> None:
    try:
        async with ctx.session_factory.begin() as session:
            ticket = await tickets.close(session, callback_data.id, callback.from_user.id, is_admin=True)
            user_id = ticket.user_id
    except TicketError:
        await answer_callback(callback, _("tickets.not_found"), alert=True)
        return
    user_ = await _user_translator(ctx, user_id)
    keyboard = kb([[Button.cb(user_("tickets.btn.open"), TicketCb(a="view", id=callback_data.id))]])
    await deliver(ctx.bot, user_id, user_("tickets.closed_by_support", id=callback_data.id), None, keyboard)
    await render(callback, await ticket_screen(ctx, _, callback_data.id), answer=_("tickets.closed"))
