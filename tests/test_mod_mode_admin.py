"""Product mode wizard: shop -> personal, personal -> shop (readiness, members), rollback, notices."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from aiogram.methods import EditUserStarSubscription
from sqlalchemy import select

from app.db.models import AdminRole, Payment, PaymentStatus, Plan, Subscription, SubscriptionStatus
from app.db.session import SessionFactory
from app.modules.admin_mode import admin_bot
from app.modules.admin_mode.admin_bot import ModeCb
from app.utils.time import utcnow
from tests.test_mod_admin_tools_support import (
    Harness,
    add_user,
    make_harness,
    module,
    release_module_routers,  # noqa: F401 - autouse fixture
)


async def _harness(session_factory: SessionFactory, **settings: object) -> Harness:
    return await make_harness(session_factory, [module("admin_mode")], **settings)


async def _subscription(session_factory: SessionFactory, user_id: int, **values: object) -> int:
    async with session_factory.begin() as session:
        sub = Subscription(
            user_id=user_id,
            client_uuid=f"00000000-0000-4000-8000-{user_id:012d}",
            status=SubscriptionStatus.ACTIVE,
            expires_at=utcnow() + timedelta(days=365),
            **values,
        )
        session.add(sub)
        await session.flush()
        return sub.id


async def _drain() -> None:
    await asyncio.gather(*admin_bot._background)


async def test_shop_to_personal_and_rollback(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory, stars_enabled=True)
    await add_user(session_factory, h.ctx.settings, 7)
    sub_id = await _subscription(session_factory, 7, auto_renew=True)
    await h.click(ModeCb(a="home").pack())
    assert "магазин" in h.last()
    await h.click(h.button("личный режим"))
    assert "stars" in h.last() and "у 1 подписчиков" in h.last()
    await h.click(h.button("Переключить"))
    await _drain()
    assert h.ctx.settings.app_mode == "personal"
    assert h.ctx.settings.stars_enabled is False
    async with session_factory() as session:
        assert (await session.get(Subscription, sub_id)).auto_renew is False  # type: ignore[union-attr]
    assert any("автопродление" in text for text in h.sent_to(7))

    await h.click(ModeCb(a="home").pack())
    await h.click(h.button("Откатить"))
    await h.click(h.button("Да, откатить"))
    assert h.ctx.settings.app_mode == "shop" and h.ctx.settings.stars_enabled is True
    async with session_factory() as session:
        assert (await session.get(Subscription, sub_id)).auto_renew is True


async def test_shop_to_personal_cancels_native_stars_subscription(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory, stars_enabled=True)
    await add_user(session_factory, h.ctx.settings, 7)
    sub_id = await _subscription(session_factory, 7, auto_renew=True, stars_charge_id="charge-7")
    await h.click(ModeCb(a="to_personal").pack())
    await h.click(h.button("Переключить"))
    await _drain()
    canceled = [c for c in h.api.calls if isinstance(c, EditUserStarSubscription)]
    assert [(c.user_id, c.telegram_payment_charge_id, c.is_canceled) for c in canceled] == [(7, "charge-7", True)]
    async with session_factory() as session:
        assert (await session.get(Subscription, sub_id)).stars_charge_id is None  # type: ignore[union-attr]


async def test_personal_to_shop_not_ready_then_forced_with_conversion(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory, app_mode="personal")
    await add_user(session_factory, h.ctx.settings, 8)
    sub_id = await _subscription(session_factory, 8, is_complimentary=True, auto_renew=False)
    await h.click(ModeCb(a="to_shop").pack())
    assert "❌" in h.last() and "Участников с бесплатным доступом: 1" in h.last()
    await h.click(h.button("Перевести на платную"))
    await h.click(h.button("14 дней"))
    assert "переходят на обычную подписку" in h.last()
    assert all(text != "✅ Переключить" for text, _ in h.buttons())
    await h.click(h.button("Всё равно переключить"))
    await _drain()
    assert h.ctx.settings.app_mode == "shop"
    async with session_factory() as session:
        sub = await session.get(Subscription, sub_id)
    assert sub is not None and sub.is_complimentary is False
    assert sub.expires_at - utcnow() <= timedelta(days=14)
    assert any("бесплатный доступ сохраняется" in text for text in h.sent_to(8))


async def test_personal_to_shop_ready_keep_members(session_factory: SessionFactory) -> None:
    h = await _harness(
        session_factory,
        app_mode="personal",
        stars_enabled=True,
        support_url="https://t.me/support",
        terms_url="https://example.com/terms",
        privacy_url="https://example.com/privacy",
        public_base_url="https://vpn.example.com",
    )
    async with session_factory.begin() as session:
        session.add(Plan(title="Месяц", duration_days=30, price_kop=19900))
    await add_user(session_factory, h.ctx.settings, 9)
    sub_id = await _subscription(session_factory, 9, is_complimentary=True)
    await h.click(ModeCb(a="prev_shop", v="keep").pack())
    await h.click(h.button("✅ Переключить"))
    assert h.ctx.settings.app_mode == "shop"
    async with session_factory() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.id == sub_id))
    assert sub is not None and sub.is_complimentary is True


async def test_only_owner(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(ModeCb(a="home").pack(), role=AdminRole.ADMIN)
    assert h.screens() == []


async def test_shop_to_personal_warns_about_open_invoices(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory, stars_enabled=True)
    await add_user(session_factory, h.ctx.settings, 7)
    async with session_factory.begin() as session:
        session.add(Payment(id="open", provider="stars", user_id=7, amount_kop=100, status=PaymentStatus.PENDING))
    await h.click(ModeCb(a="home").pack())
    await h.click(h.button("личный режим"))
    assert "неоплаченных счетов" in h.last()
    await h.click(ModeCb(a="go_personal").pack())  # a stale button without force does not switch
    assert h.ctx.settings.app_mode == "shop" and "неоплаченных счетов" in h.last()
    await h.click(h.button("Всё равно"))
    await _drain()
    assert h.ctx.settings.app_mode == "personal"
