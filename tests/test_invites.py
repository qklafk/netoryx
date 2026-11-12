"""Invitation links: creation, revocation and redemption into a subscription."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import Invite, Provision, Purchase, PurchaseKind, Subscription, SubscriptionStatus, User
from app.db.session import SessionFactory
from app.services import invites
from app.services.invites import UNLIMITED_EXPIRES_AT, InviteCodeExists, InviteOutcome
from tests.test_subs_support import NOW, add_sub, add_user, add_xui_panel, days

OWNER = 1000


async def _invite(session_factory: SessionFactory, **kw: object) -> Invite:
    async with session_factory.begin() as s:
        return await invites.create(s, OWNER, **kw)  # type: ignore[arg-type]


async def _redeem(
    session_factory: SessionFactory, settings: Settings, user_id: int, code: str, *, now: object = NOW
) -> invites.RedeemResult:
    async with session_factory.begin() as s:
        user = await s.get(User, user_id)
        assert user is not None
        return await invites.redeem(s, settings, user, code, now=now)  # type: ignore[arg-type]


async def _sub(session_factory: SessionFactory, user_id: int) -> Subscription:
    async with session_factory() as s:
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == user_id))
        assert sub is not None
        return sub


async def test_unlimited_invite_creates_complimentary_subscription(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        await add_xui_panel(s)
        await add_user(s, 1)
    invite = await _invite(session_factory, days=None, device_limit=5, traffic_limit_gb=0, note=" мама ")
    assert invite.note == "мама" and invite.max_uses == 1 and len(invite.code) == invites.CODE_LENGTH

    result = await _redeem(session_factory, settings, 1, invites.deep_link_payload(invite))

    assert result.ok and result.purchase is not None
    sub = await _sub(session_factory, 1)
    assert sub.expires_at == UNLIMITED_EXPIRES_AT and invites.is_unlimited(sub)
    assert sub.is_complimentary and not sub.auto_renew and not sub.is_trial
    assert (sub.device_limit, sub.traffic_limit_gb, sub.status) == (5, 0, SubscriptionStatus.ACTIVE)
    assert result.purchase.kind == PurchaseKind.INVITE and result.purchase.price_kop == 0
    assert result.purchase.payment_id == invites.redeem_ref(invite.id)
    assert result.invite is not None and result.invite.uses == 1
    async with session_factory() as s:
        assert len((await s.scalars(select(Provision))).all()) == 1  # provisioned on the active inbound
        assert await invites.redeemed_by(s, invite.id) == [1]


async def test_days_invite_extends_and_keeps_generous_limits(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        await add_sub(s, 1, expires_at=NOW + days(10), device_limit=5, traffic_gb=100, auto_renew=True)
    invite = await _invite(session_factory, days=30, device_limit=2, traffic_limit_gb=0, max_uses=0)

    result = await _redeem(session_factory, settings, 1, invite.code.lower())

    assert result.ok
    sub = await _sub(session_factory, 1)
    assert sub.expires_at == NOW + days(40)
    assert (sub.device_limit, sub.traffic_limit_gb) == (5, 0)  # more devices kept, unlimited traffic taken
    assert not sub.is_complimentary and not sub.auto_renew
    assert sub.revision == 2
    assert result.purchase is not None and result.purchase.days == 30


async def test_trial_gets_invite_limits(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        await add_sub(s, 1, expires_at=NOW + days(1), device_limit=1, traffic_gb=10, is_trial=True)
    invite = await _invite(session_factory, days=7, device_limit=2, traffic_limit_gb=50)
    assert (await _redeem(session_factory, settings, 1, invite.code)).ok
    sub = await _sub(session_factory, 1)
    assert (sub.device_limit, sub.traffic_limit_gb, sub.is_trial) == (2, 50, False)
    assert sub.expires_at == NOW + days(8)


async def test_redeem_rules(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        for uid in (1, 2, 3):
            await add_user(s, uid)
        await add_user(s, 4, is_banned=True)
    single = await _invite(session_factory, days=5)
    expired = await _invite(session_factory, days=5, expires_at=NOW - timedelta(minutes=1))
    revoked = await _invite(session_factory, days=5)
    async with session_factory.begin() as s:
        assert await invites.revoke(s, revoked.id, OWNER)
        assert not await invites.revoke(s, revoked.id, OWNER)

    assert (await _redeem(session_factory, settings, 1, "nope")).outcome == InviteOutcome.NOT_FOUND
    assert (await _redeem(session_factory, settings, 1, "")).outcome == InviteOutcome.NOT_FOUND
    assert (await _redeem(session_factory, settings, 1, expired.code)).outcome == InviteOutcome.EXPIRED
    assert (await _redeem(session_factory, settings, 1, revoked.code)).outcome == InviteOutcome.INACTIVE
    assert (await _redeem(session_factory, settings, 4, single.code)).outcome == InviteOutcome.BANNED
    assert (await _redeem(session_factory, settings, 1, single.code)).outcome == InviteOutcome.REDEEMED
    assert (await _redeem(session_factory, settings, 1, single.code)).outcome == InviteOutcome.ALREADY_USED
    assert (await _redeem(session_factory, settings, 2, single.code)).outcome == InviteOutcome.EXHAUSTED


async def test_multi_use_invite_once_per_user(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        for uid in (1, 2, 3):
            await add_user(s, uid)
    invite = await _invite(session_factory, days=3, max_uses=2)
    assert (await _redeem(session_factory, settings, 1, invite.code)).ok
    assert (await _redeem(session_factory, settings, 1, invite.code)).outcome == InviteOutcome.ALREADY_USED
    assert (await _redeem(session_factory, settings, 2, invite.code)).ok
    assert (await _redeem(session_factory, settings, 3, invite.code)).outcome == InviteOutcome.EXHAUSTED
    async with session_factory() as s:
        stored = await s.get(Invite, invite.id)
        assert stored is not None and stored.uses == 2
        count = len((await s.scalars(select(Purchase).where(Purchase.kind == PurchaseKind.INVITE))).all())
    assert count == 2


async def test_unlimited_subscription_is_not_shortened_by_days_invite(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
    first = await _invite(session_factory, days=None)
    second = await _invite(session_factory, days=30, max_uses=0)
    assert (await _redeem(session_factory, settings, 1, first.code)).ok
    result = await _redeem(session_factory, settings, 1, second.code)
    assert result.ok and result.purchase is not None and result.purchase.days == 0
    assert (await _sub(session_factory, 1)).expires_at == UNLIMITED_EXPIRES_AT


async def test_create_validation_and_listing(session_factory: SessionFactory) -> None:
    await _invite(session_factory, code="inv_family-2026", days=None)
    with pytest.raises(InviteCodeExists):
        await _invite(session_factory, code="FAMILY-2026")
    for bad in ({"days": 0}, {"days": 40_000}, {"device_limit": -1}, {"max_uses": -1}, {"code": "a b"}, {"code": "x"}):
        with pytest.raises(ValueError):
            await _invite(session_factory, **bad)
    with pytest.raises(ValueError):
        await _invite(session_factory, expires_at=NOW.replace(tzinfo=None))
    async with session_factory.begin() as s:
        assert (await invites.get_by_code(s, "family-2026")) is not None
        second = await invites.create(s, OWNER, days=3)
        await invites.revoke(s, second.id, OWNER)
        assert [i.code for i in await invites.list_invites(s, active_only=True)] == ["FAMILY-2026"]
        assert len(await invites.list_invites(s)) == 2
