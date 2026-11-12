"""Admin notifications: routed to a forum chat (one topic per event type) or to the admins' private chats.

Handlers, jobs and hooks call :meth:`AdminNotifier.notify`, which only puts the message on an in-memory queue and
returns immediately; a background worker delivers it, respecting Telegram's per-chat limits and ``retry_after``.

Routing (read from the current settings on every message, so changes apply at once):

* ``ADMIN_CHAT_ID`` set: the message goes to that chat; ``ADMIN_TOPIC_<EVENT>`` (``message_thread_id`` of a forum
  topic) picks the topic, otherwise the chat's general thread. A deleted topic falls back to the general thread.
* otherwise: every chat of ``ADMIN_REPORT_CHAT_IDS`` (defaults to ``ADMIN_IDS``).
* events listed in ``ADMIN_NOTIFY_MUTE`` are dropped.

Never put secrets into notification texts.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)

if TYPE_CHECKING:
    from aiogram import Bot

    from app.config import Settings
    from app.core.context import AppContext

log = logging.getLogger(__name__)

QUEUE_SIZE = 1000
SEND_ATTEMPTS = 3
MAX_TEXT_LENGTH = 4096
# Telegram allows ~20 messages per minute in a group and ~1 per second in a private chat.
GROUP_INTERVAL_SEC = 3.0
PRIVATE_INTERVAL_SEC = 0.05
MAX_RETRY_AFTER_SEC = 120.0
THROTTLE_KEYS_LIMIT = 2048  # remembered throttle keys before old ones are pruned


class AdminEvent(enum.StrEnum):
    """Notification categories; each maps to an optional forum topic (``ADMIN_TOPIC_<NAME>``)."""

    PAYMENTS = "payments"  # payments, purchases, gifts, donations
    USERS = "users"  # registrations, trials, bans
    TICKETS = "tickets"
    WITHDRAWALS = "withdrawals"
    BACKUPS = "backups"
    ALERTS = "alerts"  # panel health, job failures, unhandled errors, maintenance


@dataclass(frozen=True, slots=True)
class Route:
    chat_id: int
    thread_id: int | None = None


@dataclass(slots=True)
class _Item:
    event: AdminEvent
    text: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    document: Any = None  # aiogram InputFile for documents (backups)


def _setting(settings: Settings, name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def routes(settings: Settings, event: AdminEvent) -> list[Route]:
    """Where a notification of ``event`` goes under ``settings`` (empty if muted or nobody to send to)."""
    muted = {str(m).strip().lower() for m in _setting(settings, "admin_notify_mute", None) or ()}
    if event.value in muted:
        return []
    chat_id: int | None = _setting(settings, "admin_chat_id", None)
    if chat_id:
        topic = _setting(settings, f"admin_topic_{event.value}", None)
        return [Route(chat_id, int(topic) if topic else None)]
    return [Route(int(cid)) for cid in dict.fromkeys(settings.report_chat_ids)]


def _is_thread_error(exc: TelegramBadRequest) -> bool:
    text = str(exc).lower()
    return "thread" in text or "topic" in text


class AdminNotifier:
    """Background delivery of admin notifications (see the module docstring)."""

    def __init__(
        self,
        bot: Bot,
        settings: Callable[[], Settings],
        *,
        queue_size: int = QUEUE_SIZE,
        group_interval: float = GROUP_INTERVAL_SEC,
        private_interval: float = PRIVATE_INTERVAL_SEC,
    ) -> None:
        self._bot = bot
        self._settings = settings
        self._queue: asyncio.Queue[_Item] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self._group_interval = group_interval
        self._private_interval = private_interval
        self._last_sent: dict[int, float] = {}
        self._throttle: dict[str, float] = {}
        self.dropped = 0
        self.sent = 0

    # --- producer side ------------------------------------------------------------------------
    def notify(
        self,
        event: AdminEvent,
        text: str,
        *,
        throttle_key: str | None = None,
        throttle_sec: float = 60.0,
        **kwargs: Any,
    ) -> bool:
        """Queue a notification; never blocks or raises. False if it was dropped (muted, throttled, queue full).

        ``throttle_key`` drops repeats of the same key within ``throttle_sec`` (e.g. one alert per error type per
        minute). Extra keyword arguments go to ``send_message`` (``reply_markup``, ``parse_mode``...).
        """
        if throttle_key is not None and not self._allow(throttle_key, throttle_sec):
            return False
        return self._put(_Item(event, text[:MAX_TEXT_LENGTH], kwargs))

    def notify_document(self, event: AdminEvent, document: Any, caption: str | None = None, **kwargs: Any) -> bool:
        """Queue a document (aiogram ``InputFile``) for the event's chats; same rules as :meth:`notify`."""
        if caption is not None:
            kwargs["caption"] = caption[:1024]
        return self._put(_Item(event, "", kwargs, document))

    def _put(self, item: _Item) -> bool:
        if not routes(self._settings(), item.event):
            return False
        self._ensure_worker()
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1
            log.warning("admin notification queue is full, dropping a %s message", item.event)
            return False
        return True

    def _allow(self, key: str, window: float) -> bool:
        now = time.monotonic()
        last = self._throttle.get(key)
        if last is not None and now - last < window:
            return False
        if len(self._throttle) >= THROTTLE_KEYS_LIMIT:
            cutoff = now - max(window, 3600.0)
            self._throttle = {k: v for k, v in self._throttle.items() if v >= cutoff}
            if len(self._throttle) >= THROTTLE_KEYS_LIMIT:
                self._throttle.clear()
        self._throttle[key] = now
        return True

    # --- worker ---------------------------------------------------------------------------------
    def start(self) -> None:
        """Start the delivery worker (also started lazily by the first notification)."""
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop yet: the worker starts with the first notification sent from async code
        self._task = loop.create_task(self._run(), name="admin-notifier")

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def join(self) -> None:
        """Wait until every queued notification was handled (tests, graceful shutdown)."""
        await self._queue.join()

    async def close(self, drain_seconds: float = 5.0) -> None:
        """Deliver what is queued (up to ``drain_seconds``), then stop the worker."""
        if self._task is None:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), drain_seconds)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                for route in routes(self._settings(), item.event):
                    await self._deliver(route, item)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("admin notification failed")
            finally:
                self._queue.task_done()

    async def _pace(self, chat_id: int) -> None:
        interval = self._group_interval if chat_id < 0 else self._private_interval
        last = self._last_sent.get(chat_id)
        if last is not None:
            wait = last + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_sent[chat_id] = time.monotonic()

    async def _deliver(self, route: Route, item: _Item) -> bool:
        thread_id = route.thread_id
        for _ in range(SEND_ATTEMPTS):
            await self._pace(route.chat_id)
            try:
                await self._send(route.chat_id, thread_id, item)
                self.sent += 1
                return True
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(float(exc.retry_after), MAX_RETRY_AFTER_SEC) + 0.5)
            except TelegramBadRequest as exc:
                if thread_id is not None and _is_thread_error(exc):
                    log.warning(
                        "admin topic %s in chat %s is unavailable, using the general thread", thread_id, route.chat_id
                    )
                    thread_id = None
                    continue
                log.warning("admin notification to %s rejected: %s", route.chat_id, exc)
                return False
            except (TelegramForbiddenError, TelegramNotFound) as exc:
                log.warning("admin chat %s is unreachable: %s", route.chat_id, exc)
                return False
        return False

    async def _send(self, chat_id: int, thread_id: int | None, item: _Item) -> None:
        kwargs = dict(item.kwargs)
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        if item.document is not None:
            await self._bot.send_document(chat_id, item.document, **kwargs)
        else:
            await self._bot.send_message(chat_id, item.text, **kwargs)


# --- per-application instance -----------------------------------------------------------------------------------------
# Keyed by the context object itself (weakly), so a discarded context does not keep its notifier alive.
_NOTIFIERS: weakref.WeakKeyDictionary[Any, AdminNotifier] = weakref.WeakKeyDictionary()


def get_notifier(ctx: AppContext) -> AdminNotifier:
    """The application's notifier (created on first use; one per application context)."""
    notifier = _NOTIFIERS.get(ctx)
    if notifier is None:
        owner = weakref.ref(ctx)  # the notifier must not keep its context alive

        def current() -> Settings:
            alive = owner()
            if alive is None:
                raise RuntimeError("the application context is gone")
            return alive.settings

        notifier = _NOTIFIERS[ctx] = AdminNotifier(ctx.bot, current)
    return notifier


def notify(ctx: AppContext, event: AdminEvent, text: str, **kwargs: Any) -> bool:
    """Shortcut for ``get_notifier(ctx).notify(...)``."""
    return get_notifier(ctx).notify(event, text, **kwargs)


async def close(ctx: AppContext, drain_seconds: float = 5.0) -> None:
    """Flush and stop the application's notifier (call on shutdown)."""
    notifier = _NOTIFIERS.pop(ctx, None)
    if notifier is not None:
        await notifier.close(drain_seconds)
