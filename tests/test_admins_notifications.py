"""Admin notification routing and the background delivery queue."""

from __future__ import annotations

import asyncio
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendDocument, SendMessage
from aiogram.types import BufferedInputFile

from app.services import notifications
from app.services.notifications import AdminEvent, AdminNotifier, Route, routes
from tests.test_middlewares_support import make_bot, make_test_settings


def test_routes_fall_back_to_admin_chats() -> None:
    settings = make_test_settings(admin_ids=[1, 2], admin_report_chat_ids=[])
    assert routes(settings, AdminEvent.PAYMENTS) == [Route(1), Route(2)]
    settings = make_test_settings(admin_ids=[1], admin_report_chat_ids=[-5, -5, 7])
    assert routes(settings, AdminEvent.ALERTS) == [Route(-5), Route(7)]


def test_routes_use_forum_topics_and_mute() -> None:
    settings = make_test_settings(admin_chat_id=-100, admin_topic_payments=11, admin_notify_mute=["users"])
    assert routes(settings, AdminEvent.PAYMENTS) == [Route(-100, 11)]
    assert routes(settings, AdminEvent.TICKETS) == [Route(-100, None)]  # no topic: general thread
    assert routes(settings, AdminEvent.USERS) == []


def _notifier(**settings: Any) -> tuple[AdminNotifier, Any]:
    bot, api = make_bot()
    current = make_test_settings(**settings)
    return AdminNotifier(bot, lambda: current, group_interval=0.0, private_interval=0.0), api


async def test_delivery_with_topic_and_fallback_to_general_thread() -> None:
    notifier, api = _notifier(admin_chat_id=-100, admin_topic_payments=11)
    method = SendMessage(chat_id=-100, text="x")
    api.fail(SendMessage, TelegramBadRequest(method=method, message="Bad Request: message thread not found"))
    assert notifier.notify(AdminEvent.PAYMENTS, "paid!")
    await notifier.join()
    sent = api.of(SendMessage)
    assert [(m.chat_id, m.message_thread_id) for m in sent] == [(-100, 11), (-100, None)]
    assert notifier.sent == 1
    await notifier.close()


async def test_retry_after_and_unreachable_chats(monkeypatch: Any) -> None:
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(notifications.asyncio, "sleep", fake_sleep)
    notifier, api = _notifier(admin_ids=[1, 2])
    method = SendMessage(chat_id=1, text="x")
    api.fail(
        SendMessage,
        TelegramRetryAfter(method=method, message="flood", retry_after=3),
        TelegramForbiddenError(method=method, message="blocked"),
    )
    notifier.notify(AdminEvent.ALERTS, "alert")
    await notifier.join()
    assert [m.chat_id for m in api.of(SendMessage)] == [1, 1, 2]  # retried chat 1, then chat 2 failed for good
    assert 3.5 in sleeps
    await notifier.close()


async def test_throttle_keys_and_muted_events() -> None:
    notifier, api = _notifier(admin_ids=[1], admin_notify_mute=["backups"])
    assert notifier.notify(AdminEvent.ALERTS, "a", throttle_key="err:x")
    assert not notifier.notify(AdminEvent.ALERTS, "a again", throttle_key="err:x")
    assert notifier.notify(AdminEvent.ALERTS, "b", throttle_key="err:y")
    assert not notifier.notify(AdminEvent.BACKUPS, "muted")
    await notifier.join()
    assert [m.text for m in api.of(SendMessage)] == ["a", "b"]
    await notifier.close()


async def test_queue_full_drops_and_documents_are_sent() -> None:
    bot, api = make_bot()
    current = make_test_settings(admin_ids=[1])
    notifier = AdminNotifier(bot, lambda: current, queue_size=1, private_interval=0.0)
    document = BufferedInputFile(b"backup", filename="backup.gz")
    assert notifier.notify_document(AdminEvent.BACKUPS, document, caption="nightly")
    assert not notifier.notify(AdminEvent.ALERTS, "no room")  # the worker has not run yet
    assert notifier.dropped == 1
    await notifier.join()
    docs = api.of(SendDocument)
    assert len(docs) == 1 and docs[0].caption == "nightly"
    await notifier.close()


class Ctx:
    def __init__(self, bot: Any, settings: Any) -> None:
        self.bot = bot
        self.settings = settings


async def test_close_drains_and_per_context_instance() -> None:
    bot, api = make_bot()
    ctx = Ctx(bot, make_test_settings(admin_ids=[1]))
    notifier = notifications.get_notifier(ctx)  # type: ignore[arg-type]
    assert notifications.get_notifier(ctx) is notifier  # type: ignore[arg-type]
    for i in range(3):
        notifications.notify(ctx, AdminEvent.USERS, f"m{i}")  # type: ignore[arg-type]
    await notifications.close(ctx)  # type: ignore[arg-type]
    assert [m.text for m in api.of(SendMessage)] == ["m0", "m1", "m2"]
    assert notifications.get_notifier(ctx) is not notifier  # type: ignore[arg-type]


def test_notify_without_running_loop_is_queued_later() -> None:
    notifier, _ = _notifier(admin_ids=[1])
    assert notifier.notify(AdminEvent.ALERTS, "before the loop")
    assert notifier.pending == 1
