"""Tariff audience rules must be enforced in one service layer for bot and Mini App callers."""

from __future__ import annotations

from app.db.models import Payment, PaymentStatus, User
from app.db.session import SessionFactory
from app.services import plans
from tests.test_subs_support import add_user


async def test_tariff_audiences(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as session:
        new = await add_user(session, 1)
        await add_user(session, 10)
        invited = await add_user(session, 2, referred_by=10)
        customer = await add_user(session, 3)
        session.add(
            Payment(id="paid", provider="test", user_id=customer.tg_id, amount_kop=100, status=PaymentStatus.SUCCEEDED)
        )
        await plans.create(session, title="All", duration_days=30, price_kop=100)
        await plans.create(session, title="New", duration_days=30, price_kop=100, availability_rule="new")
        await plans.create(session, title="Customer", duration_days=30, price_kop=100, availability_rule="existing")
        await plans.create(session, title="Invite", duration_days=30, price_kop=100, availability_rule="invited")
        await plans.create(
            session,
            title="Private",
            duration_days=30,
            price_kop=100,
            availability_rule="allowlist",
            allowed_user_ids=[1],
        )

    async with session_factory() as session:
        users = [await session.get(User, user_id) for user_id in (new.tg_id, invited.tg_id, customer.tg_id)]
        offered: list[list[str]] = []
        for user in users:
            assert user is not None
            offered.append([plan.title for plan in await plans.list_available(session, user)])
    assert offered == [["All", "New", "Private"], ["All", "New", "Invite"], ["All", "Customer"]]
