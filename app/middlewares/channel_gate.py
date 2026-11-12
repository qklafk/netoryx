"""Required channel subscription (``REQUIRED_CHANNEL_ID``).

Non-members get a "subscribe to the channel" screen with a join button and a "✅ I've subscribed" re-check button;
after joining, the intercepted ``/start`` (with its deep link) is replayed. Membership is cached (10 minutes when
subscribed, 30 seconds when not; the re-check button always asks Telegram). Administrators, payment confirmations,
group chats and the legal commands are never gated. If the bot cannot check membership (it is not an administrator
of the channel, the channel id is wrong...) the gate stays open and the admins are alerted.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, TelegramObject, Update

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
from app.services import notifications

if TYPE_CHECKING:
    from aiogram import Bot

    from app.core.context import AppContext
    from app.core.i18n import Translator

log = logging.getLogger(__name__)

CHECK_CALLBACK = "gate:check"
MEMBER_TTL = 600.0
NON_MEMBER_TTL = 30.0
_MEMBER_STATUSES = frozenset({ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER})


async def check_membership(bot: Bot, channel_id: int, user_id: int) -> bool | None:
    """True / False, or ``None`` if Telegram would not tell (the bot is not an admin of the channel...)."""
    try:
        member = await bot.get_chat_member(channel_id, user_id)
    except TelegramRetryAfter:
        return None
    except TelegramAPIError as exc:
        log.warning("channel gate: cannot check membership in %s: %s", channel_id, exc)
        return None
    if member.status in _MEMBER_STATUSES:
        return True
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False


def gate_keyboard(_: Translator, join_url: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if join_url:
        rows.append([InlineKeyboardButton(text=_("access.channel.subscribe"), url=join_url)])
    rows.append([InlineKeyboardButton(text=_("access.channel.check"), callback_data=CHECK_CALLBACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class ChannelGateMiddleware(BaseMiddleware):
    """Outer ``update`` middleware; runs after the user, throttling and maintenance middlewares."""

    def __init__(self, ctx: AppContext, replayer: StartReplayer | None = None) -> None:
        self._ctx = ctx
        self._replayer = replayer or StartReplayer()
        self._cache: TTLCache[tuple[int, int], bool] = TTLCache(100_000)

    def forget(self, user_id: int) -> None:
        """Drop cached membership of ``user_id`` (e.g. after a ``chat_member`` update says they left)."""
        channel_id = self._ctx.settings.required_channel_id
        if channel_id is not None:
            self._cache.pop((channel_id, user_id))

    async def is_member(self, bot: Bot, channel_id: int, user_id: int, *, fresh: bool = False) -> bool:
        key = (channel_id, user_id)
        if not fresh:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        result = await check_membership(bot, channel_id, user_id)
        if result is None:
            i18n = self._ctx.i18n
            notifications.notify(
                self._ctx,
                notifications.AdminEvent.ALERTS,
                i18n.t("admin_notify.channel_check_failed", i18n.default_lang, channel_id=channel_id),
                throttle_key=f"gate:{channel_id}",
                throttle_sec=600.0,
            )
            return True  # fail open: a misconfiguration must not lock every user out
        self._cache.set(key, result, MEMBER_TTL if result else NON_MEMBER_TTL)
        return result

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        settings = self._ctx.settings
        channel_id = settings.required_channel_id
        if channel_id is None or not isinstance(event, Update) or not self._gated(event, data):
            return await handler(event, data)
        user = from_user(data)
        if user is None:
            return await handler(event, data)
        bot: Bot = data["bot"]
        callback = event.callback_query
        if callback is not None and callback.data == CHECK_CALLBACK:
            await self._recheck(event, data, bot, channel_id)
            return None
        if await self.is_member(bot, channel_id, user.id):
            return await handler(event, data)
        self._replayer.remember(user.id, event)
        _ = translator_of(data, self._ctx)
        join_url = settings.required_channel_url or settings.channel_url
        await respond(event, bot, _("access.channel.text"), reply_markup=gate_keyboard(_, join_url), as_message=True)
        return None

    @staticmethod
    def _gated(event: Update, data: dict[str, Any]) -> bool:
        if event.message is None and event.callback_query is None and event.inline_query is None:
            return False
        if is_payment_update(event) or is_admin(data) or is_open_command(event):
            return False
        return is_private(event, data)

    async def _recheck(self, event: Update, data: dict[str, Any], bot: Bot, channel_id: int) -> None:
        callback = event.callback_query
        assert callback is not None
        _ = translator_of(data, self._ctx)
        if not await self.is_member(bot, channel_id, callback.from_user.id, fresh=True):
            await respond(event, bot, _("access.channel.not_yet"), alert=True)
            return
        await respond(event, bot, _("access.channel.thanks"))
        message = callback.message
        chat_id = message.chat.id if message is not None else callback.from_user.id
        if message is not None:
            with contextlib.suppress(TelegramAPIError):
                await bot.delete_message(chat_id, message.message_id)
        self._replayer.replay(data["dispatcher"], bot, callback.from_user, chat_id, event.update_id)
