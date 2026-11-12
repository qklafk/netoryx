"""Screens: one bot message edited in place as the user navigates.

    await render(callback, Screen(_("menu.title", brand=brand), main_menu_kb(_)))

:func:`render` edits the message the button belongs to when possible and falls back to sending a new one (old or
deleted messages, text <-> photo transitions). Callbacks are always answered so the client never spins.
"""

from __future__ import annotations

import html
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
    Message,
)

log = logging.getLogger(__name__)

MAX_CAPTION = 1024  # visible characters of a media caption
_NOT_MODIFIED = "message is not modified"
# Edit failures after which sending a fresh message is the right move.
_EDIT_FALLBACK = (
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
    "there is no text in the message to edit",
    "there is no caption in the message to edit",
    "message can't be found",
)
_TAG = re.compile(r"<[^>]*>")


@dataclass(frozen=True, slots=True)
class Screen:
    """What the user sees: HTML text, an inline keyboard and optionally a photo (bytes, file_id or URL).

    With a photo the text becomes its caption; captions longer than Telegram allows are sent as plain text messages
    instead (the photo is dropped).
    """

    text: str
    kb: InlineKeyboardMarkup | None = None
    photo: bytes | str | None = None
    disable_preview: bool = True
    parse_mode: str | None = "HTML"

    @property
    def has_photo(self) -> bool:
        return self.photo is not None and _visible_length(self.text) <= MAX_CAPTION


async def render(
    event: Message | CallbackQuery,
    screen: Screen,
    *,
    answer: str | None = None,
    alert: bool = False,
    keep_input: bool = False,
) -> Message | None:
    """Show ``screen`` in response to ``event``; returns the message now showing it (``None`` for inline messages).

    For callbacks ``answer`` / ``alert`` set the callback notification (always sent, even if rendering fails).

    In private chats the bot keeps a single screen: when a screen answers a typed message, the previous screen and the
    typed text are removed once the new screen is shown (photos and files the user sent are kept). Pass
    ``keep_input=True`` when the typed message itself is used later (e.g. copied in a broadcast).
    """
    if isinstance(event, Message):
        sent = await _send(event.bot, event.chat.id, screen, thread_id=event.message_thread_id)
        if _is_private(event) and _single_screen():
            previous = _screens.get(event.chat.id)
            if previous is not None and previous != sent.message_id:
                await _delete(event.bot, event.chat.id, previous)
            if not keep_input and event.text is not None and event.from_user is not None and not event.from_user.is_bot:
                await delete_message(event)
        _remember(sent)
        return sent
    try:
        shown = await _render_callback(event, screen)
        if shown is not None:
            _remember(shown)
        return shown
    finally:
        await answer_callback(event, answer, alert=alert)


# ---------------------------------------------------------------------------------------------
# Single screen per private chat
# ---------------------------------------------------------------------------------------------
_MAX_TRACKED_CHATS = 50_000
_screens: OrderedDict[int, int] = OrderedDict()  # private chat id -> message id of the current screen
_single_screen: Callable[[], bool] = lambda: True  # noqa: E731 - replaced at startup with the runtime setting


def configure_single_screen(enabled: Callable[[], bool]) -> None:
    """Make the single-screen behaviour follow a (runtime) setting."""
    global _single_screen
    _single_screen = enabled


def _is_private(message: Message) -> bool:
    return message.chat.type == "private"


def _remember(message: Message) -> None:
    if not _is_private(message):
        return
    _screens[message.chat.id] = message.message_id
    _screens.move_to_end(message.chat.id)
    while len(_screens) > _MAX_TRACKED_CHATS:
        _screens.popitem(last=False)


async def _delete(bot: Bot | None, chat_id: int, message_id: int) -> None:
    if bot is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.debug("delete of the previous screen failed: %s", exc)


async def answer_callback(callback: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
    """Answer a callback query, ignoring "query is too old" and similar errors."""
    try:
        await callback.answer(text=text, show_alert=alert)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.debug("callback answer failed: %s", exc)


async def send_screen(bot: Bot, chat_id: int, screen: Screen, *, thread_id: int | None = None) -> Message:
    """Send ``screen`` as a new message (notifications, jobs)."""
    return await _send(bot, chat_id, screen, thread_id=thread_id)


async def delete_message(message: Message | InaccessibleMessage | None) -> bool:
    """Delete a message, ignoring messages that are too old or already gone; True on success."""
    if message is None or message.bot is None:
        return False
    try:
        return await message.bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.debug("delete failed: %s", exc)
        return False


async def _render_callback(callback: CallbackQuery, screen: Screen) -> Message | None:
    bot = _require_bot(callback.bot)
    message = callback.message
    if message is None:
        if callback.inline_message_id:
            await _edit_inline(bot, callback.inline_message_id, screen)
            return None
        return await _send(bot, callback.from_user.id, screen)
    if isinstance(message, InaccessibleMessage):
        return await _send(bot, message.chat.id, screen)

    current = _kind(message)
    wanted = "media" if screen.has_photo else "text"
    if current == wanted:
        try:
            return await _edit(message, screen)
        except TelegramBadRequest as exc:
            error = str(exc).lower()
            if _NOT_MODIFIED in error:
                return message
            if not any(marker in error for marker in _EDIT_FALLBACK):
                raise
            log.debug("edit failed, sending a new message: %s", exc)
    # Text <-> photo, unsupported or vanished message: replace it with a new one to keep a single screen.
    sent = await _send(bot, message.chat.id, screen, thread_id=message.message_thread_id)
    await delete_message(message)
    return sent


async def _edit(message: Message, screen: Screen) -> Message:
    if screen.has_photo:
        assert screen.photo is not None
        media = InputMediaPhoto(media=_photo(screen.photo), caption=screen.text, parse_mode=screen.parse_mode)
        result = await message.edit_media(media=media, reply_markup=screen.kb)
    else:
        result = await message.edit_text(
            text=screen.text,
            parse_mode=screen.parse_mode,
            reply_markup=screen.kb,
            link_preview_options=LinkPreviewOptions(is_disabled=screen.disable_preview),
        )
    return result if isinstance(result, Message) else message


async def _edit_inline(bot: Bot, inline_message_id: str, screen: Screen) -> None:
    try:
        await bot.edit_message_text(
            text=screen.text,
            inline_message_id=inline_message_id,
            parse_mode=screen.parse_mode,
            reply_markup=screen.kb,
            link_preview_options=LinkPreviewOptions(is_disabled=screen.disable_preview),
        )
    except TelegramBadRequest as exc:
        if _NOT_MODIFIED not in str(exc).lower():
            raise


async def _send(bot: Bot | None, chat_id: int, screen: Screen, *, thread_id: int | None = None) -> Message:
    bot = _require_bot(bot)
    if screen.has_photo:
        assert screen.photo is not None
        return await bot.send_photo(
            chat_id=chat_id,
            photo=_photo(screen.photo),
            caption=screen.text,
            parse_mode=screen.parse_mode,
            reply_markup=screen.kb,
            message_thread_id=thread_id,
        )
    return await bot.send_message(
        chat_id=chat_id,
        text=screen.text,
        parse_mode=screen.parse_mode,
        reply_markup=screen.kb,
        link_preview_options=LinkPreviewOptions(is_disabled=screen.disable_preview),
        message_thread_id=thread_id,
    )


def _kind(message: Message) -> str | None:
    """``"text"``, ``"media"`` (anything ``editMessageMedia`` accepts) or ``None`` (stickers, polls...)."""
    if message.text is not None:
        return "text"
    if message.photo or message.video or message.animation or message.document or message.audio:
        return "media"
    return None


def _photo(photo: bytes | str) -> BufferedInputFile | str:
    return BufferedInputFile(photo, filename="image.jpg") if isinstance(photo, bytes) else photo


def _visible_length(text: str) -> int:
    return len(html.unescape(_TAG.sub("", text)))


def _require_bot(bot: Bot | None) -> Bot:
    if bot is None:
        raise RuntimeError("the update is not bound to a Bot instance")
    return bot
