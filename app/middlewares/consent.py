"""One-time acceptance of the terms of service / privacy policy before using the bot.

Active when ``LEGAL_CONSENT_REQUIRED`` is on and ``TERMS_URL`` or ``PRIVACY_URL`` is set. The acceptance is stored in
``kv`` (``consent:<tg_id>`` = accepted ``LEGAL_CONSENT_VERSION``; raising the version asks everybody again) and
tracked as a ``legal.accepted`` event. The intercepted ``/start`` (with its deep link) is replayed after accepting.
Administrators, payment confirmations, group chats and ``/terms``, ``/privacy``, ``/paysupport`` are not affected.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, TelegramObject, Update

from app.db.session import read_session
from app.middlewares.common import (
    TTLCache,
    from_user,
    is_admin,
    is_open_command,
    is_payment_update,
    is_private,
    respond,
    translator_of,
)
from app.middlewares.replay import StartReplayer
from app.services import events, kv
from app.utils.time import utcnow

if TYPE_CHECKING:
    from aiogram import Bot

    from app.config import Settings
    from app.core.context import AppContext
    from app.core.i18n import Translator

log = logging.getLogger(__name__)

ACCEPT_CALLBACK = "legal:accept"
CACHE_TTL = 3600.0
KEY_PREFIX = "consent:"


def consent_key(tg_id: int) -> str:
    return f"{KEY_PREFIX}{tg_id}"


def consent_required(settings: Settings) -> bool:
    return bool(settings.legal_consent_required and (settings.terms_url or settings.privacy_url))


def consent_version(settings: Settings) -> str:
    return str(settings.legal_consent_version or "1")


def consent_keyboard(_: Translator, settings: Settings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if settings.terms_url:
        rows.append([InlineKeyboardButton(text=_("consent.terms"), url=settings.terms_url)])
    if settings.privacy_url:
        rows.append([InlineKeyboardButton(text=_("consent.privacy"), url=settings.privacy_url)])
    rows.append([InlineKeyboardButton(text=_("consent.accept"), callback_data=ACCEPT_CALLBACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class LegalConsentMiddleware(BaseMiddleware):
    """Outer ``update`` middleware; runs after the maintenance middleware, before the channel gate."""

    def __init__(self, ctx: AppContext, replayer: StartReplayer | None = None) -> None:
        self._ctx = ctx
        self._replayer = replayer or StartReplayer()
        self._accepted: TTLCache[tuple[int, str], bool] = TTLCache(100_000)

    async def has_accepted(self, tg_id: int, version: str) -> bool:
        if self._accepted.get((tg_id, version)):
            return True
        async with read_session(self._ctx.session_factory) as session, session.begin():
            stored = await kv.get(session, consent_key(tg_id))
        accepted = stored is not None and stored.split("|", 1)[0] == version
        if accepted:
            self._accepted.set((tg_id, version), True, CACHE_TTL)
        return accepted

    async def accept(self, tg_id: int, version: str) -> None:
        """Record the acceptance of ``version`` (idempotent)."""
        async with self._ctx.session_factory.begin() as session:
            await kv.set(session, consent_key(tg_id), f"{version}|{utcnow().isoformat()}")
            await events.track(session, "consent.accepted", user_id=tg_id, version=version)
        self._accepted.set((tg_id, version), True, CACHE_TTL)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        settings = self._ctx.settings
        if not consent_required(settings) or not isinstance(event, Update) or not self._applies(event, data):
            return await handler(event, data)
        user = from_user(data)
        if user is None:
            return await handler(event, data)
        version = consent_version(settings)
        bot: Bot = data["bot"]
        callback = event.callback_query
        if callback is not None and callback.data == ACCEPT_CALLBACK:
            await self._on_accept(event, data, bot, version)
            return None
        if await self.has_accepted(user.id, version):
            return await handler(event, data)
        self._replayer.remember(user.id, event)
        _ = translator_of(data, self._ctx)
        markup = consent_keyboard(_, settings)
        await respond(event, bot, _("consent.text"), popup=_("consent.required"), reply_markup=markup, as_message=True)
        return None

    @staticmethod
    def _applies(event: Update, data: dict[str, Any]) -> bool:
        if event.message is None and event.callback_query is None and event.inline_query is None:
            return False
        if is_payment_update(event) or is_admin(data) or is_open_command(event):
            return False
        return is_private(event, data)

    async def _on_accept(self, event: Update, data: dict[str, Any], bot: Bot, version: str) -> None:
        callback = event.callback_query
        assert callback is not None
        await self.accept(callback.from_user.id, version)
        _ = translator_of(data, self._ctx)
        await respond(event, bot, _("consent.accepted"))
        message = callback.message
        chat_id = message.chat.id if message is not None else callback.from_user.id
        if message is not None:
            with contextlib.suppress(TelegramAPIError):
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message.message_id, reply_markup=None)
        self._replayer.replay(data["dispatcher"], bot, callback.from_user, chat_id, event.update_id)
