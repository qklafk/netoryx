"""Shared helpers of the support screens: message content, conversation rendering, attachments."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message

from app.core.i18n import Translator
from app.db.models import Ticket, TicketMessage, TicketStatus

log = logging.getLogger(__name__)

MAX_SCREEN_TEXT = 3800  # leaves room for the header within Telegram's 4096 characters
MAX_MESSAGE_PREVIEW = 1200
STATUS_ICONS = {TicketStatus.OPEN: "🟡", TicketStatus.ANSWERED: "🟢", TicketStatus.CLOSED: "⚪️"}


@dataclass(frozen=True, slots=True)
class Content:
    text: str | None
    file_id: str | None


def content_of(message: Message) -> Content:
    """Text (or caption) and the attachment of a user's / admin's message (photo or document)."""
    file_id: str | None = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document is not None:
        file_id = message.document.file_id
    text = message.text if message.text is not None else message.caption
    return Content(text, file_id)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def subject_line(ticket: Ticket) -> str:
    return f"{STATUS_ICONS.get(ticket.status, '•')} #{ticket.id} · {_clip(ticket.subject, 40)}"


def conversation(_: Translator, messages: list[TicketMessage], *, admin_view: bool) -> str:
    """The latest messages that fit on one screen, oldest first."""
    blocks: list[str] = []
    used = 0
    for item in reversed(messages):
        author_key = (
            "tickets.author.support"
            if item.is_admin
            else ("tickets.author.user" if admin_view else "tickets.author.you")
        )
        body = _.esc(_clip(item.text, MAX_MESSAGE_PREVIEW)) if item.text else ""
        if item.file_id:
            body = (body + "\n" if body else "") + _("tickets.attachment")
        block = _("tickets.message", author=_(author_key), time=_.datetime(item.created_at), text=body)
        if used + len(block) > MAX_SCREEN_TEXT and blocks:
            blocks.append(_("tickets.earlier"))
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(reversed(blocks))


async def send_attachment(
    bot: Bot, chat_id: int, file_id: str, caption: str | None = None, reply_markup: InlineKeyboardMarkup | None = None
) -> bool:
    """Send a stored photo or document (the kind is not stored: try a photo first). True on success."""
    try:
        try:
            await bot.send_photo(chat_id, file_id, caption=caption, reply_markup=reply_markup)
        except TelegramBadRequest:
            await bot.send_document(chat_id, file_id, caption=caption, reply_markup=reply_markup)
        return True
    except (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter) as exc:
        log.info("could not send a ticket attachment to %s: %s", chat_id, exc)
        return False


async def deliver(
    bot: Bot, chat_id: int, text: str, file_id: str | None, reply_markup: InlineKeyboardMarkup | None
) -> bool:
    """Send a support message (with its attachment, if any). False if the user cannot be reached."""
    try:
        await bot.send_message(chat_id, text, reply_markup=None if file_id else reply_markup)
    except (TelegramForbiddenError, TelegramNotFound):
        return False
    except (TelegramBadRequest, TelegramRetryAfter) as exc:
        log.info("could not deliver a support message to %s: %s", chat_id, exc)
        return False
    if file_id:
        await send_attachment(bot, chat_id, file_id, reply_markup=reply_markup)
    return True
