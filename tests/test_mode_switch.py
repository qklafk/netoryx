"""Product mode switch: readiness checklist, previews, apply and rollback."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import AdminAuditLog, Payment, PaymentStatus, Subscription, SubscriptionStatus
from app.db.session import SessionFactory
from app.services import mode_switch
from app.services.invites import UNLIMITED_EXPIRES_AT
from app.services.mode_switch import MemberPolicy, Mode, ModeSwitchError, NotReady
from tests.conftest import make_settings
from tests.test_subs_support import NOW, add_plan, add_sub, add_user, days

OWNER = 1000
READY: dict[str, Any] = {
    "app_mode": "personal",
    "public_base_url": "https://vpn.example.com",
    "support_url": "https://t.me/support",
    "terms_url": "https://example.com/terms",
    "privacy_url": "https://example.com/privacy",
    "stars_enabled": True,
}


class Recorder:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail = fail or set()

    async def __call__(self, key: str, value: Any) -> None:
        if key in self.fail:
            raise ValueError("rejected")
        self.calls.append((key, value))


async def _members(session_factory: SessionFactory) -> dict[int, int]:
    """Users 1-2 complimentary forever, 3 complimentary for 10 days, 4 a regular paid subscription."""
    async with session_factory.begin() as s:
        result = {}
        for uid in (1, 2, 3, 4):
            await add_user(s, uid)
        result[1] = (await add_sub(s, 1, expires_at=UNLIMITED_EXPIRES_AT, is_complimentary=True, auto_renew=False)).id
        result[2] = (await add_sub(s, 2, expires_at=UNLIMITED_EXPIRES_AT, is_complimentary=True, auto_renew=False)).id
        result[3] = (await add_sub(s, 3, expires_at=NOW + days(10), is_complimentary=True, auto_renew=False)).id
        result[4] = (await add_sub(s, 4, expires_at=NOW + days(20), auto_renew=True)).id
        return result


async def _sub(session_factory: SessionFactory, sub_id: int) -> Subscription:
    async with session_factory() as s:
        sub = await s.get(Subscription, sub_id)
        assert sub is not None
        return sub


def test_permanent_domain() -> None:
    assert mode_switch.is_permanent_domain("https://vpn.example.com")
    for url in (None, "", "http://vpn.example.com", "https://abc.trycloudflare.com", "https://localhost:8080"):
        assert not mode_switch.is_permanent_domain(url)


async def test_readiness_checklist(session_factory: SessionFactory) -> None:
    async with session_factory() as s:
        empty = await mode_switch.readiness_for_shop(s, make_settings(public_base_url="https://x.trycloudflare.com"))
    assert not empty.ready
    assert set(empty.missing) == {"plans", "payment_provider", "support", "terms", "privacy", "domain"}

    async with session_factory.begin() as s:
        await add_plan(s)
        await add_user(s, 1)
    settings = make_settings(**READY)
    async with session_factory() as s:
        ready = await mode_switch.readiness_for_shop(s, settings)
    items = {item.key: item for item in ready.items}
    assert ready.ready and ready.missing == []
    assert not items["test_payment"].ok and items["test_payment"].detail == "stars"
    async with session_factory.begin() as s:
        s.add(Payment(id="p", provider="stars", user_id=1, amount_kop=1, status=PaymentStatus.SUCCEEDED))
    async with session_factory() as s:
        tested = await mode_switch.readiness_for_shop(s, settings)
    assert {i.key: i.ok for i in tested.items}["test_payment"]


async def test_personal_to_shop_converts_members_and_rolls_back(session_factory: SessionFactory) -> None:
    subs = await _members(session_factory)
    settings = make_settings(**READY)
    async with session_factory.begin() as s:
        await add_plan(s)
    grace = NOW + days(5)
    async with session_factory() as s:
        plan = await mode_switch.preview_to_shop(
            s,
            settings,
            default_policy=MemberPolicy.CONVERT,
            per_user={2: MemberPolicy.KEEP},
            grace_until=grace,
            now=NOW,
        )
    assert plan.target == Mode.SHOP and plan.settings_changes == {"app_mode": "shop"}
    assert [(m.user_id, m.policy, m.expires_after) for m in plan.members] == [
        (1, MemberPolicy.CONVERT, grace),
        (2, MemberPolicy.KEEP, UNLIMITED_EXPIRES_AT),
        (3, MemberPolicy.CONVERT, grace),
    ]
    assert plan.notify_user_ids == [1, 3]

    setter = Recorder()
    result = await mode_switch.apply(session_factory, settings, plan, OWNER, set_setting=setter, now=NOW)
    assert (result.converted, result.notify_user_ids, result.settings_failed) == (2, [1, 3], [])
    assert setter.calls == [("app_mode", "shop")]
    one, two, three = [await _sub(session_factory, subs[uid]) for uid in (1, 2, 3)]
    assert (one.is_complimentary, one.expires_at, one.revision) == (False, grace, 2)
    assert (two.is_complimentary, two.expires_at) == (True, UNLIMITED_EXPIRES_AT)
    assert three.expires_at == grace and not three.is_complimentary
    snapshot = await mode_switch.last_switch(session_factory)
    assert snapshot is not None and snapshot["target"] == "shop" and snapshot["settings"] == {"app_mode": "personal"}

    restorer = Recorder()
    assert await mode_switch.rollback(session_factory, OWNER, set_setting=restorer)
    assert restorer.calls == [("app_mode", "personal")]
    one = await _sub(session_factory, subs[1])
    assert (one.is_complimentary, one.expires_at, one.revision) == (True, UNLIMITED_EXPIRES_AT, 3)
    assert (await _sub(session_factory, subs[3])).expires_at == NOW + days(10)
    assert not await mode_switch.rollback(session_factory, OWNER, set_setting=restorer)
    async with session_factory() as s:
        actions = (await s.scalars(select(AdminAuditLog.action).order_by(AdminAuditLog.id))).all()
    assert actions == ["mode.switch", "mode.rollback"]


async def test_shop_requires_readiness_unless_forced(session_factory: SessionFactory) -> None:
    await _members(session_factory)
    settings = make_settings(app_mode="personal")
    async with session_factory() as s:
        plan = await mode_switch.preview_to_shop(s, settings)
    assert all(m.policy == MemberPolicy.KEEP for m in plan.members) and plan.notify_user_ids == []
    with pytest.raises(NotReady) as exc:
        await mode_switch.apply(session_factory, settings, plan, OWNER, set_setting=None)
    assert "plans" in exc.value.missing
    result = await mode_switch.apply(session_factory, settings, plan, OWNER, set_setting=None, force=True)
    assert result.converted == 0 and result.settings_failed == ["app_mode"]  # no setter: nothing changed


async def test_convert_needs_future_grace_date(session_factory: SessionFactory) -> None:
    async with session_factory() as s:
        for grace in (None, NOW - timedelta(minutes=1), NOW.replace(tzinfo=None)):
            with pytest.raises(ModeSwitchError):
                await mode_switch.preview_to_shop(
                    s, make_settings(), default_policy=MemberPolicy.CONVERT, grace_until=grace, now=NOW
                )


async def test_shop_to_personal_closes_sales(session_factory: SessionFactory) -> None:
    subs = await _members(session_factory)
    settings = make_settings(stars_enabled=True, cryptobot_enabled=True, cryptobot_token="t")
    async with session_factory() as s:
        plan = await mode_switch.preview_to_personal(s, settings)
    assert plan.settings_changes == {"app_mode": "personal", "stars_enabled": False, "cryptobot_enabled": False}
    assert plan.auto_renew_off == [subs[4]] and plan.notify_user_ids == [4]
    setter = Recorder(fail={"cryptobot_enabled"})
    result = await mode_switch.apply(session_factory, settings, plan, OWNER, set_setting=setter, now=NOW)
    assert result.auto_renew_disabled == 1 and result.settings_failed == ["cryptobot_enabled"]
    sub = await _sub(session_factory, subs[4])
    assert not sub.auto_renew and sub.expires_at == NOW + days(20) and sub.status == SubscriptionStatus.ACTIVE

    restorer = Recorder()
    assert await mode_switch.rollback(session_factory, OWNER, set_setting=restorer)
    assert (await _sub(session_factory, subs[4])).auto_renew
    assert dict(restorer.calls) == {"app_mode": "shop", "stars_enabled": True, "cryptobot_enabled": True}


async def test_rollback_to_shop_keeps_access_granted_after_the_switch(session_factory: SessionFactory) -> None:
    """Shop -> personal -> roll back: a member extended in between keeps the new expiry and free access."""
    subs = await _members(session_factory)
    async with session_factory.begin() as s:
        await add_user(s, 5)
        subs[5] = (await add_sub(s, 5, expires_at=NOW + days(30), auto_renew=True)).id
    settings = make_settings(stars_enabled=True)
    async with session_factory() as s:
        plan = await mode_switch.preview_to_personal(s, settings)
    await mode_switch.apply(session_factory, settings, plan, OWNER, set_setting=Recorder(), now=NOW)
    async with session_factory.begin() as s:  # the owner gives member 4 unlimited free access
        sub = await s.get(Subscription, subs[4])
        assert sub is not None
        sub.expires_at, sub.is_complimentary = UNLIMITED_EXPIRES_AT, True
        sub.revision += 1

    assert await mode_switch.rollback(session_factory, OWNER, set_setting=Recorder())
    four = await _sub(session_factory, subs[4])
    assert (four.expires_at, four.is_complimentary, four.auto_renew) == (UNLIMITED_EXPIRES_AT, True, False)
    five = await _sub(session_factory, subs[5])
    assert (five.expires_at, five.auto_renew) == (NOW + days(30), True)  # untouched since the switch: restored
    async with session_factory() as s:
        entry = (await s.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "mode.rollback"))).one()
    assert entry.details is not None and json.loads(entry.details)["skipped"] == [subs[4]]


async def test_rollback_to_personal_keeps_time_bought_after_the_switch(session_factory: SessionFactory) -> None:
    """Personal -> shop -> roll back: a converted member who bought a plan meanwhile is not cut back."""
    subs = await _members(session_factory)
    settings = make_settings(**READY)
    async with session_factory.begin() as s:
        await add_plan(s)
    grace = NOW + days(5)
    async with session_factory() as s:
        plan = await mode_switch.preview_to_shop(
            s, settings, default_policy=MemberPolicy.CONVERT, grace_until=grace, now=NOW
        )
    await mode_switch.apply(session_factory, settings, plan, OWNER, set_setting=Recorder(), now=NOW)
    async with session_factory.begin() as s:  # member 3 buys 30 days
        sub = await s.get(Subscription, subs[3])
        assert sub is not None
        sub.expires_at = grace + days(30)

    assert await mode_switch.rollback(session_factory, OWNER, set_setting=Recorder())
    three = await _sub(session_factory, subs[3])
    assert (three.expires_at, three.is_complimentary) == (grace + days(30), False)
    one = await _sub(session_factory, subs[1])
    assert (one.expires_at, one.is_complimentary) == (UNLIMITED_EXPIRES_AT, True)


async def test_open_payments_block_switch_to_personal(session_factory: SessionFactory) -> None:
    """Sales closing would leave invoices the customer can still pay with no gateway to credit them."""
    async with session_factory.begin() as s:
        await add_user(s, 1)
        s.add(Payment(id="open", provider="cryptobot", user_id=1, amount_kop=100, status=PaymentStatus.PENDING))
        s.add(
            Payment(
                id="stale",
                provider="cryptobot",
                user_id=1,
                amount_kop=100,
                status=PaymentStatus.PENDING,
                created_at=mode_switch.utcnow() - timedelta(days=3),
            )
        )
        s.add(Payment(id="paid", provider="cryptobot", user_id=1, amount_kop=100, status=PaymentStatus.SUCCEEDED))
    settings = make_settings(cryptobot_enabled=True, cryptobot_token="t")
    async with session_factory() as s:
        plan = await mode_switch.preview_to_personal(s, settings)
    assert not plan.readiness.ready and plan.readiness.missing == ["pending_payments"]
    assert {i.key: i.detail for i in plan.readiness.items}["pending_payments"] == "1"
    with pytest.raises(NotReady):
        await mode_switch.apply(session_factory, settings, plan, OWNER, set_setting=Recorder())
    assert await mode_switch.last_switch(session_factory) is None
    result = await mode_switch.apply(session_factory, settings, plan, OWNER, set_setting=Recorder(), force=True)
    assert result.target == Mode.PERSONAL

    async with session_factory.begin() as s:
        payment = await s.get(Payment, "open")
        assert payment is not None
        payment.status = PaymentStatus.SUCCEEDED
    async with session_factory() as s:
        assert (await mode_switch.preview_to_personal(s, settings)).readiness.ready


async def test_convert_includes_paused_members(session_factory: SessionFactory) -> None:
    """A paused member with free access is converted too, and stays paused."""
    subs = await _members(session_factory)
    async with session_factory.begin() as s:
        await add_user(s, 5)
        subs[5] = (
            await add_sub(
                s,
                5,
                expires_at=UNLIMITED_EXPIRES_AT,
                is_complimentary=True,
                auto_renew=False,
                status=SubscriptionStatus.DISABLED,
            )
        ).id
        await add_plan(s)
    settings = make_settings(**READY)
    grace = NOW + days(5)
    async with session_factory() as s:
        plan = await mode_switch.preview_to_shop(
            s, settings, default_policy=MemberPolicy.CONVERT, grace_until=grace, now=NOW
        )
    assert 5 in [m.user_id for m in plan.members] and 5 in plan.notify_user_ids
    await mode_switch.apply(session_factory, settings, plan, OWNER, set_setting=Recorder(), now=NOW)
    five = await _sub(session_factory, subs[5])
    assert (five.is_complimentary, five.expires_at, five.status) == (False, grace, SubscriptionStatus.DISABLED)


async def test_switch_to_personal_cancels_native_stars_subscriptions(session_factory: SessionFactory) -> None:
    """Telegram keeps charging a Stars subscription until the bot cancels it: closing sales must cancel them."""
    subs = await _members(session_factory)
    async with session_factory.begin() as s:
        four = await s.get(Subscription, subs[4])
        assert four is not None
        four.stars_charge_id = "charge-4"
        await add_user(s, 5)
        await add_sub(s, 5, expires_at=NOW + days(9), auto_renew=False, stars_charge_id="charge-5")
    settings = make_settings(stars_enabled=True)
    async with session_factory() as s:
        plan = await mode_switch.preview_to_personal(s, settings)
    canceled: list[int] = []

    async def cancel(user_id: int) -> bool:
        canceled.append(user_id)
        return user_id != 5

    result = await mode_switch.apply(
        session_factory, settings, plan, OWNER, set_setting=Recorder(), cancel_stars=cancel, now=NOW
    )
    assert sorted(canceled) == [4, 5]
    assert result.stars_cancel_failed == [5]


async def test_switch_to_personal_without_canceler_reports_stars_subscriptions(session_factory: SessionFactory) -> None:
    subs = await _members(session_factory)
    async with session_factory.begin() as s:
        four = await s.get(Subscription, subs[4])
        assert four is not None
        four.stars_charge_id = "charge-4"
    settings = make_settings(stars_enabled=True)
    async with session_factory() as s:
        plan = await mode_switch.preview_to_personal(s, settings)
    result = await mode_switch.apply(session_factory, settings, plan, OWNER, set_setting=Recorder(), now=NOW)
    assert result.stars_cancel_failed == [4]
