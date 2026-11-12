"""Partner balance: transfers, payout requests, statistics and anti-fraud flags."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import (
    AdminAuditLog,
    BalanceTransaction,
    LedgerAccount,
    Payment,
    PaymentStatus,
    ReferralReward,
    TxKind,
    User,
    WithdrawalStatus,
)
from app.db.session import SessionFactory
from app.services import partner, wallet
from app.services.partner import FraudFlagCode, PartnerError, PartnerErrorCode
from tests.conftest import make_settings
from tests.test_subs_support import NOW, add_user

PARTNER = 100
ADMIN = 1000


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "referral_reward_mode": "partner",
        "withdrawals_enabled": True,
        "min_withdrawal_rub": 500,
        "withdrawal_methods": ["card", "sbp"],
        "referral_levels_percent": [20, 10, 5],
    }
    base.update(overrides)
    return make_settings(**base)


async def _partner(session_factory: SessionFactory, referral_kop: int = 100_000) -> None:
    async with session_factory.begin() as s:
        await add_user(s, PARTNER, created_at=NOW - timedelta(days=100))
        if referral_kop:
            await wallet.apply(
                s, PARTNER, referral_kop, TxKind.REFERRAL_COMMISSION, "L1:seed", account=LedgerAccount.REFERRAL
            )


async def _balances(session_factory: SessionFactory, user_id: int = PARTNER) -> tuple[int, int]:
    async with session_factory() as s:
        user = await s.get(User, user_id)
        assert user is not None
        return user.balance_kop, user.referral_balance_kop


# ---------------------------------------------------------------------------------------------
# transfers
# ---------------------------------------------------------------------------------------------
async def test_transfer_moves_money_with_two_ledger_rows(session_factory: SessionFactory) -> None:
    await _partner(session_factory)
    async with session_factory.begin() as s:
        result = await partner.transfer_to_main(s, PARTNER, 30_000, ref="t1")
    assert result.applied and result.ref == "t1"
    assert await _balances(session_factory) == (30_000, 70_000)
    async with session_factory() as s:
        rows = (await s.scalars(select(BalanceTransaction).where(BalanceTransaction.ref == "t1"))).all()
    assert sorted((r.account, r.amount_kop) for r in rows) == [
        (LedgerAccount.MAIN, 30_000),
        (LedgerAccount.REFERRAL, -30_000),
    ]
    assert {r.kind for r in rows} == {TxKind.REFERRAL_TRANSFER}


async def test_transfer_is_idempotent_per_ref(session_factory: SessionFactory) -> None:
    await _partner(session_factory)
    for _ in range(2):
        async with session_factory.begin() as s:
            result = await partner.transfer_to_main(s, PARTNER, 10_000, ref="same")
    assert not result.applied
    assert await _balances(session_factory) == (10_000, 90_000)


@pytest.mark.parametrize("amount", [0, -5, True])
async def test_transfer_rejects_bad_amounts(session_factory: SessionFactory, amount: Any) -> None:
    await _partner(session_factory)
    async with session_factory.begin() as s:
        with pytest.raises(PartnerError) as exc:
            await partner.transfer_to_main(s, PARTNER, amount)
    assert exc.value.code == PartnerErrorCode.BAD_AMOUNT


async def test_transfer_insufficient_funds_writes_nothing(session_factory: SessionFactory) -> None:
    await _partner(session_factory, referral_kop=5_000)
    async with session_factory.begin() as s:
        with pytest.raises(PartnerError) as exc:
            await partner.transfer_to_main(s, PARTNER, 6_000, ref="x")
        assert exc.value.code == PartnerErrorCode.INSUFFICIENT_FUNDS
        assert not await wallet.has_transaction(s, PARTNER, TxKind.REFERRAL_TRANSFER, "x")
    assert await _balances(session_factory) == (0, 5_000)


async def test_transfer_for_unknown_user(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        with pytest.raises(PartnerError) as exc:
            await partner.transfer_to_main(s, 999, 100)
    assert exc.value.code == PartnerErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------------------------
# withdrawals
# ---------------------------------------------------------------------------------------------
async def test_request_withdrawal_debits_immediately(session_factory: SessionFactory) -> None:
    await _partner(session_factory)
    settings = _settings()
    async with session_factory.begin() as s:
        withdrawal = await partner.request_withdrawal(s, settings, PARTNER, 60_000, "card", "  4111 1111\n1111 1111 ")
    assert withdrawal.status == WithdrawalStatus.PENDING
    assert withdrawal.details == "4111 1111 1111 1111"
    assert await _balances(session_factory) == (0, 40_000)
    async with session_factory() as s:
        assert await wallet.has_transaction(
            s, PARTNER, TxKind.WITHDRAWAL, partner.withdrawal_ref(withdrawal.id), account=LedgerAccount.REFERRAL
        )
        assert (await partner.open_withdrawal(s, PARTNER)) is not None


@pytest.mark.parametrize(
    ("overrides", "amount", "method", "details", "code"),
    [
        ({"withdrawals_enabled": False}, 60_000, "card", "x", PartnerErrorCode.DISABLED),
        ({"referral_reward_mode": "balance"}, 60_000, "card", "x", PartnerErrorCode.DISABLED),
        ({}, 49_999, "card", "x", PartnerErrorCode.BELOW_MINIMUM),
        ({}, 0, "card", "x", PartnerErrorCode.BAD_AMOUNT),
        ({}, 60_000, "paypal", "x", PartnerErrorCode.BAD_METHOD),
        ({}, 60_000, "card", "   ", PartnerErrorCode.BAD_DETAILS),
        ({}, 60_000, "card", "x" * 600, PartnerErrorCode.BAD_DETAILS),
        ({}, 200_000, "card", "x", PartnerErrorCode.INSUFFICIENT_FUNDS),
    ],
)
async def test_request_withdrawal_rules(
    session_factory: SessionFactory,
    overrides: dict[str, Any],
    amount: int,
    method: str,
    details: str,
    code: PartnerErrorCode,
) -> None:
    await _partner(session_factory)
    async with session_factory.begin() as s:
        with pytest.raises(PartnerError) as exc:
            await partner.request_withdrawal(s, _settings(**overrides), PARTNER, amount, method, details)
    assert exc.value.code == code
    assert await _balances(session_factory) == (0, 100_000)
    async with session_factory() as s:
        assert await partner.list_for_user(s, PARTNER) == []


async def test_only_one_open_withdrawal(session_factory: SessionFactory) -> None:
    await _partner(session_factory)
    settings = _settings()
    async with session_factory.begin() as s:
        first = await partner.request_withdrawal(s, settings, PARTNER, 50_000, "sbp", "+7900")
    async with session_factory.begin() as s:
        await partner.approve(s, first.id, ADMIN)
    async with session_factory.begin() as s:
        with pytest.raises(PartnerError) as exc:
            await partner.request_withdrawal(s, settings, PARTNER, 50_000, "sbp", "+7900")
    assert exc.value.code == PartnerErrorCode.PENDING_EXISTS
    async with session_factory.begin() as s:
        await partner.mark_paid(s, first.id, ADMIN, comment="done")
    async with session_factory.begin() as s:
        second = await partner.request_withdrawal(s, settings, PARTNER, 50_000, "sbp", "+7900")
    assert second.id != first.id
    assert await _balances(session_factory) == (0, 0)


async def test_reject_refunds_once(session_factory: SessionFactory) -> None:
    await _partner(session_factory)
    async with session_factory.begin() as s:
        withdrawal = await partner.request_withdrawal(s, _settings(), PARTNER, 70_000, "card", "card")
    async with session_factory.begin() as s:
        rejected = await partner.reject(s, withdrawal.id, ADMIN, comment="wrong details", source="web", ip="1.2.3.4")
    assert rejected.status == WithdrawalStatus.REJECTED
    assert rejected.admin_comment == "wrong details" and rejected.admin_id == ADMIN
    assert await _balances(session_factory) == (0, 100_000)
    async with session_factory.begin() as s:
        with pytest.raises(PartnerError) as exc:
            await partner.reject(s, withdrawal.id, ADMIN)
    assert exc.value.code == PartnerErrorCode.BAD_STATUS
    assert await _balances(session_factory) == (0, 100_000)
    async with session_factory() as s:
        actions = (await s.scalars(select(AdminAuditLog.action))).all()
    assert "withdrawal.reject" in actions


async def test_paid_cannot_be_rejected_and_unknown_is_not_found(session_factory: SessionFactory) -> None:
    await _partner(session_factory)
    async with session_factory.begin() as s:
        withdrawal = await partner.request_withdrawal(s, _settings(), PARTNER, 70_000, "card", "card")
        await partner.mark_paid(s, withdrawal.id, ADMIN)
    async with session_factory.begin() as s:
        with pytest.raises(PartnerError) as exc:
            await partner.reject(s, withdrawal.id, ADMIN)
        assert exc.value.code == PartnerErrorCode.BAD_STATUS
        with pytest.raises(PartnerError) as missing:
            await partner.approve(s, 12345, ADMIN)
        assert missing.value.code == PartnerErrorCode.NOT_FOUND
    assert await _balances(session_factory) == (0, 30_000)


async def test_list_pending_with_user_and_flags(session_factory: SessionFactory) -> None:
    await _partner(session_factory)
    async with session_factory.begin() as s:
        await add_user(s, 200, created_at=NOW - timedelta(days=1))
        await wallet.apply(s, 200, 80_000, TxKind.REFERRAL_COMMISSION, "L1:x", account=LedgerAccount.REFERRAL)
        a = await partner.request_withdrawal(s, _settings(), PARTNER, 50_000, "card", "a")
        b = await partner.request_withdrawal(s, _settings(), 200, 50_000, "card", "b")
        await partner.approve(s, b.id, ADMIN)
    async with session_factory() as s:
        queue = await partner.list_pending(s, now=NOW)
        only_pending = await partner.list_pending(s, statuses=(WithdrawalStatus.PENDING,), with_flags=False)
    assert [item.withdrawal.id for item in queue] == [a.id, b.id]
    assert queue[0].user is not None and queue[0].user.tg_id == PARTNER
    assert queue[0].flags == []
    assert [f.code for f in queue[1].flags] == [FraudFlagCode.YOUNG_ACCOUNT]
    assert [item.withdrawal.id for item in only_pending] == [a.id]


# ---------------------------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------------------------
async def _tree(session_factory: SessionFactory) -> None:
    """PARTNER -> 201, 202 -> (201 ->) 301 -> 401."""
    async with session_factory.begin() as s:
        await add_user(s, PARTNER, created_at=NOW - timedelta(days=100))
        await add_user(s, 201, referred_by=PARTNER)
        await add_user(s, 202, referred_by=PARTNER)
        await add_user(s, 301, referred_by=201)
        await add_user(s, 401, referred_by=301)
        rewards = [
            (201, 1, "payment:a", 2_000, 0),
            (201, 1, "payment:b", 1_000, 0),
            (301, 2, "payment:c", 500, 0),
            (202, 1, "purchase:1", 0, 3),
            (201, 0, "bonus:inviter:201", 5_000, 0),
        ]
        s.add_all(
            ReferralReward(
                beneficiary_id=PARTNER, source_user_id=src, level=level, source_ref=ref, amount_kop=kop, days=d
            )
            for src, level, ref, kop, d in rewards
        )
        await wallet.apply(s, PARTNER, 3_500, TxKind.REFERRAL_COMMISSION, "L1:a", account=LedgerAccount.REFERRAL)


async def test_partner_stats_per_level(session_factory: SessionFactory) -> None:
    await _tree(session_factory)
    async with session_factory() as s:
        stats = await partner.partner_stats(s, _settings(), PARTNER)
    assert [(lv.level, lv.users, lv.paid_users, lv.earned_kop, lv.earned_days, lv.rewards) for lv in stats.levels] == [
        (1, 2, 2, 3_000, 3, 3),
        (2, 1, 1, 500, 0, 1),
        (3, 1, 0, 0, 0, 0),
    ]
    assert (stats.total_users, stats.total_paid_users) == (4, 3)
    assert (stats.bonus_kop, stats.total_earned_kop, stats.total_earned_days) == (5_000, 8_500, 3)
    assert stats.referral_balance_kop == 3_500
    assert (stats.pending_withdrawal_kop, stats.withdrawn_kop) == (0, 0)


async def test_tree_counts_respects_depth(session_factory: SessionFactory) -> None:
    await _tree(session_factory)
    async with session_factory() as s:
        assert await partner.tree_counts(s, PARTNER, 5) == [2, 1, 1, 0, 0]
        assert await partner.tree_counts(s, PARTNER, 0) == []


async def test_stats_include_withdrawal_totals(session_factory: SessionFactory) -> None:
    await _partner(session_factory)
    settings = _settings(referral_levels_percent=[10])
    async with session_factory.begin() as s:
        w = await partner.request_withdrawal(s, settings, PARTNER, 50_000, "card", "x")
        await partner.mark_paid(s, w.id, ADMIN)
        await partner.request_withdrawal(s, settings, PARTNER, 50_000, "card", "x")
    async with session_factory() as s:
        stats = await partner.partner_stats(s, settings, PARTNER)
    assert (stats.pending_withdrawal_kop, stats.withdrawn_kop, stats.referral_balance_kop) == (50_000, 50_000, 0)
    assert [lv.level for lv in stats.levels] == [1]


# ---------------------------------------------------------------------------------------------
# anti-fraud
# ---------------------------------------------------------------------------------------------
async def test_fraud_flags(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await add_user(s, PARTNER, created_at=NOW - timedelta(days=2))
        for n in range(20):
            await add_user(s, 1_000_000 + n, referred_by=PARTNER, created_at=NOW - timedelta(hours=1))
        s.add(
            ReferralReward(
                beneficiary_id=PARTNER, source_user_id=1_000_000, level=1, source_ref="payment:big", amount_kop=50_000
            )
        )
    async with session_factory() as s:
        flags = {f.code: f.detail for f in await partner.fraud_flags(s, PARTNER, settings=settings, now=NOW)}
    assert set(flags) == {
        FraudFlagCode.YOUNG_ACCOUNT,
        FraudFlagCode.LOW_CONVERSION,
        FraudFlagCode.REGISTRATION_BURST,
        FraudFlagCode.CONCENTRATED_EARNINGS,
    }
    assert flags[FraudFlagCode.LOW_CONVERSION] == {"referrals": 20, "paid": 0}
    assert flags[FraudFlagCode.REGISTRATION_BURST]["count"] == 20
    assert flags[FraudFlagCode.CONCENTRATED_EARNINGS]["source_user_id"] == 1_000_000


async def test_no_low_conversion_flag_when_referrals_pay(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        await add_user(s, PARTNER, created_at=NOW - timedelta(days=200))
        for n in range(10):
            uid = 2_000_000 + n
            await add_user(s, uid, referred_by=PARTNER, created_at=NOW - timedelta(days=n * 3))
            s.add(
                Payment(
                    id=f"p{n}",
                    provider="stars",
                    user_id=uid,
                    amount_kop=10_000,
                    status=PaymentStatus.SUCCEEDED,
                    credited_at=NOW,
                )
            )
    async with session_factory() as s:
        assert await partner.fraud_flags(s, PARTNER, now=NOW) == []
