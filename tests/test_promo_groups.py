"""Audience groups resolve the best personal discount deterministically."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.config import Settings
from app.db.models import PromoGroup, PromoGroupMember, WinbackOffer
from app.db.session import SessionFactory
from app.services import promo, subscriptions, winback
from app.utils.time import utcnow
from tests.test_subs_support import add_plan, add_user


async def test_personal_discount_uses_highest_active_group(session_factory: SessionFactory) -> None:
    now = utcnow()
    async with session_factory.begin() as session:
        await add_user(session, 1)
        low = PromoGroup(title="early", discount_percent=10)
        high = PromoGroup(title="vip", discount_percent=25)
        expired = PromoGroup(title="old", discount_percent=50, expires_at=now - timedelta(seconds=1))
        session.add_all([low, high, expired])
        await session.flush()
        session.add_all(
            [
                PromoGroupMember(group_id=low.id, user_id=1),
                PromoGroupMember(group_id=high.id, user_id=1),
                PromoGroupMember(group_id=expired.id, user_id=1),
            ]
        )

    async with session_factory() as session:
        discount = await promo.personal_discount(session, 1, now=now)
    assert discount.percent == 25 and discount.group_id is not None
    assert discount.price(19_900) == 14_925
    assert promo.PersonalDiscount().price(19_900) == 19_900


async def test_personal_discount_is_written_to_purchase_price(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as session:
        user = await add_user(session, 2, balance_kop=16_000)
        plan = await add_plan(session, price_kop=20_000)
        group = PromoGroup(title="one user", discount_percent=20)
        session.add(group)
        await session.flush()
        session.add(PromoGroupMember(group_id=group.id, user_id=user.tg_id))
        price = (await promo.personal_discount(session, user.tg_id)).price(plan.price_kop)
        purchase = await subscriptions.purchase_plan(session, settings, user, plan, price_kop=price)

    assert price == 16_000
    assert purchase.price_kop == price
    assert user.balance_kop == 0


async def test_winback_offer_is_temporary_and_beats_group_discount(
    session_factory: SessionFactory, settings: Settings
) -> None:
    now = utcnow()
    async with session_factory.begin() as session:
        user = await add_user(session, 3)
        user.created_at = now - timedelta(days=4)
        group = PromoGroup(title="regular", discount_percent=10)
        session.add(group)
        await session.flush()
        session.add(PromoGroupMember(group_id=group.id, user_id=user.tg_id))

    enabled = settings.model_copy(
        update={"winback_enabled": True, "winback_discount_percent": 30, "winback_offer_days": 7}
    )
    offers = await winback.issue(session_factory, enabled, now=now)
    assert [(offer.user_id, offer.kind, offer.discount_percent) for offer in offers] == [(3, "trial", 30)]
    async with session_factory() as session:
        assert (await promo.personal_discount(session, 3, now=now)).percent == 30
        offer = await session.scalar(select(WinbackOffer))
    assert offer is not None
    async with session_factory.begin() as session:
        stored = await session.get(WinbackOffer, offer.id)
        assert stored is not None
        stored.expires_at = now - timedelta(seconds=1)
    async with session_factory() as session:
        assert (await promo.personal_discount(session, 3, now=now)).percent == 10


async def _offer_and_buy(session_factory: SessionFactory, settings: Settings, tg_id: int) -> int:
    """Give the user a 30% offer, buy one plan with it and return the discount left afterwards."""
    now = utcnow()
    async with session_factory.begin() as session:
        user = await add_user(session, tg_id, balance_kop=50_000)
        plan = await add_plan(session, price_kop=20_000)
        session.add(
            WinbackOffer(user_id=tg_id, kind="churned", discount_percent=30, expires_at=now + timedelta(days=7))
        )
        await session.flush()
        price = (await promo.personal_discount(session, tg_id)).price(plan.price_kop)
        assert price == 14_000
        await subscriptions.purchase_plan(session, settings, user, plan, price_kop=price)
    async with session_factory() as session:
        return (await promo.personal_discount(session, tg_id)).percent


async def test_single_use_winback_offer_is_spent_by_a_discounted_purchase(
    session_factory: SessionFactory, settings: Settings
) -> None:
    assert await _offer_and_buy(session_factory, settings.model_copy(update={"winback_usage": "once"}), 4) == 0


async def test_period_winback_offer_stays_until_it_expires(session_factory: SessionFactory, settings: Settings) -> None:
    assert await _offer_and_buy(session_factory, settings.model_copy(update={"winback_usage": "period"}), 5) == 30
