"""Update middlewares driven through a real aiogram Dispatcher with a recording Telegram API."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram import Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import AnswerCallbackQuery, AnswerPreCheckoutQuery, GetChatMember, SendMessage
from sqlalchemy import select, update

from app.db.models import Admin, AdminRole, Invite, Subscription, SubscriptionStatus, User
from app.db.session import SessionFactory
from app.middlewares import Middlewares, setup_middlewares
from app.middlewares.channel_gate import CHECK_CALLBACK
from app.middlewares.consent import ACCEPT_CALLBACK, consent_key
from app.services import kv, notifications
from app.services.maintenance import HIT_PREFIX
from app.utils.time import utcnow
from tests.test_middlewares_support import (
    FakeCtx,
    FakeSession,
    Recorder,
    callback_update,
    make_ctx,
    message_update,
    pre_checkout_update,
    successful_payment_update,
)

USER = 5001
OWNER = 1000  # in ADMIN_IDS


class Harness:
    def __init__(self, ctx: FakeCtx, api: FakeSession) -> None:
        self.ctx = ctx
        self.api = api
        self.messages = Recorder()
        self.callbacks = Recorder()
        self.precheckouts = Recorder()
        router = Router()
        router.message.register(self.messages.handle)
        router.callback_query.register(self.callbacks.handle)
        router.pre_checkout_query.register(self.precheckouts.handle)
        ctx.dispatcher.include_router(router)
        self.mw: Middlewares = setup_middlewares(ctx.dispatcher, ctx)  # type: ignore[arg-type]

    async def feed(self, update: Any) -> Any:
        result = await self.ctx.dispatcher.feed_update(self.ctx.bot, update)
        await self.mw.replayer.wait()
        return result


@pytest.fixture
def make(session_factory: SessionFactory, tmp_path: Path) -> Any:
    def factory(**settings: Any) -> Harness:
        ctx, api = make_ctx(session_factory, tmp_path, **settings)
        return Harness(ctx, api)

    return factory


async def _user(sf: SessionFactory, uid: int = USER) -> User | None:
    async with sf() as s:
        return await s.get(User, uid)


async def _add_subscription(sf: SessionFactory, uid: int) -> None:
    async with sf.begin() as s:
        s.add(
            Subscription(
                user_id=uid,
                client_uuid=f"00000000-0000-0000-0000-{uid:012d}",
                status=SubscriptionStatus.ACTIVE,
                expires_at=utcnow() + timedelta(days=30),
            )
        )


# --- context & user ---------------------------------------------------------------------------------------------------
async def test_registers_user_and_injects_data(make: Any, session_factory: SessionFactory) -> None:
    h = make()
    await h.feed(message_update(USER, "/start", username="Ann_X"))
    await h.feed(message_update(USER, "again", username="Ann_X"))

    first, second = h.messages.seen
    assert first["ctx"] is h.ctx and first["settings"] is h.ctx.settings
    assert first["db_user"].tg_id == USER and first["user_created"] is True
    assert second["user_created"] is False
    assert first["admin_role"] is None and first["lang"] == "ru"
    assert first["_"]("access.banned") == "BANNED"
    user = await _user(session_factory)
    assert user is not None and user.username == "ann_x"


async def test_admin_roles_are_injected(make: Any, session_factory: SessionFactory) -> None:
    h = make()
    async with session_factory.begin() as s:
        s.add(Admin(tg_id=2002, role=AdminRole.SUPPORT))
    await h.feed(message_update(OWNER))
    await h.feed(message_update(2002))
    assert [d["admin_role"] for d in h.messages.seen] == [AdminRole.OWNER, AdminRole.SUPPORT]


async def test_last_seen_is_not_rewritten_on_every_update(make: Any, session_factory: SessionFactory) -> None:
    h = make()
    await h.feed(message_update(USER, username="a"))
    old = utcnow() - timedelta(minutes=1)
    async with session_factory.begin() as s:
        await s.execute(update(User).where(User.tg_id == USER).values(last_seen_at=old))
    await h.feed(message_update(USER, username="a"))  # same profile, within the window: read-only path
    user = await _user(session_factory)
    assert user is not None and user.last_seen_at is not None
    assert abs((user.last_seen_at - old).total_seconds()) < 1

    await h.feed(message_update(USER, username="b"))  # profile changed: written at once
    user = await _user(session_factory)
    assert user is not None and user.username == "b"


async def test_group_messages_do_not_register_users(make: Any, session_factory: SessionFactory) -> None:
    h = make()
    await h.feed(message_update(USER, "hello", chat_id=-100500, chat_type="supergroup"))
    assert h.messages.seen[0]["db_user"] is None
    assert await _user(session_factory) is None


async def test_blocked_flag_is_cleared_when_the_user_writes(make: Any, session_factory: SessionFactory) -> None:
    h = make()
    await h.feed(message_update(USER))
    async with session_factory.begin() as s:
        await s.execute(update(User).where(User.tg_id == USER).values(bot_blocked=True))
    await h.feed(message_update(USER))
    user = await _user(session_factory)
    assert user is not None and user.bot_blocked is False


# --- banned users -----------------------------------------------------------------------------------------------------
async def _ban(sf: SessionFactory, uid: int = USER) -> None:
    async with sf.begin() as s:
        await s.execute(update(User).where(User.tg_id == uid).values(is_banned=True))


async def test_banned_user_is_dropped_with_one_notice(make: Any, session_factory: SessionFactory) -> None:
    h = make()
    await h.feed(message_update(USER))
    await _ban(session_factory)
    await h.feed(message_update(USER))
    await h.feed(message_update(USER))
    await h.feed(callback_update(USER, "x"))
    assert len(h.messages.seen) == 1 and not h.callbacks.seen
    assert h.api.texts().count("BANNED") == 1
    assert h.api.of(AnswerCallbackQuery)  # the spinner is stopped silently


async def test_banned_user_payments_still_flow(make: Any, session_factory: SessionFactory) -> None:
    h = make()
    await h.feed(message_update(USER))
    await _ban(session_factory)
    await h.feed(successful_payment_update(USER))
    assert len(h.messages.seen) == 2  # money arrived: the payment handler must run
    await h.feed(pre_checkout_update(USER))
    assert not h.precheckouts.seen
    declined = h.api.of(AnswerPreCheckoutQuery)
    assert declined and declined[0].ok is False and declined[0].error_message == "BANNED"


# --- throttling -------------------------------------------------------------------------------------------------------
async def test_throttling_warns_once_and_exempts_admins(make: Any) -> None:
    h = make(throttle_rate_limit=2, throttle_period_sec=60)
    for _ in range(5):
        await h.feed(message_update(USER))
    assert len(h.messages.seen) == 2
    assert h.api.texts().count("SLOW DOWN") == 1
    for _ in range(5):
        await h.feed(message_update(OWNER))
    assert len(h.messages.seen) == 7


async def test_payments_are_never_throttled(make: Any) -> None:
    h = make(throttle_rate_limit=1, throttle_period_sec=60)
    await h.feed(message_update(USER))
    for _ in range(3):
        await h.feed(successful_payment_update(USER))
        await h.feed(pre_checkout_update(USER))
    assert len(h.messages.seen) == 4 and len(h.precheckouts.seen) == 3


# --- personal mode ----------------------------------------------------------------------------------------------------
async def test_personal_mode_refuses_strangers(make: Any) -> None:
    h = make(app_mode="personal")
    await h.feed(message_update(USER, "/start"))
    await h.feed(callback_update(USER, "menu"))
    assert not h.messages.seen and not h.callbacks.seen
    assert "INVITE ONLY" in h.api.texts()
    assert "INVITE ONLY" in h.api.popups()


async def test_personal_mode_lets_members_owners_and_valid_invites_in(
    make: Any, session_factory: SessionFactory
) -> None:
    h = make(app_mode="personal")
    async with session_factory.begin() as s:
        s.add(Invite(code="FAMILY1", created_by=OWNER, max_uses=1, uses=0, days=None))
        s.add(Invite(code="USEDUP1", created_by=OWNER, max_uses=1, uses=1, days=None))
    await h.feed(message_update(OWNER, "/start"))
    await h.feed(message_update(USER, "/start inv_family1"))
    await h.feed(message_update(USER + 1, "/start inv_USEDUP1"))
    assert [d["event"].from_user.id for d in h.messages.seen] == [OWNER, USER]
    assert "INVITE INVALID" in h.api.texts()

    await h.feed(message_update(USER + 2, "/start"))  # refused, but now known
    await _add_subscription(session_factory, USER + 2)
    await h.feed(message_update(USER + 2, "/start"))
    await h.feed(message_update(USER + 2, "/terms"))
    await h.feed(message_update(USER + 3, "/terms"))  # legal pages are open to everyone
    assert [d["event"].from_user.id for d in h.messages.seen][2:] == [USER + 2, USER + 2, USER + 3]


async def test_personal_mode_member_being_removed_is_refused_at_once(
    make: Any, session_factory: SessionFactory
) -> None:
    h = make(app_mode="personal")
    await h.feed(message_update(USER, "/start"))  # refused, but now known
    await _add_subscription(session_factory, USER)
    await h.feed(message_update(USER, "/start"))
    assert len(h.messages.seen) == 1
    async with session_factory.begin() as s:  # what removing a member does first
        await s.execute(
            update(Subscription).where(Subscription.user_id == USER).values(status=SubscriptionStatus.DISABLED)
        )
        await kv.set(s, f"member_removed:{USER}", utcnow().isoformat())
    await h.feed(message_update(USER, "/start"))
    await h.feed(callback_update(USER, "proxy"))
    assert len(h.messages.seen) == 1 and not h.callbacks.seen
    assert "INVITE ONLY" in h.api.popups()


async def test_shop_mode_has_no_access_policy(make: Any) -> None:
    h = make()
    await h.feed(message_update(USER, "/start"))
    assert len(h.messages.seen) == 1


# --- maintenance ------------------------------------------------------------------------------------------------------
async def test_full_maintenance_blocks_users_and_remembers_them(make: Any, session_factory: SessionFactory) -> None:
    h = make()
    await h.ctx.maintenance.enable(OWNER, eta=utcnow() + timedelta(hours=1))
    await h.feed(message_update(USER))
    await h.feed(callback_update(USER, "x"))
    await h.feed(message_update(OWNER))
    await h.feed(successful_payment_update(USER))
    assert [d["event"].from_user.id for d in h.messages.seen] == [OWNER, USER]  # admin + payment pass
    assert any(t.startswith("MAINT until ") for t in h.api.texts())
    assert "MAINT popup" in h.api.popups()
    async with session_factory() as s:
        assert await kv.get(s, f"{HIT_PREFIX}{USER}") == "1"


async def test_maintenance_custom_message_and_allowlist(make: Any) -> None:
    h = make()
    await h.ctx.maintenance.enable(OWNER, message_ru="Обновляем <сервер>")
    await h.feed(message_update(USER))
    assert "Обновляем &lt;сервер&gt;" in h.api.texts()
    await h.ctx.maintenance.set_allowlist(OWNER, [USER])
    await h.feed(message_update(USER))
    assert len(h.messages.seen) == 1


async def test_partial_maintenance_answers_unhandled_updates(make: Any) -> None:
    h = make()
    h.ctx.dispatcher.sub_routers.clear()  # nothing handles anything, like a module switched off by its filter
    await h.ctx.maintenance.set_disabled_modules(OWNER, ["gifts"])
    await h.feed(callback_update(USER, "gift:open"))
    assert "ERR unavailable" in h.api.popups()


# --- legal consent ----------------------------------------------------------------------------------------------------
async def test_consent_is_asked_once_and_start_is_replayed(make: Any, session_factory: SessionFactory) -> None:
    h = make(legal_consent_required=True, terms_url="https://example.com/terms")
    await h.feed(message_update(USER, "/start ref_ABC"))
    assert not h.messages.seen and "ACCEPT TERMS" in h.api.texts()
    sent = h.api.of(SendMessage)[-1]
    buttons = [b for row in sent.reply_markup.inline_keyboard for b in row]
    assert [b.url for b in buttons if b.url] == ["https://example.com/terms"]
    assert buttons[-1].callback_data == ACCEPT_CALLBACK

    await h.feed(callback_update(USER, ACCEPT_CALLBACK))
    assert [d["event"].text for d in h.messages.seen] == ["/start ref_ABC"]  # the deep link survived
    async with session_factory() as s:
        assert (await kv.get(s, consent_key(USER)) or "").startswith("1|")

    await h.feed(message_update(USER, "next"))
    assert len(h.messages.seen) == 2


async def test_consent_version_bump_asks_again_and_open_commands_pass(make: Any) -> None:
    h = make(legal_consent_required=True, privacy_url="https://example.com/privacy")
    await h.feed(message_update(USER, "/privacy"))
    assert len(h.messages.seen) == 1
    await h.feed(callback_update(USER, ACCEPT_CALLBACK))
    assert h.messages.seen[-1]["event"].text == "/start"  # nothing stored: a plain /start shows the menu
    h.ctx.current = h.ctx.current.model_copy(update={"legal_consent_version": "2"})
    await h.feed(message_update(USER, "hello"))
    assert h.messages.seen[-1]["event"].text == "/start"
    assert h.api.texts().count("ACCEPT TERMS") == 1


async def test_consent_off_without_links(make: Any) -> None:
    h = make(legal_consent_required=True)
    await h.feed(message_update(USER))
    assert len(h.messages.seen) == 1


# --- channel gate -----------------------------------------------------------------------------------------------------
async def test_channel_gate_and_replay_after_joining(make: Any) -> None:
    h = make(required_channel_id=-1001, required_channel_url="https://t.me/chan")
    await h.feed(message_update(USER, "/start gift_XYZ"))
    assert not h.messages.seen and "JOIN CHANNEL" in h.api.texts()

    await h.feed(callback_update(USER, CHECK_CALLBACK))
    assert "NOT YET" in h.api.popups()

    h.api.members[USER] = "member"
    await h.feed(callback_update(USER, CHECK_CALLBACK))
    assert [d["event"].text for d in h.messages.seen] == ["/start gift_XYZ"]
    checks = len(h.api.of(GetChatMember))
    await h.feed(message_update(USER, "more"))
    assert len(h.messages.seen) == 2 and len(h.api.of(GetChatMember)) == checks  # membership cached


async def test_channel_gate_exemptions(make: Any) -> None:
    h = make(required_channel_id=-1001)
    await h.feed(message_update(OWNER))
    await h.feed(successful_payment_update(USER))
    await h.feed(message_update(USER, "/paysupport"))
    await h.feed(message_update(USER, "group", chat_id=-5, chat_type="group"))
    assert len(h.messages.seen) == 4
    assert not h.api.of(GetChatMember)


async def test_channel_gate_fails_open_and_alerts_admins(make: Any) -> None:
    h = make(required_channel_id=-1001)
    h.api.fail(GetChatMember, TelegramBadRequest(method=GetChatMember(chat_id=-1001, user_id=USER), message="no"))
    await h.feed(message_update(USER))
    assert len(h.messages.seen) == 1
    notifier = notifications.get_notifier(h.ctx)
    await notifier.join()
    assert any("cannot check channel -1001" in t for t in h.api.texts())
    await notifier.close()


# --- errors -----------------------------------------------------------------------------------------------------------
async def test_errors_reply_and_alert_admins_once(make: Any) -> None:
    h = make()
    h.messages.raise_with = lambda: RuntimeError("boom")
    await h.feed(message_update(USER))
    await h.feed(message_update(USER))
    notifier = notifications.get_notifier(h.ctx)
    await notifier.join()
    assert h.api.texts().count("ERR generic") == 2
    alerts = [c for c in h.api.of(SendMessage) if c.chat_id == OWNER]
    assert len(alerts) == 1  # rate-limited per error signature
    await notifier.close()


async def test_forbidden_error_marks_user_blocked(make: Any, session_factory: SessionFactory) -> None:
    h = make()
    h.messages.raise_with = lambda: TelegramForbiddenError(method=SendMessage(chat_id=USER, text="x"), message="bl")
    await h.feed(message_update(USER))
    user = await _user(session_factory)
    assert user is not None and user.bot_blocked is True
    assert "ERR generic" not in h.api.texts()


async def test_callback_errors_use_the_short_popup(make: Any) -> None:
    h = make()
    h.callbacks.raise_with = lambda: ValueError("bad")
    await h.feed(callback_update(USER, "x"))
    assert "ERR alert" in h.api.popups()


async def test_order_puts_admin_role_before_gates(make: Any, session_factory: SessionFactory) -> None:
    h = make(app_mode="personal", required_channel_id=-1001, legal_consent_required=True, terms_url="https://t")
    await h.ctx.maintenance.enable(OWNER)
    await h.feed(message_update(OWNER, "/start"))
    assert len(h.messages.seen) == 1
    async with session_factory() as s:
        assert (await s.scalars(select(User.tg_id))).all() == [OWNER]
