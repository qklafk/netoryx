"""Users section of the bot admin: search, card and per-user actions."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import CopyMessage, SendMessage
from aiogram.types import MessageOriginHiddenUser, MessageOriginUser
from sqlalchemy import func, select

from app.db.models import (
    AdminAuditLog,
    AdminRole,
    BalanceTransaction,
    Inbound,
    Panel,
    PanelKind,
    Plan,
    Provision,
    ProvisionState,
    Purchase,
    PurchaseKind,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.db.session import SessionFactory
from app.services.panels.base import RemoteAccount
from tests.fakes import FakePanel
from tests.test_middlewares_support import tg_user
from tests.test_mod_admin_support import OWNER, USER, Builder, add_admin, add_subscription, add_user


@pytest.fixture
def make(session_factory: SessionFactory, tmp_path: Path) -> Iterator[Builder]:
    builder = Builder(session_factory, tmp_path)
    yield builder
    builder.close()


async def _user(sf: SessionFactory, uid: int = USER) -> User | None:
    async with sf() as s:
        return await s.get(User, uid)


async def _sub(sf: SessionFactory, uid: int = USER) -> Subscription | None:
    async with sf() as s:
        return await s.scalar(select(Subscription).where(Subscription.user_id == uid))


async def _audit_actions(sf: SessionFactory) -> list[str]:
    async with sf() as s:
        return list(await s.scalars(select(AdminAuditLog.action).order_by(AdminAuditLog.id)))


def _confirm(h: object) -> str:
    buttons = h.buttons()  # type: ignore[attr-defined]
    return buttons["✅ Да"]


# --- search ----------------------------------------------------------------------------------------------------------


async def test_search_by_id_username_and_name(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory, first_name="Мария")
    await add_user(session_factory, 5002, first_name="Марина")
    await h.press("nav:adm_users:")
    assert "Пришлите ID" in h.last_text()

    await h.message(str(USER))
    assert "Мария" in h.last_text() and f"<code>{USER}</code>" in h.last_text()

    await h.press("nav:adm_users:")
    await h.message("@user5002")
    assert "Марина" in h.last_text()

    await h.press("nav:adm_users:")
    await h.message("мари")
    assert "Нашлось 2" in h.last_text()

    await h.press("nav:adm_users:")
    await h.message("nobody_here")
    assert "никого не нашлось" in h.last_text()


async def test_search_by_forward_and_user_command(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await h.press("nav:adm_users:")
    origin = MessageOriginUser(date=datetime.now(UTC), sender_user=tg_user(USER))
    await h.message("forwarded", forward_origin=origin)
    assert f"<code>{USER}</code>" in h.last_text()

    await h.press("nav:adm_users:")
    hidden = MessageOriginHiddenUser(date=datetime.now(UTC), sender_user_name="Secret")
    await h.message("forwarded", forward_origin=hidden)
    assert "скрыл аккаунт" in h.last_text()

    await h.message(f"/user {USER}")
    assert "Подписки нет" in h.last_text()


async def test_lists_paginate(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    for uid in range(6000, 6010):
        await add_user(session_factory, uid)
    await h.press("adml:recent:0")
    assert "страница 1" in h.last_text()
    assert "▶️" in h.buttons()
    await h.press("adml:recent:1")
    assert "страница 2" in h.last_text()
    await h.press("adml:banned:0")
    assert "Список пуст" in h.last_text()


async def test_viewer_sees_card_without_actions(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_admin(session_factory, 2002, AdminRole.VIEWER)
    await add_user(session_factory)
    await add_subscription(session_factory)
    await h.press(f"admu:card:{USER}:", uid=2002)
    labels = " ".join(h.buttons())
    assert "История" in labels
    assert "Баланс" not in labels and "Забанить" not in labels and "Удалить" not in labels
    h.reset()
    await h.press(f"admu:bal:{USER}:", uid=2002)
    assert "нет прав" in (h.popups()[-1] or "")


async def test_card_shows_subscription_and_flags(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory, balance_kop=12345, is_banned=True)
    await add_subscription(session_factory, is_complimentary=True, auto_renew=False)
    await h.press(f"nav:adm_user:{USER}")
    text = h.last_text()
    assert "Активна до" in text and "бесплатная" in text and "забанен" in text
    assert "123,45 ₽" in text


# --- money -----------------------------------------------------------------------------------------------------------


async def test_balance_adjust_with_confirmation_is_idempotent(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await h.press(f"admu:bal:{USER}:")
    await h.message("abc")
    assert "Нужна сумма" in h.last_text()
    await h.message("150,50")
    assert "+150,50 ₽" in h.last_text()
    yes = _confirm(h)
    await h.press(yes)
    await h.press(yes)  # double tap
    user = await _user(session_factory)
    assert user is not None and user.balance_kop == 15050
    assert (await _audit_actions(session_factory)).count("user.balance_adjust") == 1

    await h.press(f"admu:bal:{USER}:")
    await h.message("-1000")
    await h.press(_confirm(h))
    assert "недостаточно средств" in h.last_text()
    user = await _user(session_factory)
    assert user is not None and user.balance_kop == 15050


async def test_partner_balance_adjust(make: Builder, session_factory: SessionFactory) -> None:
    h = make(referral_reward_mode="partner")
    await add_user(session_factory)
    await h.press(f"admu:pbal:{USER}:")
    await h.message("300")
    await h.press(_confirm(h))
    user = await _user(session_factory)
    assert user is not None and user.referral_balance_kop == 30000 and user.balance_kop == 0
    async with session_factory() as s:
        assert await s.scalar(select(func.count(BalanceTransaction.id))) == 1


async def test_support_cannot_change_money(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_admin(session_factory, 2003, AdminRole.SUPPORT)
    await add_user(session_factory)
    await h.press(f"admu:bal_ok:{USER}:10000.nonce123", uid=2003)
    user = await _user(session_factory)
    assert user is not None and user.balance_kop == 0


# --- days, plan, limits ----------------------------------------------------------------------------------------------


async def test_add_and_remove_days(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await h.press(f"admu:days:{USER}:")
    await h.message("30")
    assert "Добавить к подписке <b>30 дней</b>" in h.last_text()
    yes = _confirm(h)
    await h.press(yes)
    await h.press(yes)  # double tap: once
    sub = await _sub(session_factory)
    assert sub is not None and sub.status == SubscriptionStatus.ACTIVE
    async with session_factory() as s:
        kinds = list(await s.scalars(select(Purchase.kind)))
    assert kinds == [PurchaseKind.ADMIN_GRANT]
    expires = sub.expires_at

    await h.press(f"admu:days:{USER}:")
    await h.message("-10")
    await h.press(_confirm(h))
    sub = await _sub(session_factory)
    assert sub is not None and abs((expires - sub.expires_at) - timedelta(days=10)) < timedelta(seconds=2)

    await h.press(f"admu:days:{USER}:")
    await h.message("-100")
    await h.press(_confirm(h))
    sub = await _sub(session_factory)
    assert sub is not None and sub.status == SubscriptionStatus.EXPIRED
    assert "subscription.remove_days" in await _audit_actions(session_factory)


async def test_remove_days_without_subscription(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await h.press(f"admu:days:{USER}:")
    await h.message("-5")
    await h.press(_confirm(h))
    assert "нет подписки" in h.last_text()


async def test_change_plan_and_limits(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await add_subscription(session_factory)
    async with session_factory.begin() as s:
        plan = Plan(title="Семейный", duration_days=30, price_kop=50000, device_limit=6, traffic_limit_gb=100)
        s.add(plan)
    await h.press(f"admu:plan:{USER}:")
    assert any("Семейный" in label for label in h.buttons())
    await h.press(f"admu:setplan:{USER}:{plan.id}")
    sub = await _sub(session_factory)
    assert sub is not None and sub.plan_id == plan.id and sub.device_limit == 6 and sub.traffic_limit_gb == 100

    await h.press(f"admu:dev:{USER}:")
    await h.message("0")
    await h.press(f"admu:traf:{USER}:")
    await h.message("x")
    assert "целое число" in h.last_text()
    await h.message("25")
    sub = await _sub(session_factory)
    assert sub is not None and sub.device_limit == 0 and sub.traffic_limit_gb == 25
    assert "без ограничений" in h.last_text()


async def test_toggles_auto_renew_complimentary_and_block(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await add_subscription(session_factory)
    await h.press(f"admu:renew:{USER}:")
    sub = await _sub(session_factory)
    assert sub is not None and sub.auto_renew is False
    await h.press(f"admu:renew:{USER}:")
    await h.press(f"admu:comp:{USER}:")
    sub = await _sub(session_factory)
    assert sub is not None and sub.is_complimentary and sub.auto_renew is False
    await h.press(f"admu:block:{USER}:")
    sub = await _sub(session_factory)
    assert sub is not None and sub.status == SubscriptionStatus.DISABLED
    await h.press(f"admu:unblock:{USER}:")
    sub = await _sub(session_factory)
    assert sub is not None and sub.status == SubscriptionStatus.ACTIVE
    actions = await _audit_actions(session_factory)
    assert "subscription.set_complimentary" in actions and "subscription.set_status" in actions


# --- link, devices, sync ---------------------------------------------------------------------------------------------


async def _provisioned(h: Builder | object, sf: SessionFactory, panel: FakePanel) -> None:
    async with sf.begin() as s:
        s.add(Panel(id=1, name="P1", kind=PanelKind.XUI, url="https://p1"))
        s.add(Inbound(id=1, panel_id=1, remote_id=7, name="in"))
    sub = await _sub(sf)
    assert sub is not None
    async with sf.begin() as s:
        s.add(
            Provision(
                subscription_id=sub.id,
                panel_id=1,
                inbound_id=1,
                inbound_key=1,
                remote_email=f"u{USER}",
                desired_state=ProvisionState.ENABLED,
                actual_state=ProvisionState.ENABLED,
                synced_revision=1,
            )
        )
    panel.accounts[(7, f"u{USER}")] = RemoteAccount(
        email=f"u{USER}",
        uuid=sub.client_uuid,
        enabled=True,
        expires_at=None,
        device_limit=3,
        traffic_limit_bytes=0,
    )


async def test_rotate_link_reset_devices_and_sync(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    panel = FakePanel()
    h.ctx.panels.backends[1] = panel
    await add_user(session_factory)
    await add_subscription(session_factory)
    await _provisioned(h, session_factory, panel)

    await h.press(f"admu:rotate:{USER}:")
    assert "новую ссылку" in h.last_text()
    await h.press(_confirm(h))
    user = await _user(session_factory)
    assert user is not None and user.sub_token != f"token-{USER}"

    await h.press(f"admu:devices:{USER}:")
    await h.press(_confirm(h))
    assert "Устройства сброшены" in h.last_text()
    assert any(call[0] == "reset_devices" for call in panel.calls)

    await h.press(f"admu:sync:{USER}:")
    assert "Сохранено" in h.last_text()


async def test_ban_unban_and_admin_protection(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await h.press(f"admu:ban:{USER}:")
    await h.press(_confirm(h))
    user = await _user(session_factory)
    assert user is not None and user.is_banned
    await h.press(f"admu:unban:{USER}:")
    user = await _user(session_factory)
    assert user is not None and not user.is_banned

    await add_user(session_factory, 2004)
    await add_admin(session_factory, 2004, AdminRole.SUPPORT)
    await h.press("admu:ban_ok:2004:")
    assert "администратор" in h.last_text()
    user = await _user(session_factory, 2004)
    assert user is not None and not user.is_banned


# --- message ---------------------------------------------------------------------------------------------------------


async def test_message_user_copies_any_message_after_confirmation(
    make: Builder, session_factory: SessionFactory
) -> None:
    h = make()
    await add_user(session_factory)
    await h.press(f"admu:msg:{USER}:")
    await h.message("Привет! Сервер переехал.")
    prompt = h.calls(SendMessage)[-1]
    assert "Отправить это сообщение" in prompt.text
    yes = prompt.reply_markup.inline_keyboard[0][0].callback_data
    await h.press(yes)
    await h.press(yes)
    copies = h.calls(CopyMessage)
    assert len(copies) == 1 and copies[0].chat_id == USER and copies[0].from_chat_id == OWNER
    assert "доставлено" in h.last_text()
    assert "user.message" in await _audit_actions(session_factory)


async def test_message_to_user_who_blocked_the_bot(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    h.api.fail(
        CopyMessage,
        TelegramForbiddenError(method=CopyMessage(chat_id=USER, from_chat_id=1, message_id=1), message="blocked"),
    )
    await h.press(f"admu:msg_ok:{USER}:55")
    assert "заблокировал бота" in h.last_text()
    user = await _user(session_factory)
    assert user is not None and user.bot_blocked


# --- history and deletion --------------------------------------------------------------------------------------------


async def test_history_lists_payments_and_ledger(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await h.press(f"admu:bal:{USER}:")
    await h.message("50")
    await h.press(_confirm(h))
    await h.press(f"admu:hist:{USER}:")
    text = h.last_text()
    assert "корректировка админом" in text and "+50 ₽" in text


async def test_delete_user_removes_rows_and_panel_accounts(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    panel = FakePanel()
    h.ctx.panels.backends[1] = panel
    await add_user(session_factory)
    await add_subscription(session_factory)
    await _provisioned(h, session_factory, panel)
    await h.press(f"admu:del:{USER}:")
    assert "насовсем" in h.last_text()
    await h.press(_confirm(h))
    assert "удалён" in h.last_text()
    assert await _user(session_factory) is None
    assert await _sub(session_factory) is None
    assert not panel.accounts
    assert "user.delete" in await _audit_actions(session_factory)


async def test_delete_is_refused_for_admins_and_support(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_admin(session_factory, 2003, AdminRole.SUPPORT)
    await add_user(session_factory)
    await h.press(f"admu:del_ok:{USER}:", uid=2003)
    assert await _user(session_factory) is not None
    await add_user(session_factory, 2005)
    await add_admin(session_factory, 2005, AdminRole.ADMIN)
    await h.press("admu:del_ok:2005:")
    assert await _user(session_factory, 2005) is not None
