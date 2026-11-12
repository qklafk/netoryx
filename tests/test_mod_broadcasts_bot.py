"""Broadcasts from the bot: compose, buttons, audience, preview, schedule, send with live progress, stop, history."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.methods import CopyMessage, EditMessageText, SendMessage
from sqlalchemy import select

from app.core.locks import LockManager
from app.db.models import AdminRole, Broadcast, BroadcastStatus, Plan
from app.db.session import SessionFactory
from app.modules.broadcasts import jobs, progress
from app.modules.broadcasts.admin_bot import audience_from_value
from app.modules.broadcasts.callbacks import BcCb
from app.scheduler import LOCK_PREFIX
from app.services import broadcasts
from app.services.broadcasts import Audience
from app.utils.time import utcnow
from tests.test_mod_admin_tools_support import (
    OWNER,
    Harness,
    add_user,
    make_harness,
    module,
    release_module_routers,  # noqa: F401 - autouse fixture
)


async def _harness(session_factory: SessionFactory) -> Harness:
    return await make_harness(session_factory, [module("broadcasts")])


async def _compose(h: Harness, *, buttons: str | None = None) -> None:
    await h.click(BcCb(a="home").pack())
    await h.click(h.button("Новая рассылка"))
    await h.say("Привет! Скидка 20% 🎉")
    if buttons is None:
        await h.click(h.button("Без кнопок"))
    else:
        await h.say(buttons)


async def _draft(session_factory: SessionFactory) -> Broadcast:
    async with session_factory() as session:
        draft = await session.scalar(select(Broadcast).order_by(Broadcast.id.desc()))
    assert draft is not None
    return draft


async def test_compose_to_draft_with_buttons_and_audience(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    for uid in (1, 2, 3):
        await add_user(session_factory, h.ctx.settings, uid)
    await _compose(h, buttons="Канал - https://t.me/channel | Сайт - https://example.com")
    assert "Кому отправить" in h.last()
    await h.click(h.button("Все пользователи"))
    draft = await _draft(session_factory)
    assert draft.status == BroadcastStatus.DRAFT
    assert draft.source_chat_id == OWNER and draft.source_message_id
    assert broadcasts.keyboard(draft.buttons) is not None
    assert f"Рассылка #{draft.id}" in h.last()
    assert "Получателей: <b>3</b>" in h.last()
    assert "Кнопок: 2" in h.last()


async def test_bad_buttons_are_explained(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await _compose(h, buttons="just text without url")
    assert "Не получилось разобрать кнопки" in h.last()


async def test_parametrised_audience_and_custom_ids(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    async with session_factory.begin() as session:
        session.add(Plan(title="Месяц", duration_days=30, price_kop=19900, device_limit=3, traffic_limit_gb=0))
    await _compose(h)
    await h.click(h.button("По тарифу"))
    assert "Выберите тариф" in h.last()
    await h.click(h.button("Месяц"))
    assert "тариф «Месяц»" in h.last()

    await _compose(h)
    await h.click(h.button("Выбранным по ID"))
    await h.say("нет тут чисел")
    assert "Не нашёл ни одного ID" in h.last()
    await h.say("5, 6 7")
    draft = await _draft(session_factory)
    async with session_factory() as session:
        options = await broadcasts.get_options(session, draft)
    assert options.audience.kind == Audience.CUSTOM
    assert options.audience.user_ids == (5, 6, 7)


async def test_preview_goes_to_the_admin(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await _compose(h)
    await h.click(h.button("Все пользователи"))
    await h.click(h.button("Предпросмотр"))
    copies = h.api.of(CopyMessage)
    assert copies and int(copies[-1].chat_id) == OWNER
    assert "Отправил вам предпросмотр" in (h.popups()[-1] or "")


async def test_schedule_and_unschedule(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await add_user(session_factory, h.ctx.settings, 1)
    await _compose(h)
    await h.click(h.button("Все пользователи"))
    await h.click(h.button("Запланировать"))
    await h.say("когда-нибудь")
    assert "Не понял время" in h.last()
    await h.say("+2h")
    draft = await _draft(session_factory)
    async with session_factory() as session:
        options = await broadcasts.get_options(session, draft)
    assert options.scheduled_at is not None
    assert timedelta(hours=1, minutes=59) < options.scheduled_at - utcnow() <= timedelta(hours=2)
    await h.click(h.button("Снять расписание"))
    async with session_factory() as session:
        options = await broadcasts.get_options(session, draft)
    assert options.scheduled_at is None


async def test_send_now_runs_with_progress_and_finishes(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    for uid in (11, 12, 13):
        await add_user(session_factory, h.ctx.settings, uid)
    await _compose(h)
    await h.click(h.button("Все пользователи"))
    await h.click(h.button("Отправить сейчас"))
    assert "Получателей: <b>3</b>" in h.last()
    await h.click(h.button("Да, отправить"))
    draft = await _draft(session_factory)
    assert draft.status == BroadcastStatus.RUNNING
    assert "отправляется" in h.last()

    await jobs.run_broadcasts(h.ctx)  # type: ignore[arg-type]
    draft = await _draft(session_factory)
    assert draft.status == BroadcastStatus.DONE
    assert draft.sent == 3
    recipients = sorted(int(c.chat_id) for c in h.api.of(CopyMessage))
    assert recipients == [11, 12, 13]
    edits = [c for c in h.api.of(EditMessageText) if "завершена" in c.text]
    assert edits, "the progress message shows the result"


async def test_scheduled_broadcast_posts_progress_to_author(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await add_user(session_factory, h.ctx.settings, 21)
    async with session_factory.begin() as session:
        draft = await broadcasts.create_draft(session, OWNER, broadcasts.AudienceSpec(), text="Hi")
        draft_id = draft.id
        await broadcasts.schedule(session, draft_id, utcnow() + timedelta(minutes=1), OWNER)
    await jobs.run_broadcasts(h.ctx)  # type: ignore[arg-type]
    async with session_factory() as session:
        assert (await session.get(Broadcast, draft_id)).status == BroadcastStatus.DRAFT  # type: ignore[union-attr]
    async with session_factory.begin() as session:
        await broadcasts.schedule(session, draft_id, None, OWNER)
        await broadcasts.start(session, draft_id, OWNER)
    await jobs.run_broadcasts(h.ctx)  # type: ignore[arg-type]
    started = [c for c in h.api.of(SendMessage) if int(c.chat_id) == OWNER and "Рассылка" in c.text]
    assert started, "the author gets a progress message"
    assert 21 in [int(c.chat_id) for c in h.api.of(SendMessage)]


async def test_stop_running_broadcast(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await add_user(session_factory, h.ctx.settings, 31)
    await _compose(h)
    await h.click(h.button("Все пользователи"))
    await h.click(h.button("Отправить сейчас"))
    await h.click(h.button("Да, отправить"))
    await h.click(h.button("Остановить"))
    await h.click(h.button("Да, остановить"))
    draft = await _draft(session_factory)
    assert draft.status == BroadcastStatus.CANCELED
    assert "остановлена" in h.last()


async def test_history_lists_broadcasts(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    async with session_factory.begin() as session:
        for _ in range(10):
            await broadcasts.create_draft(session, OWNER, broadcasts.AudienceSpec(), text="x")
    await h.click(BcCb(a="hist").pack())
    assert "История рассылок" in h.last()
    assert any(text.startswith("▶️") for text, _ in h.buttons())
    await h.click(h.button("#10"))
    assert "Рассылка #10" in h.last()


async def test_permissions(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(BcCb(a="home").pack(), role=AdminRole.SUPPORT)
    await h.click(BcCb(a="home").pack(), uid=555, role=None)
    assert h.screens() == []


def test_audience_values() -> None:
    assert audience_from_value("expiring_in_days.3").days == 3
    assert audience_from_value("language.en").language == "en"
    assert audience_from_value("all").kind == Audience.ALL


def test_progress_bar() -> None:
    assert progress.bar(0, 0)[1] == 100
    line, percent = progress.bar(5, 10, width=10)
    assert percent == 50 and line == "▰" * 5 + "▱" * 5


async def test_sender_stops_when_its_lock_was_taken_over(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long broadcast whose job lock expired and went to another process must not keep sending."""
    h = await _harness(session_factory)
    for uid in (41, 42, 43, 44):
        await add_user(session_factory, h.ctx.settings, uid)
    async with session_factory.begin() as session:
        draft = await broadcasts.create_draft(session, OWNER, broadcasts.AudienceSpec(), text="Hi")
        draft_id = draft.id
        await broadcasts.start(session, draft_id, OWNER)
    monkeypatch.setattr(broadcasts, "PERSIST_EVERY", 1)
    monkeypatch.setattr(jobs, "LOCK_RENEW_EVERY_SEC", 0.0)
    h.ctx.locks = LockManager(session_factory, owner="this")
    other = LockManager(session_factory, owner="other")
    assert await other.acquire(LOCK_PREFIX + progress.JOB_ID, 900)

    await jobs.run_broadcasts(h.ctx)  # type: ignore[arg-type]
    async with session_factory() as session:
        stored = await session.get(Broadcast, draft_id)
    assert stored is not None and stored.status == BroadcastStatus.RUNNING and stored.sent == 1
    assert len([c for c in h.api.of(SendMessage) if int(c.chat_id) in (41, 42, 43, 44)]) == 1
