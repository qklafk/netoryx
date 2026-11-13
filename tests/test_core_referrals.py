from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import (
    BalanceTransaction,
    Event,
    LedgerAccount,
    Payment,
    PaymentStatus,
    Plan,
    Purchase,
    PurchaseKind,
    ReferralReward,
    TxKind,
    User,
)
from app.db.session import SessionFactory
from app.services import referrals, subscriptions, users, wallet
from app.services.payments import service as payment_service
from app.services.payments.base import ProviderStatus
from app.services.referrals import ReferralOutcome, ReferralResult
from app.utils.time import utcnow
from tests.conftest import make_settings

INVITER = 100
INVITEE = 200


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "referral_invitee_bonus_rub": 50,
        "referral_inviter_bonus_rub": 30,
        "referral_inviter_requires_payment": False,
        "referral_invitee_requires_payment": False,
        "referral_inviter_percent": 0,
    }
    base.update(overrides)
    return make_settings(**base)


async def _register(session_factory: SessionFactory, settings: Settings, tg_id: int) -> tuple[User, bool]:
    async with session_factory.begin() as s:
        return await users.get_or_create(s, settings, tg_id, f"user{tg_id}", "N", "ru")


async def _join(
    session_factory: SessionFactory, settings: Settings, tg_id: int, code: str | None, *, is_new: bool | None = None
) -> ReferralResult:
    async with session_factory.begin() as s:
        user, created = await users.get_or_create(s, settings, tg_id, None, "N", "ru")
        return await referrals.apply_on_registration(
            s, settings, user, code, is_new=created if is_new is None else is_new
        )


async def _inviter_code(session_factory: SessionFactory, settings: Settings) -> str:
    inviter, _ = await _register(session_factory, settings, INVITER)
    return inviter.referral_code


async def _balances(session_factory: SessionFactory) -> dict[int, int]:
    async with session_factory() as s:
        return {u.tg_id: u.balance_kop for u in (await s.scalars(select(User))).all()}


async def test_applied_with_immediate_inviter_bonus(session_factory: SessionFactory) -> None:
    settings = _settings()
    code = await _inviter_code(session_factory, settings)
    result = await _join(session_factory, settings, INVITEE, code.lower())
    assert result == ReferralResult(ReferralOutcome.APPLIED, INVITER, 5_000, 3_000)
    assert await _balances(session_factory) == {INVITER: 3_000, INVITEE: 5_000}
    async with session_factory() as s:
        invitee = await users.get(s, INVITEE)
        assert invitee is not None and invitee.referred_by == INVITER
        assert await referrals.stats(s, INVITER) == (1, 3_000)
        event = await s.scalar(select(Event).where(Event.type == "referral.applied"))
        assert event is not None and event.user_id == INVITEE


async def test_deep_link_prefix_is_accepted(session_factory: SessionFactory) -> None:
    settings = _settings()
    code = await _inviter_code(session_factory, settings)
    result = await _join(session_factory, settings, INVITEE, f"ref_{code}")
    assert result.outcome is ReferralOutcome.APPLIED


async def test_inviter_bonus_deferred_until_first_payment(session_factory: SessionFactory) -> None:
    settings = _settings(referral_inviter_requires_payment=True, referral_levels_percent=[])
    code = await _inviter_code(session_factory, settings)
    result = await _join(session_factory, settings, INVITEE, code)
    assert result == ReferralResult(ReferralOutcome.APPLIED, INVITER, 5_000, 0)
    assert (await _balances(session_factory))[INVITER] == 0

    async with session_factory.begin() as s:
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "pay-1", 15_000) == (INVITER, 3_000)
    async with session_factory.begin() as s:  # second payment: fixed bonus is not repeated
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "pay-2", 15_000) is None
    assert (await _balances(session_factory))[INVITER] == 3_000


async def test_percent_bonus_per_payment_and_idempotent(session_factory: SessionFactory) -> None:
    settings = _settings(
        referral_inviter_requires_payment=True, referral_inviter_percent=10, referral_levels_percent=[]
    )
    code = await _inviter_code(session_factory, settings)
    await _join(session_factory, settings, INVITEE, code)
    async with session_factory.begin() as s:
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "p1", 15_005) == (INVITER, 3_000 + 1_500)
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "p1", 15_005) is None  # replay
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "p2", 9) is None  # 10% of 9 kop rounds to 0
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "p3", 10_000) == (INVITER, 1_000)
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "p4", 0) is None
    async with session_factory() as s:
        refs = sorted(
            (await s.scalars(select(BalanceTransaction.ref).where(BalanceTransaction.user_id == INVITER))).all()
        )
        assert refs == [f"fixed:{INVITEE}", "pct:p1", "pct:p3"]
        assert await referrals.stats(s, INVITER) == (1, 5_500)


async def test_payment_of_user_without_inviter_grants_nothing(session_factory: SessionFactory) -> None:
    settings = _settings(referral_inviter_requires_payment=True, referral_inviter_percent=10)
    await _register(session_factory, settings, INVITEE)
    async with session_factory.begin() as s:
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "p1", 10_000) is None
        assert await referrals.on_invitee_payment(s, settings, 999, "p1", 10_000) is None  # unknown user


async def test_payment_bonus_skipped_when_program_disabled_or_inviter_banned(session_factory: SessionFactory) -> None:
    settings = _settings(referral_inviter_requires_payment=True, referral_inviter_percent=10)
    code = await _inviter_code(session_factory, settings)
    await _join(session_factory, settings, INVITEE, code)
    async with session_factory.begin() as s:
        off = _settings(referral_enabled=False, referral_inviter_requires_payment=True)
        assert await referrals.on_invitee_payment(s, off, INVITEE, "p1", 10_000) is None
        inviter = await users.get(s, INVITER)
        assert inviter is not None
        inviter.is_banned = True
    async with session_factory.begin() as s:
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "p1", 10_000) is None
    assert (await _balances(session_factory))[INVITER] == 0


@pytest.mark.parametrize(
    ("code", "outcome"),
    [(None, ReferralOutcome.NOT_FOUND), ("", ReferralOutcome.NOT_FOUND), ("NOSUCHCODE", ReferralOutcome.NOT_FOUND)],
)
async def test_missing_or_unknown_code(
    session_factory: SessionFactory, code: str | None, outcome: ReferralOutcome
) -> None:
    settings = _settings()
    await _inviter_code(session_factory, settings)
    result = await _join(session_factory, settings, INVITEE, code)
    assert result == ReferralResult(outcome)
    assert await _balances(session_factory) == {INVITER: 0, INVITEE: 0}


async def test_disabled_program(session_factory: SessionFactory) -> None:
    settings = _settings(referral_enabled=False)
    code = await _inviter_code(session_factory, settings)
    assert (await _join(session_factory, settings, INVITEE, code)).outcome is ReferralOutcome.DISABLED
    assert await _balances(session_factory) == {INVITER: 0, INVITEE: 0}


async def test_self_referral_is_rejected(session_factory: SessionFactory) -> None:
    settings = _settings()
    async with session_factory.begin() as s:
        user, created = await users.get_or_create(s, settings, INVITEE, None, "N", "ru")
        result = await referrals.apply_on_registration(s, settings, user, user.referral_code, is_new=created)
    assert result.outcome is ReferralOutcome.SELF
    assert await _balances(session_factory) == {INVITEE: 0}


async def test_existing_user_cannot_claim_a_code(session_factory: SessionFactory) -> None:
    settings = _settings()
    code = await _inviter_code(session_factory, settings)
    await _register(session_factory, settings, INVITEE)  # registered earlier without a code
    result = await _join(session_factory, settings, INVITEE, code)
    assert result.outcome is ReferralOutcome.NOT_NEW
    assert await _balances(session_factory) == {INVITER: 0, INVITEE: 0}


async def test_code_applies_only_once(session_factory: SessionFactory) -> None:
    settings = _settings()
    code = await _inviter_code(session_factory, settings)
    other, _ = await _register(session_factory, settings, 300)
    assert (await _join(session_factory, settings, INVITEE, code)).outcome is ReferralOutcome.APPLIED
    # even if a buggy caller passes is_new=True again, referred_by is already set
    again = await _join(session_factory, settings, INVITEE, other.referral_code, is_new=True)
    assert again.outcome is ReferralOutcome.NOT_NEW
    assert await _balances(session_factory) == {INVITER: 3_000, INVITEE: 5_000, 300: 0}


async def test_banned_inviter_code_is_not_found(session_factory: SessionFactory) -> None:
    settings = _settings()
    code = await _inviter_code(session_factory, settings)
    async with session_factory.begin() as s:
        inviter = await users.get(s, INVITER)
        assert inviter is not None
        inviter.is_banned = True
    assert (await _join(session_factory, settings, INVITEE, code)).outcome is ReferralOutcome.NOT_FOUND


async def test_zero_bonuses_still_link(session_factory: SessionFactory) -> None:
    settings = _settings(referral_invitee_bonus_rub=0, referral_inviter_bonus_rub=0)
    code = await _inviter_code(session_factory, settings)
    result = await _join(session_factory, settings, INVITEE, code)
    assert result == ReferralResult(ReferralOutcome.APPLIED, INVITER, 0, 0)
    async with session_factory() as s:
        assert await referrals.stats(s, INVITER) == (1, 0)
        assert (await s.scalars(select(BalanceTransaction))).all() == []


async def test_double_tap_grants_once(session_factory: SessionFactory) -> None:
    """Two concurrent /start handlers for the same user: each sees itself as the creator at most once."""
    settings = _settings()
    code = await _inviter_code(session_factory, settings)
    results = await asyncio.gather(*(_join(session_factory, settings, INVITEE, code) for _ in range(8)))
    assert [r.outcome for r in results].count(ReferralOutcome.APPLIED) == 1
    assert await _balances(session_factory) == {INVITER: 3_000, INVITEE: 5_000}
    async with session_factory() as s:
        assert len((await s.scalars(select(BalanceTransaction))).all()) == 2


async def test_top_referrers(session_factory: SessionFactory) -> None:
    settings = _settings(referral_invitee_bonus_rub=0, referral_inviter_bonus_rub=0)
    a, _ = await _register(session_factory, settings, 1)
    b, _ = await _register(session_factory, settings, 2)
    await _register(session_factory, settings, 3)  # invites nobody
    for tg_id in (10, 11, 12):
        await _join(session_factory, settings, tg_id, a.referral_code)
    for tg_id in (20, 21):
        await _join(session_factory, settings, tg_id, b.referral_code)
    async with session_factory() as s:
        top = await referrals.top(s)
        assert [(u.tg_id, n) for u, n in top] == [(1, 3), (2, 2)]
        assert [(u.tg_id, n) for u, n in await referrals.top(s, limit=1)] == [(1, 3)]
        assert await referrals.stats(s, 3) == (0, 0)


def test_normalize_code() -> None:
    assert referrals.normalize_code(" ref_abcd ") == "ABCD"
    assert referrals.normalize_code("R_ABCD") == "ABCD"
    assert referrals.normalize_code("abcd") == "ABCD"
    assert referrals.normalize_code(None) == ""


# ---------------------------------------------------------------------------------------------
# REFERRAL_REWARD_MODE / REFERRAL_LEVELS_PERCENT / *_BONUS_DAYS
# ---------------------------------------------------------------------------------------------
LEVEL2 = 50  # invited INVITER


async def _code_of(session_factory: SessionFactory, tg_id: int) -> str:
    async with session_factory() as s:
        user = await users.get(s, tg_id)
        assert user is not None
        return user.referral_code


async def _chain(session_factory: SessionFactory, settings: Settings) -> None:
    """LEVEL2 -> INVITER -> INVITEE (each one invited the next)."""
    await _register(session_factory, settings, LEVEL2)
    joined = await _join(session_factory, settings, INVITER, await _code_of(session_factory, LEVEL2))
    assert joined.outcome is ReferralOutcome.APPLIED
    joined = await _join(session_factory, settings, INVITEE, await _code_of(session_factory, INVITER))
    assert joined.outcome is ReferralOutcome.APPLIED


async def _plan(session_factory: SessionFactory, price_kop: int = 50_000, days: int = 30) -> Plan:
    async with session_factory.begin() as s:
        plan = Plan(title="plan", duration_days=days, price_kop=price_kop, device_limit=3)
        s.add(plan)
    return plan


async def _buy(session_factory: SessionFactory, settings: Settings, user_id: int, plan: Plan) -> Purchase:
    async with session_factory.begin() as s:
        await wallet.apply(s, user_id, plan.price_kop, TxKind.ADMIN_ADJUST, f"topup:{user_id}:{plan.id}")
        user = await users.get(s, user_id)
        assert user is not None
        return await subscriptions.purchase_plan(s, settings, user, plan)


async def _rewards(session_factory: SessionFactory) -> list[tuple[int, int, str, int, int]]:
    async with session_factory() as s:
        rows = (await s.scalars(select(ReferralReward).order_by(ReferralReward.id))).all()
    return [(r.beneficiary_id, r.level, r.source_ref, r.amount_kop, r.days) for r in rows]


async def _money(session_factory: SessionFactory) -> dict[int, tuple[int, int]]:
    async with session_factory() as s:
        return {u.tg_id: (u.balance_kop, u.referral_balance_kop) for u in (await s.scalars(select(User))).all()}


def _days_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "referral_reward_mode": "days",
        "referral_levels_percent": [20, 10],
        "referral_invitee_bonus_days": 3,
        "referral_inviter_bonus_days": 7,
        "referral_inviter_requires_payment": False,
    }
    base.update(overrides)
    return _settings(**base)


async def test_levels_percent_paid_on_payment_not_again_on_purchase(session_factory: SessionFactory) -> None:
    settings = _settings(referral_invitee_bonus_rub=0, referral_inviter_bonus_rub=0, referral_levels_percent=[20, 10])
    await _chain(session_factory, settings)
    async with session_factory.begin() as s:
        await referrals.on_invitee_payment(s, settings, INVITEE, "pay-1", 50_000)
    await _buy(session_factory, settings, INVITEE, await _plan(session_factory))  # spends the paid money
    money = await _money(session_factory)
    assert money[INVITER] == (10_000, 0) and money[LEVEL2] == (5_000, 0)
    assert await _rewards(session_factory) == [
        (INVITER, 1, "payment:pay-1", 10_000, 0),
        (LEVEL2, 2, "payment:pay-1", 5_000, 0),
    ]
    async with session_factory() as s:
        tx = await s.scalar(select(BalanceTransaction).where(BalanceTransaction.user_id == INVITER))
        assert tx is not None and (tx.kind, tx.account) == (TxKind.REFERRAL_COMMISSION, LedgerAccount.MAIN)
        assert await referrals.stats(s, INVITER) == (1, 10_000)


async def test_default_levels_apply_without_legacy_percent(session_factory: SessionFactory) -> None:
    """Defaults: REFERRAL_LEVELS_PERCENT=10, legacy REFERRAL_INVITER_PERCENT=0 -> 10% of the credited top-up."""
    settings = _settings(referral_invitee_bonus_rub=0, referral_inviter_bonus_rub=0)
    assert settings.referral_levels_percent == [10] and settings.referral_reward_mode == "balance"
    await _chain(session_factory, settings)
    async with session_factory.begin() as s:  # a credited top-up earns the commission
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "topup-1", 50_000) == (INVITER, 5_000)
    await _buy(session_factory, settings, INVITEE, await _plan(session_factory))  # spending it earns nothing more
    money = await _money(session_factory)
    assert money[INVITER] == (5_000, 0) and money[LEVEL2] == (0, 0)  # one level only


async def test_days_mode_bonus_days_and_level_days(session_factory: SessionFactory) -> None:
    settings = _days_settings()
    await _register(session_factory, settings, INVITER)
    result = await _join(session_factory, settings, INVITEE, await _code_of(session_factory, INVITER))
    assert result == ReferralResult(ReferralOutcome.APPLIED, INVITER, 0, 0, 3, 7)
    assert await _money(session_factory) == {INVITER: (0, 0), INVITEE: (0, 0)}  # no rubles in days mode

    purchase = await _buy(session_factory, settings, INVITEE, await _plan(session_factory, days=30))
    assert (await _money(session_factory))[INVITER] == (0, 0)
    assert await _rewards(session_factory) == [
        (INVITEE, 0, f"bonus:invitee:{INVITEE}", 0, 3),
        (INVITER, 0, f"bonus:inviter:{INVITEE}", 0, 7),
        (INVITER, 1, f"purchase:{purchase.id}", 0, 6),  # 20% of 30 days
    ]
    async with session_factory() as s:
        granted = (
            await s.execute(
                select(Purchase.user_id, Purchase.days)
                .where(Purchase.kind == PurchaseKind.REFERRAL_REWARD)
                .order_by(Purchase.id)
            )
        ).all()
        assert [tuple(row) for row in granted] == [(INVITEE, 3), (INVITER, 7), (INVITER, 6)]
        sub = await subscriptions.get_for_user(s, INVITER)
        assert sub is not None and sub.expires_at > utcnow() + timedelta(days=12)


async def test_days_mode_deferred_inviter_bonus_on_first_payment(session_factory: SessionFactory) -> None:
    settings = _days_settings(referral_inviter_requires_payment=True)
    code = await _inviter_code(session_factory, settings)
    assert (await _join(session_factory, settings, INVITEE, code)).inviter_bonus_days == 0
    async with session_factory.begin() as s:
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "p1", 10_000) == (INVITER, 0)
    async with session_factory.begin() as s:
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "p2", 10_000) is None
    assert [r for r in await _rewards(session_factory) if r[0] == INVITER] == [
        (INVITER, 0, f"bonus:inviter:{INVITEE}", 0, 7)
    ]
    assert (await _money(session_factory))[INVITER] == (0, 0)


async def test_no_level_rewards_for_free_days_and_replays(session_factory: SessionFactory) -> None:
    settings = _days_settings(referral_invitee_bonus_days=0, referral_inviter_bonus_days=0)
    await _chain(session_factory, settings)
    async with session_factory.begin() as s:
        invitee = await users.get(s, INVITEE)
        assert invitee is not None
        promo_days = await subscriptions.grant_days(s, settings, invitee, 30, kind=PurchaseKind.PROMO)
        assert await referrals.on_invitee_purchase(s, settings, promo_days) == []
    purchase = await _buy(session_factory, settings, INVITEE, await _plan(session_factory))
    async with session_factory.begin() as s:  # replay of the same purchase grants nothing more
        again = await s.get(Purchase, purchase.id)
        assert again is not None
        assert await referrals.on_invitee_purchase(s, settings, again) == []
        assert await referrals.on_invitee_purchase(s, _settings(referral_levels_percent=[]), again) == []
    assert len(await _rewards(session_factory)) == 2  # levels 1 and 2 of the one paid purchase


async def test_banned_level_is_skipped_but_chain_continues(session_factory: SessionFactory) -> None:
    settings = _settings(referral_invitee_bonus_rub=0, referral_inviter_bonus_rub=0, referral_levels_percent=[20, 10])
    await _chain(session_factory, settings)
    async with session_factory.begin() as s:
        inviter = await users.get(s, INVITER)
        assert inviter is not None
        inviter.is_banned = True
    async with session_factory.begin() as s:
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "pay-1", 50_000) is None  # inviter banned
    money = await _money(session_factory)
    assert money[INVITER] == (0, 0) and money[LEVEL2] == (5_000, 0)


# ---------------------------------------------------------------------------------------------
# Regressions: deferred bonus vs. partial payments, commissions on real payments, free plans
# ---------------------------------------------------------------------------------------------
async def test_deferred_inviter_bonus_needs_a_full_payment_of_at_least_min_topup(
    session_factory: SessionFactory,
) -> None:
    settings = _settings(referral_inviter_requires_payment=True, referral_levels_percent=[])
    code = await _inviter_code(session_factory, settings)
    await _join(session_factory, settings, INVITEE, code)
    async with session_factory.begin() as s:  # underpaid YooMoney invoice: 2 RUB of 50 RUB
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "p1", 200, expected_kop=5_000) is None
        assert settings.min_topup_kop == 5_000  # a full payment below MIN_TOPUP does not unlock it either
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "p2", 4_999) is None
    assert (await _balances(session_factory))[INVITER] == 0
    assert [r for r in await _rewards(session_factory) if r[0] == INVITER] == []
    async with session_factory.begin() as s:  # the bonus is not burnt: a later full payment unlocks it
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "p3", 5_000, expected_kop=5_000) == (
            INVITER,
            3_000,
        )
    assert (await _balances(session_factory))[INVITER] == 3_000


async def test_invitee_money_bonus_waits_for_their_first_full_payment(session_factory: SessionFactory) -> None:
    # by default new accounts get nothing spendable at sign-up: farming the bonus with throwaway accounts is pointless
    settings = _settings(referral_invitee_requires_payment=True, referral_levels_percent=[])
    code = await _inviter_code(session_factory, settings)
    result = await _join(session_factory, settings, INVITEE, code)
    assert result.outcome == ReferralOutcome.APPLIED and result.invitee_bonus_kop == 0
    assert (await _balances(session_factory))[INVITEE] == 0
    async with session_factory.begin() as s:  # underpaid, then below MIN_TOPUP: still locked
        await referrals.on_invitee_payment(s, settings, INVITEE, "p1", 200, expected_kop=5_000)
        await referrals.on_invitee_payment(s, settings, INVITEE, "p2", 4_999)
    assert (await _balances(session_factory))[INVITEE] == 0
    async with session_factory.begin() as s:
        await referrals.on_invitee_payment(s, settings, INVITEE, "p3", 5_000, expected_kop=5_000)
    async with session_factory.begin() as s:  # once only
        await referrals.on_invitee_payment(s, settings, INVITEE, "p4", 5_000, expected_kop=5_000)
    assert (await _balances(session_factory))[INVITEE] == 5_000


async def test_underpaid_provider_payment_does_not_unlock_inviter_bonus(session_factory: SessionFactory) -> None:

    settings = make_settings()  # defaults: requires_payment, 50 RUB inviter bonus, MIN_TOPUP 50 RUB
    assert settings.referral_inviter_requires_payment
    code = await _inviter_code(session_factory, settings)
    await _join(session_factory, settings, INVITEE, code)
    async with session_factory.begin() as s:
        s.add(Payment(id="ym-1", provider="yoomoney", user_id=INVITEE, amount_kop=5_000, status=PaymentStatus.PENDING))
    async with session_factory() as s:
        payment = await s.get(Payment, "ym-1")
    assert payment is not None
    result = await payment_service.apply_provider_status(
        session_factory, settings, payment, ProviderStatus(PaymentStatus.SUCCEEDED, 200, "op-1")
    )
    assert result is not None and result.amount_kop == 200 and result.is_partial
    async with session_factory() as s:
        fixed = await s.scalar(select(BalanceTransaction).where(BalanceTransaction.ref == f"fixed:{INVITEE}"))
        assert fixed is None
        inviter = await users.get(s, INVITER)
        assert inviter is not None and inviter.balance_kop == 20  # only the 10% level commission on 2 RUB


async def test_level_commissions_are_paid_on_credited_payments(session_factory: SessionFactory) -> None:
    settings = _settings(referral_invitee_bonus_rub=0, referral_inviter_bonus_rub=0, referral_levels_percent=[20, 10])
    await _chain(session_factory, settings)
    async with session_factory.begin() as s:
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "pay-1", 50_000) == (INVITER, 10_000)
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "pay-1", 50_000) is None  # replay
    money = await _money(session_factory)
    assert money[INVITER] == (10_000, 0) and money[LEVEL2] == (5_000, 0)
    assert await _rewards(session_factory) == [
        (INVITER, 1, "payment:pay-1", 10_000, 0),
        (LEVEL2, 2, "payment:pay-1", 5_000, 0),
    ]
    async with session_factory() as s:
        refs = (await s.scalars(select(BalanceTransaction.ref).where(BalanceTransaction.user_id == INVITER))).all()
        assert refs == ["L1:pay-1"]


async def test_partner_commission_on_payment_goes_to_referral_account(session_factory: SessionFactory) -> None:
    settings = _settings(referral_invitee_bonus_rub=0, referral_inviter_bonus_rub=0, referral_reward_mode="partner")
    await _chain(session_factory, settings)
    async with session_factory.begin() as s:
        assert await referrals.on_invitee_payment(s, settings, INVITEE, "pay-1", 50_000) == (INVITER, 5_000)
    assert (await _money(session_factory))[INVITER] == (0, 5_000)
    async with session_factory() as s:
        tx = await s.scalar(select(BalanceTransaction).where(BalanceTransaction.user_id == INVITER))
        assert tx is not None and (tx.kind, tx.account) == (TxKind.REFERRAL_COMMISSION, LedgerAccount.REFERRAL)


@pytest.mark.parametrize("mode", ["balance", "partner"])
async def test_wallet_funded_purchase_earns_no_commission(session_factory: SessionFactory, mode: str) -> None:
    """Bonus / promo / admin money spent on a plan is not a real payment: no commission (no farming)."""
    settings = _settings(referral_inviter_bonus_rub=0, referral_reward_mode=mode)
    await _chain(session_factory, settings)
    async with session_factory.begin() as s:
        await wallet.apply(s, INVITEE, 10_000, TxKind.PROMO, "promo:1")
    await _buy(session_factory, settings, INVITEE, await _plan(session_factory, price_kop=15_000))
    assert (await _money(session_factory))[INVITER] == (5_000, 0)  # only its own invitee bonus from LEVEL2
    assert [r for r in await _rewards(session_factory) if r[1] > 0] == []


async def test_days_mode_free_plan_purchases_earn_no_level_days(session_factory: SessionFactory) -> None:
    settings = _days_settings(referral_levels_percent=[20])
    await _register(session_factory, settings, INVITER)
    await _join(session_factory, settings, INVITEE, await _code_of(session_factory, INVITER))
    free = await _plan(session_factory, price_kop=0, days=7)
    for _ in range(3):
        purchase = await _buy(session_factory, settings, INVITEE, free)
        async with session_factory.begin() as s:
            assert await referrals.on_invitee_purchase(s, settings, purchase) == []
    assert [r for r in await _rewards(session_factory) if r[1] > 0] == []


async def test_inviter_bonus_given_as_days_is_not_paid_again_as_money(session_factory: SessionFactory) -> None:
    days = _days_settings(referral_inviter_requires_payment=True, referral_levels_percent=[])
    code = await _inviter_code(session_factory, days)
    await _join(session_factory, days, INVITEE, code)
    async with session_factory.begin() as s:
        assert await referrals.on_invitee_payment(s, days, INVITEE, "p1", 10_000) == (INVITER, 0)
    money = _settings(referral_inviter_requires_payment=True, referral_levels_percent=[])
    async with session_factory.begin() as s:
        assert await referrals.on_invitee_payment(s, money, INVITEE, "p2", 10_000) is None
        inviter = await s.get(User, INVITER)
        assert inviter is not None and inviter.balance_kop == 0
    assert (await _money(session_factory))[INVITER] == (0, 0)
    async with session_factory() as s:
        assert (await s.scalars(select(BalanceTransaction).where(BalanceTransaction.user_id == INVITER))).all() == []
    assert [r for r in await _rewards(session_factory) if r[0] == INVITER] == [
        (INVITER, 0, f"bonus:inviter:{INVITEE}", 0, 7)
    ]


async def test_commission_is_not_paid_twice_after_switching_to_partner_mode(session_factory: SessionFactory) -> None:
    balance = _settings(referral_invitee_bonus_rub=0, referral_inviter_bonus_rub=0)
    await _chain(session_factory, balance)
    async with session_factory.begin() as s:
        assert await referrals.on_invitee_payment(s, balance, INVITEE, "pay-1", 50_000) == (INVITER, 5_000)
    partner = _settings(referral_invitee_bonus_rub=0, referral_inviter_bonus_rub=0, referral_reward_mode="partner")
    async with session_factory.begin() as s:  # a replay of the same payment after the mode switch
        assert await referrals.on_invitee_payment(s, partner, INVITEE, "pay-1", 50_000) is None
    assert (await _money(session_factory))[INVITER] == (5_000, 0)
