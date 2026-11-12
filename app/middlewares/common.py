"""Helpers shared by the update middlewares: event classification, replies and small TTL caches."""

from __future__ import annotations

import contextlib
import html
import logging
import re
import time
from collections.abc import Hashable
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineQuery, Message, PreCheckoutQuery, Update, User

from app.services.users import detect_language

if TYPE_CHECKING:
    from aiogram import Bot

    from app.core.context import AppContext
    from app.core.i18n import Translator

log = logging.getLogger(__name__)

_MISSING: Any = object()
_TAG = re.compile(r"<[^>]+>")
# Commands that stay reachable for everyone (legal pages and payment support are required by Telegram).
OPEN_COMMANDS = frozenset({"terms", "privacy", "paysupport"})


class TTLCache[K: Hashable, V]:
    """A small in-memory cache with per-entry expiry and size-bounded pruning (monotonic clock)."""

    def __init__(self, max_size: int = 10_000) -> None:
        self._data: dict[K, tuple[float, V]] = {}
        self._max_size = max_size

    def get(self, key: K, default: V | None = None) -> V | None:
        item = self._data.get(key)
        if item is None:
            return default
        expires, value = item
        if expires <= time.monotonic():
            self._data.pop(key, None)
            return default
        return value

    def __contains__(self, key: object) -> bool:
        return self.get(key, _MISSING) is not _MISSING  # type: ignore[arg-type]

    def set(self, key: K, value: V, ttl: float) -> None:
        if len(self._data) >= self._max_size and key not in self._data:
            self.prune()
        self._data[key] = (time.monotonic() + ttl, value)

    def pop(self, key: K) -> V | None:
        item = self._data.pop(key, None)
        return None if item is None or item[0] <= time.monotonic() else item[1]

    def prune(self) -> None:
        """Drop expired entries; if still full, drop the entries closest to expiry (oldest first)."""
        now = time.monotonic()
        self._data = {k: v for k, v in self._data.items() if v[0] > now}
        overflow = len(self._data) - self._max_size + 1
        if overflow > 0:
            for key, _ in sorted(self._data.items(), key=lambda kv: kv[1][0])[:overflow]:
                del self._data[key]

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


# --- update classification --------------------------------------------------------------------------------------------
def is_payment_update(update: Update) -> bool:
    """Payment confirmations must never be blocked: pre-checkout queries and successful/refunded payments."""
    if update.pre_checkout_query is not None:
        return True
    message = update.message
    return message is not None and (message.successful_payment is not None or message.refunded_payment is not None)


def from_user(data: dict[str, Any]) -> User | None:
    user = data.get("event_from_user")
    return user if isinstance(user, User) else None


def is_private(update: Update, data: dict[str, Any]) -> bool:
    """True for updates that come from a user's private chat with the bot (or chat-less user events)."""
    chat = data.get("event_chat")
    if chat is not None:
        return getattr(chat, "type", None) == "private"
    return update.callback_query is not None or update.inline_query is not None or update.pre_checkout_query is not None


def command_of(update: Update) -> tuple[str, str] | None:
    """``(command, args)`` of a message like ``/start ref_X`` (lower-cased command without the bot name)."""
    message = update.message
    text = message.text if message is not None else None
    if not text or not text.startswith("/"):
        return None
    head, _, args = text.partition(" ")
    command = head[1:].split("@", 1)[0].lower()
    return command, args.strip()


def is_open_command(update: Update) -> bool:
    cmd = command_of(update)
    return cmd is not None and cmd[0] in OPEN_COMMANDS


def is_start(update: Update) -> bool:
    cmd = command_of(update)
    return cmd is not None and cmd[0] == "start"


# --- context ----------------------------------------------------------------------------------------------------------
def translator_of(data: dict[str, Any], ctx: AppContext) -> Translator:
    """The ``_`` injected by the user middleware, or one for the default language."""
    translator = data.get("_")
    if translator is not None:
        return translator
    lang = data.get("lang")
    if lang is None:
        user = from_user(data)
        lang = detect_language(user.language_code if user is not None else None)
    return ctx.i18n.translator(lang, ctx.settings.zone)


def is_admin(data: dict[str, Any]) -> bool:
    return data.get("admin_role") is not None


# --- replies ----------------------------------------------------------------------------------------------------------
async def respond(
    update: Update,
    bot: Bot,
    text: str,
    *,
    popup: str | None = None,
    alert: bool = False,
    reply_markup: InlineKeyboardMarkup | None = None,
    as_message: bool = False,
) -> None:
    """Tell the user ``text`` in the way that fits the update; never raises.

    Messages get a reply; callback queries are answered (a popup with ``alert``) and, with ``as_message`` or a
    keyboard, also get a message in the chat; inline queries get an empty result; pre-checkout queries are declined
    with ``text`` as the reason. ``popup`` is a shorter text for popups (defaults to ``text`` without HTML).
    """
    event = update.event
    try:
        if isinstance(event, Message):
            await bot.send_message(event.chat.id, text, reply_markup=reply_markup)
        elif isinstance(event, CallbackQuery):
            await bot.answer_callback_query(event.id, text=_popup(popup or text), show_alert=alert)
            if (as_message or reply_markup is not None) and event.message is not None:
                await bot.send_message(event.message.chat.id, text, reply_markup=reply_markup)
        elif isinstance(event, InlineQuery):
            await bot.answer_inline_query(event.id, results=[], cache_time=5, is_personal=True)
        elif isinstance(event, PreCheckoutQuery):
            await bot.answer_pre_checkout_query(event.id, ok=False, error_message=_popup(popup or text))
    except TelegramAPIError as exc:
        log.debug("could not respond to update %s: %s", update.update_id, exc)
    except Exception:
        log.exception("could not respond to update %s", update.update_id)


async def answer_silently(update: Update, bot: Bot) -> None:
    """Stop the button spinner of a callback query without showing anything."""
    if update.callback_query is None:
        return
    with contextlib.suppress(TelegramAPIError):
        await bot.answer_callback_query(update.callback_query.id)


def _popup(text: str) -> str:
    """Plain text for popups and pre-checkout errors (no HTML, at most 200 characters)."""
    plain = strip_html(text)
    return plain if len(plain) <= 200 else plain[:199] + "…"


def strip_html(text: str) -> str:
    return html.unescape(_TAG.sub("", text)).strip()
