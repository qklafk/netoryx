"""Broadcasts: audience segments, drafts, scheduling and the resumable sender."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage
from sqlalchemy import select

from app.db.models import (
    Broadcast,
    BroadcastStatus,
    Location,
    Payment,
    PaymentStatus,
    Provision,
    ProvisionState,
    SubscriptionStatus,
    User,
)
from app.db.session import SessionFactory
from app.services import broadcasts
from app.services.broadcasts import Audience, AudienceSpec, BroadcastError, BroadcastErrorCode, MediaType
from tests.test_subs_support import NOW, add_plan, add_sub, add_user, add_xui_panel, days

ADMIN = 1000
_METHOD = SendMessage(chat_id=1, text="x")


@dataclass
class FakeBot:
    """Records sends; ``behaviour[chat_id]`` is a list of exceptions raised by successive attempts."""

    behaviour: dict[int, list[Exception]] = field(default_factory=dict)
    sent: list[tuple[str, int, dict[str, Any]]] = field(default_factory=list)

    def _attempt(self, kind: str, chat_id: int, **kw: Any) -> None:
        queue = self.behaviour.get(chat_id)
        if queue:
            raise queue.pop(0)
        self.sent.append((kind, chat_id, kw))

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
        self._attempt("message", chat_id, text=text, **kw)

    async def copy_message(self, chat_id: int, from_chat_id: int, message_id: int, **kw: Any) -> None:
        self._attempt("copy", chat_id, from_chat_id=from_chat_id, message_id=message_id, **kw)

    async def send_photo(self, chat_id: int, photo: str, **kw: Any) -> None:
        self._attempt("photo", chat_id, photo=photo, **kw)

    async def send_video(self, chat_id: int, video: str, **kw: Any) -> None:
        self._attempt("video", chat_id, video=video, **kw)

    async def send_animation(self, chat_id: int, animation: str, **kw: Any) -> None:
        self._attempt("animation", chat_id, animation=animation, **kw)

    async def send_document(self, chat_id: int, document: str, **kw: Any) -> None:
        self._attempt("document", chat_id, document=document, **kw)

    @property
    def recipients(self) -> list[int]:
        return [chat_id for _, chat_id, _ in self.sent]


@dataclass
class Ctx:
    bot: Bot
    session_factory: SessionFactory


def _ctx(session_factory: SessionFactory, bot: FakeBot) -> Ctx:
    return Ctx(cast(Bot, bot), session_factory)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float, *args: Any) -> None:
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(broadcasts.asyncio, "sleep", fake_sleep)
    return slept


async def _draft(session_factory: SessionFactory, spec: AudienceSpec | None = None, **kw: Any) -> int:
    kw.setdefault("text", "<b>News</b>")
    async with session_factory.begin() as s:
        draft = await broadcasts.create_draft(s, ADMIN, spec or AudienceSpec(), **kw)
    return draft.id


async def _get(session_factory: SessionFactory, broadcast_id: int) -> Broadcast:
    async with session_factory() as s:
        broadcast = await s.get(Broadcast, broadcast_id)
        assert broadcast is not None
        return broadcast


# ---------------------------------------------------------------------------------------------
# audience
# ---------------------------------------------------------------------------------------------
async def _population(session_factory: SessionFactory) -> dict[str, int]:
    async with session_factory.begin() as s:
        plan = await add_plan(s)
        panel, (inbound,) = await add_xui_panel(s)
        for uid in range(1, 10):
            await add_user(s, uid, language="en" if uid == 3 else "ru")
        await add_sub(s, 1, expires_at=NOW + days(30), plan=plan)  # active, paid
        await add_sub(s, 2, expires_at=NOW + days(2), is_trial=True)  # trial, expiring
        await add_sub(s, 3, expires_at=NOW - days(1), status=SubscriptionStatus.EXPIRED)
        await add_sub(s, 4, expires_at=NOW - days(1))  # ACTIVE but already past expiry
        sub5 = await add_sub(s, 5, expires_at=NOW + days(10))
        # 6-9 have no subscription; 7000 is banned, 8000 blocked the bot: never recipients
        await add_user(s, 7_000, is_banned=True)
        await add_user(s, 8_000, bot_blocked=True)
        s.add(
            Payment(
                id="p1", provider="stars", user_id=1, amount_kop=100, status=PaymentStatus.SUCCEEDED, credited_at=NOW
            )
        )
        donation = Payment(id="p2", provider="stars", user_id=5, amount_kop=100, status=PaymentStatus.SUCCEEDED)
        donation.credited_at, donation.is_donation = NOW, True
        s.add(donation)
        s.add(
            Provision(
                subscription_id=sub5.id,
                panel_id=panel.id,
                inbound_id=inbound.id,
                inbound_key=inbound.id,
                desired_state=ProvisionState.ENABLED,
                actual_state=ProvisionState.ENABLED,
            )
        )
        location = Location(name="Germany", flag="DE")
        s.add(location)
        await s.flush()
        inbound.location_id = location.id
        return {"plan": plan.id, "panel": panel.id, "location": location.id}


async def _ids(session_factory: SessionFactory, spec: AudienceSpec) -> list[int]:
    async with session_factory() as s:
        return sorted((await s.scalars(broadcasts.audience_query(spec, NOW))).all())


async def test_audience_segments(session_factory: SessionFactory) -> None:
    refs = await _population(session_factory)
    cases = {
        AudienceSpec(Audience.ALL): list(range(1, 10)),
        AudienceSpec(Audience.ACTIVE): [1, 2, 5],
        AudienceSpec(Audience.TRIAL): [2],
        AudienceSpec(Audience.EXPIRED): [3, 4],
        AudienceSpec(Audience.NO_SUBSCRIPTION): [6, 7, 8, 9],
        AudienceSpec(Audience.NEVER_PAID): [2, 3, 4, 5, 6, 7, 8, 9],
        AudienceSpec(Audience.EXPIRING, days=3): [2],
        AudienceSpec(Audience.EXPIRING, days=15): [2, 5],
        AudienceSpec(Audience.LANGUAGE, language="en"): [3],
        AudienceSpec(Audience.PLAN, plan_id=refs["plan"]): [1],
        AudienceSpec(Audience.PANEL, panel_id=refs["panel"]): [5],
        AudienceSpec(Audience.LOCATION, location_id=refs["location"]): [5],
        AudienceSpec(Audience.CUSTOM, user_ids=(1, 7_000, 8_000, 9, 424242)): [1, 9],
    }
    for spec, expected in cases.items():
        assert await _ids(session_factory, spec) == expected, spec.kind
    async with session_factory() as s:
        assert await broadcasts.count_audience(s, AudienceSpec(Audience.ACTIVE), NOW) == 3


@pytest.mark.parametrize(
    "spec",
    [
        AudienceSpec(Audience.EXPIRING),
        AudienceSpec(Audience.EXPIRING, days=0),
        AudienceSpec(Audience.LANGUAGE),
        AudienceSpec(Audience.PLAN),
        AudienceSpec(Audience.PANEL),
        AudienceSpec(Audience.LOCATION),
        AudienceSpec(Audience.CUSTOM),
    ],
)
def test_audience_parameters_are_required(spec: AudienceSpec) -> None:
    with pytest.raises(BroadcastError) as exc:
        spec.validate()
    assert exc.value.code == BroadcastErrorCode.BAD_AUDIENCE


def test_audience_spec_round_trip_and_parsers() -> None:
    spec = AudienceSpec(Audience.CUSTOM, user_ids=(5, 6))
    assert AudienceSpec.from_dict(spec.to_dict()) == spec
    assert AudienceSpec.from_dict({"kind": "expiring_in_days", "days": "3"}).days == 3
    with pytest.raises(BroadcastError):
        AudienceSpec.from_dict({"kind": "nope"})
    assert broadcasts.parse_user_ids("1, 2 2;x 3\n-4") == (1, 2, 3, -4)
    assert broadcasts.parse_buttons("Site - https://a.example | Chat - tg://resolve?domain=x\n\nBuy - https://b") == [
        [{"text": "Site", "url": "https://a.example"}, {"text": "Chat", "url": "tg://resolve?domain=x"}],
        [{"text": "Buy", "url": "https://b"}],
    ]
    for bad in ("no url here", "Evil - javascript:alert(1)", " - https://x"):
        with pytest.raises(BroadcastError):
            broadcasts.parse_buttons(bad)


# ---------------------------------------------------------------------------------------------
# drafts
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"text": "  "}, BroadcastErrorCode.EMPTY),
        ({"text": "x" * 4097}, BroadcastErrorCode.TOO_LONG),
        ({"text": "x" * 1025, "media_type": MediaType.PHOTO, "media": "file"}, BroadcastErrorCode.TOO_LONG),
        ({"text": "x", "media_type": MediaType.PHOTO}, BroadcastErrorCode.BAD_MEDIA),
        ({"source_chat_id": 1, "source_message_id": 2, "media_type": MediaType.PHOTO, "media": "f"}, BroadcastErrorCode.BAD_MEDIA),  # noqa: E501
        ({"text": "x", "buttons": [[{"text": "a", "url": "ftp://x"}]]}, BroadcastErrorCode.BAD_BUTTONS),
        ({"text": "x", "buttons": [[]]}, BroadcastErrorCode.BAD_BUTTONS),
    ],
)  # fmt: skip
async def test_draft_validation(session_factory: SessionFactory, kw: dict[str, Any], code: BroadcastErrorCode) -> None:
    async with session_factory.begin() as s:
        with pytest.raises(BroadcastError) as exc:
            await broadcasts.create_draft(s, ADMIN, AudienceSpec(), **kw)
    assert exc.value.code == code


async def test_draft_stores_options(session_factory: SessionFactory) -> None:
    spec = AudienceSpec(Audience.LANGUAGE, language="en")
    broadcast_id = await _draft(
        session_factory, spec, text="cap", media_type=MediaType.PHOTO, media="AgAD", disable_preview=False
    )
    async with session_factory() as s:
        broadcast = await s.get(Broadcast, broadcast_id)
        assert broadcast is not None
        options = await broadcasts.get_options(s, broadcast)
    assert broadcast.status == BroadcastStatus.DRAFT and broadcast.audience == "language"
    assert (options.audience, options.media_type, options.media, options.disable_preview) == (
        spec,
        MediaType.PHOTO,
        "AgAD",
        False,
    )


# ---------------------------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------------------------
async def _users(session_factory: SessionFactory, ids: range) -> None:
    async with session_factory.begin() as s:
        for uid in ids:
            await add_user(s, uid)


async def test_run_sends_counts_and_marks_blocked(session_factory: SessionFactory, _fast_sleep: list[float]) -> None:
    await _users(session_factory, range(1, 8))
    broadcast_id = await _draft(session_factory, buttons=[[{"text": "Open", "url": "https://example.com"}]])
    bot = FakeBot(
        behaviour={
            2: [TelegramForbiddenError(_METHOD, "Forbidden: bot was blocked by the user")],
            3: [TelegramBadRequest(_METHOD, "Bad Request: message is too long")],
            4: [TelegramRetryAfter(_METHOD, "Flood", 3)],
            5: [TelegramBadRequest(_METHOD, "Bad Request: chat not found")],
            6: [RuntimeError("boom")],
        }
    )
    seen: list[broadcasts.Progress] = []

    async def on_progress(progress: broadcasts.Progress) -> None:
        seen.append(progress)

    result = await broadcasts.run(_ctx(session_factory, bot), broadcast_id, rate_per_sec=0, on_progress=on_progress)

    assert result is not None and result.status == BroadcastStatus.DONE
    assert bot.recipients == [1, 4, 7]
    assert (result.sent, result.failed, result.blocked, result.total) == (3, 2, 2, 7)
    assert 3.5 in _fast_sleep  # RetryAfter waited
    kind, _, kw = bot.sent[0]
    assert kind == "message" and kw["parse_mode"] == "HTML" and kw["reply_markup"].inline_keyboard[0][0].text == "Open"
    stored = await _get(session_factory, broadcast_id)
    assert (stored.status, stored.sent, stored.failed, stored.blocked, stored.last_user_id) == (
        BroadcastStatus.DONE,
        3,
        2,
        2,
        7,
    )
    assert stored.started_at is not None and stored.finished_at is not None
    async with session_factory() as s:
        blocked = (await s.scalars(select(User.tg_id).where(User.bot_blocked.is_(True)))).all()
    assert sorted(blocked) == [2, 5]
    assert seen and seen[-1].status == BroadcastStatus.DONE
    assert await broadcasts.run(_ctx(session_factory, bot), broadcast_id) is None  # finished: not runnable


async def test_run_resumes_from_cursor(session_factory: SessionFactory) -> None:
    await _users(session_factory, range(1, 6))
    broadcast_id = await _draft(session_factory)
    async with session_factory.begin() as s:
        await broadcasts.start(s, broadcast_id, ADMIN, now=NOW)
        broadcast = await s.get(Broadcast, broadcast_id)
        assert broadcast is not None and broadcast.total == 5
        broadcast.last_user_id, broadcast.sent = 3, 3
    bot = FakeBot()
    result = await broadcasts.run(_ctx(session_factory, bot), broadcast_id, rate_per_sec=0)
    assert bot.recipients == [4, 5]
    assert result is not None and (result.sent, result.total) == (5, 5)


async def test_cancel_stops_the_runner(session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broadcasts, "PERSIST_EVERY", 2)
    await _users(session_factory, range(1, 11))
    broadcast_id = await _draft(session_factory)
    bot = FakeBot()

    async def cancel_on_first_checkpoint(progress: broadcasts.Progress) -> None:
        async with session_factory.begin() as s:
            await broadcasts.cancel(s, broadcast_id, ADMIN)

    result = await broadcasts.run(
        _ctx(session_factory, bot), broadcast_id, rate_per_sec=0, on_progress=cancel_on_first_checkpoint
    )
    assert result is not None and result.status == BroadcastStatus.CANCELED
    assert bot.recipients == [1, 2, 3, 4]  # stopped at the second checkpoint
    stored = await _get(session_factory, broadcast_id)
    assert stored.status == BroadcastStatus.CANCELED and stored.sent == 2
    async with session_factory.begin() as s:
        assert not await broadcasts.cancel(s, broadcast_id, ADMIN)


async def test_copy_and_media_content(session_factory: SessionFactory) -> None:
    await _users(session_factory, range(1, 3))
    copy_id = await _draft(session_factory, text=None, source_chat_id=ADMIN, source_message_id=77)
    media_ids = {}
    for media_type in MediaType:
        media_ids[media_type] = await _draft(session_factory, text="cap", media_type=media_type, media="FILE")
    bot = FakeBot()
    ctx = _ctx(session_factory, bot)
    await broadcasts.run(ctx, copy_id, rate_per_sec=0)
    assert bot.sent[0][0] == "copy" and bot.sent[0][2]["from_chat_id"] == ADMIN and bot.sent[0][2]["message_id"] == 77
    for media_type, broadcast_id in media_ids.items():
        bot.sent.clear()
        await broadcasts.run(ctx, broadcast_id, rate_per_sec=0)
        assert [kind for kind, _, _ in bot.sent] == [media_type.value, media_type.value]
        assert bot.sent[0][2]["caption"] == "cap"
    bot.sent.clear()
    assert await broadcasts.send_preview(session_factory, cast(Bot, bot), copy_id, ADMIN) == broadcasts.Delivery.SENT
    assert bot.recipients == [ADMIN]


async def test_schedule_and_run_due(session_factory: SessionFactory) -> None:
    await _users(session_factory, range(1, 3))
    later = await _draft(session_factory)
    soon = await _draft(session_factory)
    plain = await _draft(session_factory)
    async with session_factory.begin() as s:
        await broadcasts.schedule(s, later, NOW + timedelta(hours=5), ADMIN, now=NOW)
        await broadcasts.schedule(s, soon, NOW + timedelta(minutes=5), ADMIN, now=NOW)
        with pytest.raises(BroadcastError) as exc:
            await broadcasts.schedule(s, plain, NOW - timedelta(minutes=1), ADMIN, now=NOW)
        assert exc.value.code == BroadcastErrorCode.BAD_SCHEDULE
        assert await broadcasts.due_ids(s, NOW) == []
        assert await broadcasts.due_ids(s, NOW + timedelta(minutes=10)) == [soon]
    bot = FakeBot()
    results = await broadcasts.run_due(_ctx(session_factory, bot), rate_per_sec=0, now=NOW + timedelta(minutes=10))
    assert [r.broadcast_id for r in results] == [soon]
    assert (await _get(session_factory, soon)).status == BroadcastStatus.DONE
    assert (await _get(session_factory, later)).status == BroadcastStatus.DRAFT
    async with session_factory.begin() as s:
        with pytest.raises(BroadcastError):
            await broadcasts.schedule(s, soon, NOW + timedelta(hours=1), ADMIN, now=NOW)
        assert [b.id for b in await broadcasts.history(s, limit=2)] == [plain, soon]


async def test_running_broadcast_is_not_run_twice(session_factory: SessionFactory) -> None:
    await _users(session_factory, range(1, 4))
    broadcast_id = await _draft(session_factory)
    release = asyncio.Event()
    bot = FakeBot()
    original = bot.send_message

    async def slow_send(chat_id: int, text: str, **kw: Any) -> None:
        await release.wait()
        await original(chat_id, text, **kw)

    bot.send_message = slow_send  # type: ignore[method-assign]
    ctx = _ctx(session_factory, bot)
    first = asyncio.create_task(broadcasts.run(ctx, broadcast_id, rate_per_sec=0))
    await asyncio.sleep(0.05)
    assert await broadcasts.run(ctx, broadcast_id, rate_per_sec=0) is None
    release.set()
    result = await first
    assert result is not None and bot.recipients == [1, 2, 3]


async def test_transient_errors_are_retried_then_the_runner_pauses(
    session_factory: SessionFactory, _fast_sleep: list[float]
) -> None:
    """Network errors and Bot API 5xx are not a reason to skip a user: retry, then pause and resume later."""
    await _users(session_factory, range(1, 6))
    broadcast_id = await _draft(session_factory)
    down: list[Exception] = [TelegramNetworkError(_METHOD, "timeout") for _ in range(10)]
    bot = FakeBot(behaviour={2: [TelegramServerError(_METHOD, "Bad Gateway"), TelegramNetworkError(_METHOD, "x")]})
    bot.behaviour[3] = down
    result = await broadcasts.run(_ctx(session_factory, bot), broadcast_id, rate_per_sec=0)
    assert result is not None and result.status == BroadcastStatus.RUNNING
    assert bot.recipients == [1, 2] and (result.sent, result.failed) == (2, 0)
    stored = await _get(session_factory, broadcast_id)
    assert (stored.status, stored.sent, stored.failed, stored.last_user_id) == (BroadcastStatus.RUNNING, 2, 0, 2)

    bot.behaviour.clear()  # Telegram is back: the next tick resumes with user 3
    result = await broadcasts.run(_ctx(session_factory, bot), broadcast_id, rate_per_sec=0)
    assert result is not None and result.status == BroadcastStatus.DONE
    assert bot.recipients == [1, 2, 3, 4, 5] and (result.sent, result.failed) == (5, 0)


async def test_progress_callback_can_interrupt_at_a_checkpoint(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broadcasts, "PERSIST_EVERY", 2)
    await _users(session_factory, range(1, 6))
    broadcast_id = await _draft(session_factory)
    bot = FakeBot()

    async def lose_lock(progress: broadcasts.Progress) -> None:
        raise broadcasts.Interrupted("lock lost")

    result = await broadcasts.run(_ctx(session_factory, bot), broadcast_id, rate_per_sec=0, on_progress=lose_lock)
    assert result is not None and result.status == BroadcastStatus.RUNNING
    assert bot.recipients == [1, 2]
    stored = await _get(session_factory, broadcast_id)
    assert (stored.status, stored.sent, stored.last_user_id) == (BroadcastStatus.RUNNING, 2, 2)
