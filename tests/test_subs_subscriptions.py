"""Tests for app.services.subscriptions: trial, purchase, grants, auto-renew, expiry, reminders."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import set_committed_value

from app.config import Settings
from app.db.models import (
    AdminAuditLog,
    BalanceTransaction,
    Event,
    Plan,
    Provision,
    Purchase,
    PurchaseKind,
    Subscription,
    SubscriptionStatus,
    TxKind,
    User,
)
from app.db.session import SessionFactory
from app.services import subscriptions as subs
from app.services.subscriptions import RenewOutcome, SubErrorCode, SubscriptionError
from tests.conftest import make_settings
from tests.test_subs_support import NOW, add_plan, add_rw_panel, add_sub, add_user, add_xui_panel, days


async def _count(session_factory: SessionFactory, model: type) -> int:
    async with session_factory() as s:
        return int(await s.scalar(select(func.count()).select_from(model)) or 0)


async def _events(session_factory: SessionFactory, type_: str) -> list[Event]:
    async with session_factory() as s:
        return list((await s.scalars(select(Event).where(Event.type == type_).order_by(Event.id))).all())


async def _balance(session_factory: SessionFactory, user_id: int) -> int:
    async with session_factory() as s:
        return int(await s.scalar(select(User.balance_kop).where(User.tg_id == user_id)) or 0)


async def _sub_of(session_factory: SessionFactory, user_id: int) -> Subscription:
    async with session_factory() as s:
        sub = await subs.get_for_user(s, user_id)
    assert sub is not None
    return sub


# ---------------------------------------------------------------------------------------------
# Trial
# ---------------------------------------------------------------------------------------------
async def test_start_trial_creates_subscription(session_factory: SessionFactory) -> None:
    settings = make_settings(trial_days=3, trial_device_limit=1, trial_traffic_gb=10)
    async with session_factory.begin() as s:
        user = await add_user(s, 1)
        await add_rw_panel(s)
        sub = await subs.start_trial(s, settings, user, now=NOW)
        assert user.trial_used is True

    sub = await _sub_of(session_factory, 1)
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.expires_at == NOW + days(3)
    assert (sub.device_limit, sub.traffic_limit_gb, sub.is_trial, sub.plan_id) == (1, 10, True, None)
    assert sub.revision == 1 and len(sub.client_uuid) == 36
    async with session_factory() as s:
        db_user = await s.get(User, 1)
        purchase = await s.scalar(select(Purchase))
    assert db_user is not None and db_user.trial_used
    assert purchase is not None
    assert (purchase.kind, purchase.price_kop, purchase.days, purchase.expires_before) == (
        PurchaseKind.TRIAL,
        0,
        3,
        None,
    )
    assert len(await _events(session_factory, "trial.started")) == 1
    assert await _count(session_factory, Provision) == 1


async def test_trial_only_once(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1)
        await subs.start_trial(s, settings, user, now=NOW)
    async with session_factory.begin() as s:
        reloaded = await s.get(User, 1)
        assert reloaded is not None
        with pytest.raises(SubscriptionError) as err:
            await subs.start_trial(s, settings, reloaded, now=NOW)
    assert err.value.code == SubErrorCode.TRIAL_USED


async def test_trial_refused_when_flag_set_concurrently(session_factory: SessionFactory, settings: Settings) -> None:
    """The in-memory user says "unused" but the row was claimed meanwhile: the conditional UPDATE wins."""
    async with session_factory.begin() as s:
        await add_user(s, 1, trial_used=True)
    async with session_factory.begin() as s:
        user = await s.get(User, 1)
        assert user is not None
        set_committed_value(user, "trial_used", False)  # stale in-memory view
        with pytest.raises(SubscriptionError) as err:
            await subs.start_trial(s, settings, user, now=NOW)
    assert err.value.code == SubErrorCode.TRIAL_USED
    assert await _count(session_factory, Subscription) == 0


async def test_trial_disabled(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1)
        with pytest.raises(SubscriptionError) as err:
            await subs.start_trial(s, make_settings(trial_days=0), user)
    assert err.value.code == SubErrorCode.TRIAL_DISABLED


async def test_trial_refused_for_banned_and_existing_subscribers(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        banned = await add_user(s, 1, is_banned=True)
        subscriber = await add_user(s, 2)
        await add_sub(s, 2, expires_at=NOW - days(1), status=SubscriptionStatus.EXPIRED)
        with pytest.raises(SubscriptionError) as err_banned:
            await subs.start_trial(s, settings, banned, now=NOW)
        with pytest.raises(SubscriptionError) as err_existing:
            await subs.start_trial(s, settings, subscriber, now=NOW)
    assert err_banned.value.code == SubErrorCode.BANNED
    assert err_existing.value.code == SubErrorCode.TRIAL_USED


# ---------------------------------------------------------------------------------------------
# Purchase
# ---------------------------------------------------------------------------------------------
async def test_purchase_creates_subscription_and_charges(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1, balance_kop=20_000)
        plan = await add_plan(s, days=30, price_kop=15_000, device_limit=5, traffic_gb=100)
        await add_xui_panel(s, inbounds=2)
        purchase = await subs.purchase_plan(s, settings, user, plan, now=NOW)
        assert user.balance_kop == 5_000

    assert (purchase.kind, purchase.price_kop, purchase.days, purchase.plan_id) == (
        PurchaseKind.PURCHASE,
        15_000,
        30,
        plan.id,
    )
    assert purchase.expires_before is None and purchase.expires_after == NOW + days(30)
    sub = await _sub_of(session_factory, 1)
    assert (sub.device_limit, sub.traffic_limit_gb, sub.plan_id, sub.is_trial) == (5, 100, plan.id, False)
    assert await _balance(session_factory, 1) == 5_000
    async with session_factory() as s:
        tx = await s.scalar(select(BalanceTransaction))
    assert tx is not None and tx.kind == TxKind.PURCHASE and tx.amount_kop == -15_000
    assert tx.ref.startswith("purchase:")
    (event,) = await _events(session_factory, "purchase")
    assert event.amount_kop == 15_000 and event.user_id == 1
    assert json.loads(event.data or "{}")["plan_id"] == plan.id
    assert await _count(session_factory, Provision) == 2


async def test_purchase_extends_active_from_expiry(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1, balance_kop=15_000)
        plan = await add_plan(s, days=30, price_kop=15_000)
        sub = await add_sub(s, 1, expires_at=NOW + days(5), last_reminder_days=1)
        purchase = await subs.purchase_plan(s, settings, user, plan, now=NOW)
    sub = await _sub_of(session_factory, 1)
    assert sub.expires_at == NOW + days(35)
    assert purchase.expires_before == NOW + days(5)
    assert sub.revision == 2
    assert sub.last_reminder_days is None


async def test_purchase_after_expiry_starts_from_now(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1, balance_kop=15_000)
        plan = await add_plan(s, days=30, price_kop=15_000)
        await add_sub(s, 1, expires_at=NOW - days(10), status=SubscriptionStatus.EXPIRED)
        await subs.purchase_plan(s, settings, user, plan, now=NOW)
    sub = await _sub_of(session_factory, 1)
    assert sub.expires_at == NOW + days(30)
    assert sub.status == SubscriptionStatus.ACTIVE


async def test_purchase_ends_trial_and_takes_new_plan_limits(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1, balance_kop=40_000)
        plan = await add_plan(s, days=90, price_kop=40_000, device_limit=7, traffic_gb=0)
        await add_sub(s, 1, expires_at=NOW + days(1), is_trial=True, device_limit=1, traffic_gb=5)
        await subs.purchase_plan(s, settings, user, plan, now=NOW)
    sub = await _sub_of(session_factory, 1)
    assert (sub.is_trial, sub.device_limit, sub.traffic_limit_gb) == (False, 7, 0)
    assert sub.expires_at == NOW + days(91)


async def test_purchase_insufficient_funds_writes_nothing(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1, balance_kop=100)
        plan = await add_plan(s, price_kop=15_000)
        with pytest.raises(SubscriptionError) as err:
            await subs.purchase_plan(s, settings, user, plan, now=NOW)
        # The caller may catch the error and commit: nothing must have been written.
    assert err.value.code == SubErrorCode.INSUFFICIENT_FUNDS
    assert await _count(session_factory, Subscription) == 0
    assert await _count(session_factory, BalanceTransaction) == 0
    assert await _count(session_factory, Purchase) == 0
    assert await _balance(session_factory, 1) == 100


@pytest.mark.parametrize(
    ("banned", "plan_active", "status", "code"),
    [
        (True, True, None, SubErrorCode.BANNED),
        (False, False, None, SubErrorCode.NO_PLAN),
        (False, True, SubscriptionStatus.DISABLED, SubErrorCode.DISABLED),
    ],
)
async def test_purchase_refusals_charge_nothing(
    session_factory: SessionFactory,
    settings: Settings,
    banned: bool,
    plan_active: bool,
    status: SubscriptionStatus | None,
    code: SubErrorCode,
) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1, balance_kop=50_000, is_banned=banned)
        plan = await add_plan(s, is_active=plan_active)
        if status is not None:
            await add_sub(s, 1, expires_at=NOW + days(3), status=status)
        with pytest.raises(SubscriptionError) as err:
            await subs.purchase_plan(s, settings, user, plan, now=NOW)
    assert err.value.code == code
    assert await _balance(session_factory, 1) == 50_000


async def test_archived_plan_can_be_bought_with_a_payment_made_for_it(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1, balance_kop=15_000)
        plan = await add_plan(s, is_active=False)
        purchase = await subs.purchase_plan(s, settings, user, plan, payment_id="pay-1", now=NOW)
    assert purchase.payment_id == "pay-1"


async def test_purchase_for_same_payment_is_idempotent(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1, balance_kop=50_000)
        plan = await add_plan(s, price_kop=15_000)
        first = await subs.purchase_plan(s, settings, user, plan, payment_id="pay-1", now=NOW)
    async with session_factory.begin() as s:
        user2 = await s.get(User, 1)
        plan2 = await s.get(Plan, plan.id)
        assert user2 is not None and plan2 is not None
        second = await subs.purchase_plan(s, settings, user2, plan2, payment_id="pay-1", now=NOW + days(1))
    assert second.id == first.id
    assert await _balance(session_factory, 1) == 35_000
    assert (await _sub_of(session_factory, 1)).expires_at == NOW + days(30)
    async with session_factory() as s:
        refs = list(await s.scalars(select(BalanceTransaction.ref)))
    assert refs == ["purchase:pay-1"]


async def test_free_plan_purchase(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1)
        plan = await add_plan(s, price_kop=0)
        await subs.purchase_plan(s, settings, user, plan, now=NOW)
    assert (await _sub_of(session_factory, 1)).expires_at == NOW + days(30)


async def test_purchase_plan_rejects_grant_kinds(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1)
        plan = await add_plan(s)
        with pytest.raises(ValueError):
            await subs.purchase_plan(s, settings, user, plan, kind=PurchaseKind.PROMO)


# ---------------------------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------------------------
async def test_grant_creates_subscription_with_first_active_plan_limits(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1)
        await add_plan(s, device_limit=9, sort_order=5, title="late")
        await add_plan(s, device_limit=4, traffic_gb=20, sort_order=1, title="first")
        await add_plan(s, device_limit=1, sort_order=0, is_active=False, title="archived")
        purchase = await subs.grant_days(s, settings, user, 7, kind=PurchaseKind.PROMO, now=NOW)
    sub = await _sub_of(session_factory, 1)
    assert (sub.device_limit, sub.traffic_limit_gb, sub.plan_id, sub.is_trial) == (4, 20, None, False)
    assert sub.expires_at == NOW + days(7)
    assert (purchase.kind, purchase.price_kop, purchase.days) == (PurchaseKind.PROMO, 0, 7)
    assert len(await _events(session_factory, "subscription.granted")) == 1
    assert await _count(session_factory, AdminAuditLog) == 0


async def test_grant_without_any_plan_uses_trial_limits(session_factory: SessionFactory) -> None:
    settings = make_settings(trial_device_limit=2, trial_traffic_gb=15)
    async with session_factory.begin() as s:
        user = await add_user(s, 1)
        await subs.grant_days(s, settings, user, 3, kind=PurchaseKind.PROMO, now=NOW)
    sub = await _sub_of(session_factory, 1)
    assert (sub.device_limit, sub.traffic_limit_gb) == (2, 15)


async def test_gift_redeem_applies_plan_limits(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1)
        plan = await add_plan(s, days=90, device_limit=6)
        await add_sub(s, 1, expires_at=NOW + days(2), device_limit=1, is_trial=True)
        await subs.grant_days(s, settings, user, 90, kind=PurchaseKind.GIFT_REDEEM, plan=plan, now=NOW)
    sub = await _sub_of(session_factory, 1)
    assert (sub.device_limit, sub.plan_id, sub.is_trial) == (6, plan.id, False)
    assert sub.expires_at == NOW + days(92)


async def test_grant_without_plan_keeps_existing_limits(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1)
        await add_plan(s, device_limit=9)
        await add_sub(s, 1, expires_at=NOW + days(2), device_limit=2, traffic_gb=3)
        await subs.grant_days(s, settings, user, 5, kind=PurchaseKind.PROMO, now=NOW)
    sub = await _sub_of(session_factory, 1)
    assert (sub.device_limit, sub.traffic_limit_gb, sub.revision) == (2, 3, 2)


async def test_admin_grant_on_disabled_subscription_keeps_block_and_audits(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1)
        await add_sub(s, 1, expires_at=NOW + days(2), status=SubscriptionStatus.DISABLED)
        purchase = await subs.grant_days(s, settings, user, 10, kind=PurchaseKind.ADMIN_GRANT, actor_id=1000, now=NOW)
    sub = await _sub_of(session_factory, 1)
    assert sub.status == SubscriptionStatus.DISABLED
    assert sub.expires_at == NOW + days(12)
    assert purchase.actor_id == 1000
    async with session_factory() as s:
        entry = await s.scalar(select(AdminAuditLog))
    assert entry is not None and entry.action == "subscription.grant_days" and entry.target_id == "1"


@pytest.mark.parametrize(
    ("days_", "kind"),
    [(0, PurchaseKind.PROMO), (-3, PurchaseKind.PROMO), (5, PurchaseKind.PURCHASE), (5, PurchaseKind.TRIAL)],
)
async def test_grant_days_validation(
    session_factory: SessionFactory, settings: Settings, days_: int, kind: PurchaseKind
) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 1)
        with pytest.raises(ValueError):
            await subs.grant_days(s, settings, user, days_, kind=kind)


# ---------------------------------------------------------------------------------------------
# Status & auto-renew flag
# ---------------------------------------------------------------------------------------------
async def test_set_status_disable_and_reenable(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        sub = await add_sub(s, 1, expires_at=NOW + days(5))
        assert await subs.set_status(s, sub, SubscriptionStatus.DISABLED, 1000, now=NOW) == SubscriptionStatus.DISABLED
        assert sub.revision == 2
        assert await subs.set_status(s, sub, SubscriptionStatus.DISABLED, 1000, now=NOW) == SubscriptionStatus.DISABLED
        assert sub.revision == 2  # no-op
        assert await subs.set_status(s, sub, SubscriptionStatus.ACTIVE, 1000, now=NOW) == SubscriptionStatus.ACTIVE
        assert sub.revision == 3
    assert await _count(session_factory, AdminAuditLog) == 2


async def test_reenabling_past_expiry_yields_expired(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        sub = await add_sub(s, 1, expires_at=NOW - days(1), status=SubscriptionStatus.DISABLED)
        assert await subs.set_status(s, sub, SubscriptionStatus.ACTIVE, 1000, now=NOW) == SubscriptionStatus.EXPIRED


async def test_set_auto_renew(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        sub = await add_sub(s, 1, expires_at=NOW + days(5))
        await subs.set_auto_renew(s, sub, False)
    assert (await _sub_of(session_factory, 1)).auto_renew is False


# ---------------------------------------------------------------------------------------------
# Auto-renew
# ---------------------------------------------------------------------------------------------
async def _renewable(
    session_factory: SessionFactory,
    *,
    balance_kop: int = 20_000,
    expires_in: timedelta = timedelta(hours=10),
    **kw: Any,
) -> tuple[int, int]:
    async with session_factory.begin() as s:
        await add_user(s, 1, balance_kop=balance_kop)
        plan = await add_plan(s, days=30, price_kop=15_000)
        sub = await add_sub(s, 1, expires_at=NOW + expires_in, plan=plan, **kw)
        return sub.id, plan.id


@pytest.mark.parametrize(
    ("setup", "due"),
    [
        ({}, True),
        ({"expires_in": timedelta(hours=24)}, True),  # window edge (AUTO_RENEW_HOURS_BEFORE=24)
        ({"expires_in": timedelta(hours=25)}, False),
        ({"expires_in": -timedelta(hours=2), "status": SubscriptionStatus.EXPIRED}, True),  # grace
        ({"expires_in": -days(2), "status": SubscriptionStatus.EXPIRED}, False),
        ({"auto_renew": False}, False),
        ({"balance_kop": 14_999}, False),
        ({"balance_kop": 15_000}, True),
        ({"stars_charge_id": "charge"}, False),
        ({"is_complimentary": True}, False),
        ({"status": SubscriptionStatus.DISABLED}, False),
    ],
)
async def test_due_for_auto_renew(
    session_factory: SessionFactory, settings: Settings, setup: dict[str, object], due: bool
) -> None:
    await _renewable(session_factory, **setup)  # type: ignore[arg-type]
    async with session_factory() as s:
        result = await subs.due_for_auto_renew(s, settings, NOW)
    assert bool(result) is due


async def test_trial_and_banned_are_not_auto_renewed(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1, balance_kop=99_999)
        await add_user(s, 2, balance_kop=99_999, is_banned=True)
        plan = await add_plan(s)
        await add_sub(s, 1, expires_at=NOW + timedelta(hours=1), is_trial=True)  # no plan
        await add_sub(s, 2, expires_at=NOW + timedelta(hours=1), plan=plan)
    async with session_factory() as s:
        assert await subs.due_for_auto_renew(s, settings, NOW) == []


async def test_run_auto_renew_charges_once_per_period(session_factory: SessionFactory, settings: Settings) -> None:
    sub_id, plan_id = await _renewable(session_factory, balance_kop=40_000)
    async with session_factory() as s:
        sub = await s.get(Subscription, sub_id)
        assert sub is not None
        expected_ref = subs.renewal_ref(sub)

    results = await subs.run_auto_renew(session_factory, settings, NOW)

    ((user_id, purchase, outcome),) = results
    assert (user_id, outcome) == (1, RenewOutcome.RENEWED)
    assert purchase is not None and purchase.kind == PurchaseKind.AUTO_RENEW and purchase.plan_id == plan_id
    assert await _balance(session_factory, 1) == 25_000
    assert (await _sub_of(session_factory, 1)).expires_at == NOW + timedelta(hours=10) + days(30)
    async with session_factory() as s:
        refs = list(await s.scalars(select(BalanceTransaction.ref)))
    assert refs == [expected_ref]
    assert expected_ref.startswith(f"renew:{sub_id}:")
    assert len(await _events(session_factory, "renewal")) == 1

    # Running again (same or slightly later time) never charges the same period twice.
    assert await subs.run_auto_renew(session_factory, settings, NOW) == []
    assert await subs.run_auto_renew(session_factory, settings, NOW + timedelta(hours=1)) == []
    assert await _balance(session_factory, 1) == 25_000


async def test_auto_renew_period_already_charged_is_not_charged_again(
    session_factory: SessionFactory, settings: Settings
) -> None:
    sub_id, _ = await _renewable(session_factory)
    async with session_factory.begin() as s:
        sub = await s.get(Subscription, sub_id)
        assert sub is not None
        s.add(BalanceTransaction(user_id=1, amount_kop=-15_000, kind=TxKind.PURCHASE, ref=subs.renewal_ref(sub)))

    ((_, purchase, outcome),) = await subs.run_auto_renew(session_factory, settings, NOW)

    assert (purchase, outcome) == (None, RenewOutcome.ALREADY_RENEWED)
    assert await _balance(session_factory, 1) == 20_000
    assert (await _sub_of(session_factory, 1)).expires_at == NOW + timedelta(hours=10)


async def test_auto_renew_of_expired_subscription_in_grace(session_factory: SessionFactory, settings: Settings) -> None:
    await _renewable(session_factory, expires_in=-timedelta(hours=3), status=SubscriptionStatus.EXPIRED)
    ((_, _, outcome),) = await subs.run_auto_renew(session_factory, settings, NOW)
    assert outcome == RenewOutcome.RENEWED
    sub = await _sub_of(session_factory, 1)
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.expires_at == NOW + days(30)  # from now, not from the past expiry


async def test_auto_renew_grandfathers_archived_plans(session_factory: SessionFactory, settings: Settings) -> None:
    _, plan_id = await _renewable(session_factory)
    async with session_factory.begin() as s:
        plan = await s.get(Plan, plan_id)
        assert plan is not None
        plan.is_active = False
    ((_, _, outcome),) = await subs.run_auto_renew(session_factory, settings, NOW)
    assert outcome == RenewOutcome.RENEWED


async def test_renew_one_rechecks_eligibility(session_factory: SessionFactory, settings: Settings) -> None:
    sub_id, _ = await _renewable(session_factory)
    async with session_factory.begin() as s:
        sub = await s.get(Subscription, sub_id)
        assert sub is not None
        sub.auto_renew = False
    async with session_factory.begin() as s:
        assert await subs._renew_one(s, settings, sub_id, NOW) == (None, RenewOutcome.NOT_DUE)
        assert await subs._renew_one(s, settings, 999, NOW) == (None, RenewOutcome.NOT_DUE)


async def test_renew_one_reports_business_errors(session_factory: SessionFactory, settings: Settings) -> None:
    sub_id, _ = await _renewable(session_factory, balance_kop=10)  # balance dropped after selection
    async with session_factory.begin() as s:
        purchase, outcome = await subs._renew_one(s, settings, sub_id, NOW)
    assert (purchase, outcome) == (None, RenewOutcome.INSUFFICIENT_FUNDS)


async def test_run_auto_renew_survives_unexpected_errors(
    session_factory: SessionFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _renewable(session_factory)

    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("db exploded")

    monkeypatch.setattr(subs, "_renew_one", boom)
    assert await subs.run_auto_renew(session_factory, settings, NOW) == [(1, None, RenewOutcome.ERROR)]


# ---------------------------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------------------------
async def test_expire_due(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        for uid in (1, 2, 3, 4):
            await add_user(s, uid)
        await add_sub(s, 1, expires_at=NOW - days(1))
        await add_sub(s, 2, expires_at=NOW)  # exactly now: expired
        await add_sub(s, 3, expires_at=NOW + timedelta(seconds=1))
        await add_sub(s, 4, expires_at=NOW - days(1), status=SubscriptionStatus.DISABLED)

    assert sorted(await subs.expire_due(session_factory, NOW)) == [1, 2]
    assert await subs.expire_due(session_factory, NOW) == []

    for uid, status, revision in ((1, "expired", 2), (2, "expired", 2), (3, "active", 1), (4, "disabled", 1)):
        sub = await _sub_of(session_factory, uid)
        assert (sub.status.value, sub.revision) == (status, revision)
    assert len(await _events(session_factory, "subscription.expired")) == 2


# ---------------------------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------------------------
def _reminder_sub(expires_in: timedelta, last: int | None = None) -> Subscription:
    return Subscription(expires_at=NOW + expires_in, last_reminder_days=last)


@pytest.mark.parametrize(
    ("expires_in", "last", "expected"),
    [
        (days(5), None, None),  # outside every threshold
        (days(3), None, 3),
        (days(2.5), None, 3),
        (days(2.5), 3, None),  # already reminded for this bucket
        (days(1.5), 3, None),  # 2 days left still belongs to the 3-day bucket
        (days(0.5), 3, 1),
        (days(0.5), 1, None),
        (days(0.5), None, 1),  # missed the 3-day reminder: only the closest one fires
        (timedelta(seconds=-1), 1, 0),
        (timedelta(0), None, 0),
        (-days(1), 0, None),
    ],
)
def test_reminder_for(expires_in: timedelta, last: int | None, expected: int | None) -> None:
    assert subs.reminder_for(_reminder_sub(expires_in, last), [1, 3], NOW) == expected


def test_reminder_for_with_missed_middle_bucket() -> None:
    thresholds = [1, 3, 7]
    assert subs.reminder_for(_reminder_sub(days(5)), thresholds, NOW) == 5
    assert subs.reminder_for(_reminder_sub(days(4), last=5), thresholds, NOW) is None
    assert subs.reminder_for(_reminder_sub(days(3), last=5), thresholds, NOW) == 3


async def test_reminders_flow(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        await add_sub(s, 1, expires_at=NOW + days(3))

    async def due_at(when_offset: timedelta) -> list[tuple[int, int]]:
        async with session_factory.begin() as s:
            found = await subs.reminders_due(s, settings, NOW + when_offset)
            for sub, days_left in found:
                assert sub.user.tg_id == 1  # user is eager-loaded
                await subs.mark_reminded(s, sub, days_left)
            return [(sub.user_id, d) for sub, d in found]

    assert await due_at(-days(1)) == []  # 4 days left
    assert await due_at(timedelta(0)) == [(1, 3)]
    assert await due_at(timedelta(hours=1)) == []
    assert await due_at(days(2.5)) == [(1, 1)]
    assert await due_at(days(2.6)) == []
    assert await due_at(days(3)) == [(1, 0)]
    assert await due_at(days(4)) == []

    # A purchase resets the cycle.
    async with session_factory.begin() as s:
        user = await s.get(User, 1)
        assert user is not None
        await subs.grant_days(s, settings, user, 3, kind=PurchaseKind.PROMO, now=NOW + days(4))
    assert await due_at(days(4)) == [(1, 3)]


async def test_reminders_skip_blocked_banned_disabled_and_stale(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1, bot_blocked=True)
        await add_user(s, 2, is_banned=True)
        await add_user(s, 3)
        await add_user(s, 4)
        await add_user(s, 5)
        await add_sub(s, 1, expires_at=NOW + days(1))
        await add_sub(s, 2, expires_at=NOW + days(1))
        await add_sub(s, 3, expires_at=NOW + days(1), status=SubscriptionStatus.DISABLED)
        await add_sub(s, 4, expires_at=NOW - days(10), status=SubscriptionStatus.EXPIRED)  # too old for a notice
        await add_sub(s, 5, expires_at=NOW - days(1), status=SubscriptionStatus.EXPIRED)
    async with session_factory() as s:
        due = await subs.reminders_due(s, settings, NOW)
    assert [(sub.user_id, d) for sub, d in due] == [(5, 0)]


async def test_expired_notice_is_sent_even_without_pre_expiry_thresholds(session_factory: SessionFactory) -> None:
    settings = make_settings(expiry_reminder_days="")
    async with session_factory.begin() as s:
        await add_user(s, 1)
        await add_user(s, 2)
        await add_sub(s, 1, expires_at=NOW + days(1))
        await add_sub(s, 2, expires_at=NOW - timedelta(hours=1))
    async with session_factory() as s:
        due = await subs.reminders_due(s, settings, NOW)
    assert [(sub.user_id, d) for sub, d in due] == [(2, 0)]


async def test_mark_reminded_from_a_later_transaction_is_persisted(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        await add_sub(s, 1, expires_at=NOW + days(3))
    async with session_factory() as s:
        ((sub, days_left),) = await subs.reminders_due(s, settings, NOW)
    # ... the message is sent with no transaction open, then recorded in a short one:
    async with session_factory.begin() as s:
        assert await subs.mark_reminded(s, sub, days_left) is True
    async with session_factory() as s:
        assert await subs.reminders_due(s, settings, NOW) == []


async def test_mark_reminded_does_not_undo_a_renewal(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        await add_sub(s, 1, expires_at=NOW + days(3))
    async with session_factory() as s:
        ((sub, days_left),) = await subs.reminders_due(s, settings, NOW)
    # The user renews before the reminder is recorded: the new period starts with no reminder sent.
    async with session_factory.begin() as s:
        user = await s.get(User, 1)
        assert user is not None
        await subs.grant_days(s, settings, user, 30, kind=PurchaseKind.PROMO, now=NOW)
    async with session_factory.begin() as s:
        assert await subs.mark_reminded(s, sub, days_left) is False
    async with session_factory() as s:
        stored = await s.get(Subscription, sub.id)
        assert stored is not None and stored.last_reminder_days is None
        assert await subs.reminders_due(s, settings, NOW + days(30)) != []


async def test_mark_reminded_never_raises_a_lower_reminder(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        await add_sub(s, 1, expires_at=NOW + days(1))
    async with session_factory() as s:
        ((sub, _),) = await subs.reminders_due(s, settings, NOW)
    async with session_factory.begin() as s:
        assert await subs.mark_reminded(s, sub, 1) is True
    async with session_factory.begin() as s:
        assert await subs.mark_reminded(s, sub, 3) is False  # a late, older reminder
    async with session_factory() as s:
        stored = await s.get(Subscription, sub.id)
        assert stored is not None and stored.last_reminder_days == 1


async def test_send_reminders_sends_outside_transactions(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        for uid in (1, 2, 3):
            await add_user(s, uid)
        await add_sub(s, 1, expires_at=NOW + days(3))
        await add_sub(s, 2, expires_at=NOW + days(1))
        await add_sub(s, 3, expires_at=NOW - days(1), status=SubscriptionStatus.EXPIRED)
    sent: list[tuple[int, int]] = []

    async def send(sub: Subscription, days_left: int) -> bool:
        # The write lock is free while sending: another writer gets through at once.
        async with session_factory.begin() as s:
            await s.execute(select(func.count()).select_from(Subscription))
            target = await s.get(User, sub.user.tg_id)
            assert target is not None
            target.language = "en"
        if sub.user_id == 2:
            return False  # not delivered: try again next run
        if sub.user_id == 3:
            raise RuntimeError("telegram is down")
        sent.append((sub.user_id, days_left))
        return True

    assert await subs.send_reminders(session_factory, settings, send, NOW) == 1
    assert sent == [(1, 3)]
    async with session_factory() as s:
        due = await subs.reminders_due(s, settings, NOW)
    assert sorted((sub.user_id, d) for sub, d in due) == [(2, 1), (3, 0)]


async def test_mark_reminded_validates() -> None:
    with pytest.raises(ValueError):
        await subs.mark_reminded(None, Subscription(), -1)  # type: ignore[arg-type]
