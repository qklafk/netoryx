"""A refund or chargeback reported for a payment takes back what that payment gave."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db.models import (
    LedgerAccount,
    Payment,
    PaymentStatus,
    Plan,
    Purchase,
    PurchaseKind,
    ReferralReward,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.db.session import SessionFactory
from app.services.payments import service
from app.services.referrals import payment_source_ref
from app.utils.time import utcnow

USER, INVITER = 501, 502


async def _setup(sf: SessionFactory, *, sub_days_left: int = 40) -> int:
    now = utcnow()
    async with sf.begin() as s:
        s.add(User(tg_id=INVITER, referral_code="INV5", sub_token="tok-i", balance_kop=1_500))
        await s.flush()
        s.add(User(tg_id=USER, referral_code="USR5", sub_token="tok-u", referred_by=INVITER, balance_kop=3_000))
        s.add(Plan(id=7, title="m", duration_days=30, price_kop=12_000))
        await s.flush()
        sub = Subscription(
            user_id=USER,
            status=SubscriptionStatus.ACTIVE,
            expires_at=now + timedelta(days=sub_days_left),
            plan_id=7,
            client_uuid="11111111-1111-4111-8111-111111111111",
        )
        s.add(sub)
        await s.flush()
        s.add(
            Payment(
                id="pay-r",
                user_id=USER,
                provider="yookassa",
                amount_kop=15_000,
                status=PaymentStatus.SUCCEEDED,
                plan_id=7,
            )
        )
        s.add(
            Purchase(
                user_id=USER,
                subscription_id=sub.id,
                plan_id=7,
                kind=PurchaseKind.PURCHASE,
                price_kop=12_000,
                days=30,
                expires_after=sub.expires_at,
                payment_id="pay-r",
            )
        )
        s.add(
            ReferralReward(
                beneficiary_id=INVITER,
                source_user_id=USER,
                level=1,
                source_ref=payment_source_ref("pay-r"),
                amount_kop=1_500,
                account=LedgerAccount.MAIN,
            )
        )
        return sub.id


async def test_refund_takes_back_days_money_and_commissions(session_factory: SessionFactory) -> None:
    sub_id = await _setup(session_factory)
    assert await service.mark_status(session_factory, "pay-r", PaymentStatus.REFUNDED)
    async with session_factory() as s:
        sub = await s.get(Subscription, sub_id)
        user, inviter = await s.get(User, USER), await s.get(User, INVITER)
    assert sub is not None and user is not None and inviter is not None
    # 30 of 40 days were bought with this payment; 3 000 of its 15 000 were left on the balance
    assert abs(sub.expires_at - (utcnow() + timedelta(days=10))) < timedelta(minutes=1)
    assert user.balance_kop == 0 and inviter.balance_kop == 0
    # a repeated notification changes nothing
    assert not await service.mark_status(session_factory, "pay-r", PaymentStatus.REFUNDED)


async def test_refund_never_takes_more_than_is_left(session_factory: SessionFactory) -> None:
    sub_id = await _setup(session_factory, sub_days_left=5)
    async with session_factory.begin() as s:
        user = await s.get(User, USER)
        assert user is not None
        user.balance_kop = 1_000
    await service.mark_status(session_factory, "pay-r", PaymentStatus.REFUNDED)
    async with session_factory() as s:
        sub = await s.get(Subscription, sub_id)
        user = await s.get(User, USER)
    assert sub is not None and sub.status == SubscriptionStatus.EXPIRED  # ends now, never in the past
    assert user is not None and user.balance_kop == 0


async def test_an_admin_refund_leaves_the_decision_to_the_admin(session_factory: SessionFactory) -> None:
    sub_id = await _setup(session_factory)
    async with session_factory() as s:
        before = (await s.get(Subscription, sub_id)).expires_at  # type: ignore[union-attr]
    await service.mark_status(session_factory, "pay-r", PaymentStatus.REFUNDED, claw_back=False)
    async with session_factory() as s:
        sub = await s.get(Subscription, sub_id)
        user = await s.scalar(select(User).where(User.tg_id == USER))
    assert sub is not None and sub.expires_at == before and user is not None and user.balance_kop == 3_000
