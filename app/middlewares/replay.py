"""Remembers a ``/start`` that a gate (channel subscription, legal consent) intercepted and replays it later.

When the user passes the gate, the original ``/start <payload>`` update is fed to the dispatcher again, so deep links
(referral, gift, promo, invite...) are not lost. Without a stored update a plain ``/start`` is replayed, which shows
the main menu. Replays run as background tasks, after the current update has finished.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

from app.middlewares.common import TTLCache, is_start

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher

log = logging.getLogger(__name__)

PENDING_TTL = 3600.0


class StartReplayer:
    """Per-user storage of an intercepted ``/start`` update plus the replay itself."""

    def __init__(self, max_size: int = 20_000) -> None:
        self._pending: TTLCache[int, Update] = TTLCache(max_size)
        self._tasks: set[asyncio.Task[Any]] = set()

    def remember(self, user_id: int, update: Update) -> None:
        """Keep ``update`` if it is a ``/start`` (a later ``/start`` replaces an earlier one)."""
        if is_start(update):
            self._pending.set(user_id, update, PENDING_TTL)

    def has_pending(self, user_id: int) -> bool:
        return user_id in self._pending

    def replay(self, dispatcher: Dispatcher, bot: Bot, user: TgUser, chat_id: int, update_id: int) -> None:
        """Schedule the stored ``/start`` of ``user`` (or a plain one) to be processed again."""
        update = self._pending.pop(user.id) or synthetic_start(user, chat_id, update_id)
        task = asyncio.create_task(self._feed(dispatcher, bot, update), name=f"replay-start-{user.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @property
    def pending(self) -> set[asyncio.Task[Any]]:
        """Replays still running."""
        return set(self._tasks)

    async def wait(self) -> None:
        """Wait for scheduled replays (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    @staticmethod
    async def _feed(dispatcher: Dispatcher, bot: Bot, update: Update) -> None:
        try:
            await dispatcher.feed_update(bot, update)
        except Exception:
            log.exception("replaying /start (update %s) failed", update.update_id)


def synthetic_start(user: TgUser, chat_id: int, update_id: int) -> Update:
    """A ``/start`` message from ``user`` in their private chat (handlers only use it to reply)."""
    message = Message(
        message_id=0,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private", first_name=user.first_name, username=user.username),
        from_user=user,
        text="/start",
    )
    return Update(update_id=update_id, message=message)
