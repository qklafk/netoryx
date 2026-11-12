"""Partner program: partner balance transfers, payout requests (withdrawals) and partner statistics.

Partner earnings live on the REFERRAL ledger account (``users.referral_balance_kop``). From there a partner can
move money to the spendable wallet (:func:`transfer_to_main`) or ask for a payout (:func:`request_withdrawal`).
A payout request debits the partner balance immediately (so the money cannot be spent twice while the request
waits); rejecting it refunds the amount, paying it out happens outside the system and is only recorded here.

Every balance change goes through :func:`app.services.wallet.apply` with deterministic refs, so all operations are
idempotent. Functions take the caller's ``session`` and never commit.
"""

from __future__ import annotations

import enum
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from sqlalchemy import CursorResult, Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    LedgerAccount,
    Payment,
    PaymentStatus,
    ReferralReward,
    TxKind,
    User,
    Withdrawal,
    WithdrawalStatus,
)
from app.services import audit, events, wallet
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)

MAX_DETAILS_LENGTH = 512
MAX_COMMENT_LENGTH = 512
MAX_STATS_LEVELS = 10
# Statuses of a payout request that is still being processed (only one may exist per partner).
OPEN_STATUSES = (WithdrawalStatus.PENDING, WithdrawalStatus.APPROVED)

# Anti-fraud heuristics (flags for the admin, never automatic decisions).
YOUNG_ACCOUNT_DAYS = 14
LOW_CONVERSION_MIN_REFERRALS = 10
LOW_CONVERSION_RATIO = 0.05
BURST_MIN_PER_DAY = 15
CONCENTRATION_MIN_EARNED_KOP = 100_00
CONCENTRATION_SHARE = 0.8


class PartnerErrorCode(enum.StrEnum):
    DISABLED = "disabled"  # withdrawals are off or the program is not in partner mode
    BAD_AMOUNT = "bad_amount"
    BELOW_MINIMUM = "below_minimum"
    BAD_METHOD = "bad_method"
    BAD_DETAILS = "bad_details"
    PENDING_EXISTS = "pending_exists"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    NOT_FOUND = "not_found"
    BAD_STATUS = "bad_status"


class PartnerError(Exception):
    """A business rule refused the operation; nothing was written."""

    def __init__(self, code: PartnerErrorCode, message: str | None = None) -> None:
        super().__init__(message or code.value)
        self.code = code


@dataclass(frozen=True, slots=True)
class TransferResult:
    ref: str
    amount_kop: int
    applied: bool  # False: this ref was transferred before (replay)


@dataclass(frozen=True, slots=True)
class LevelStats:
    level: int
    users: int  # referrals on this level of the tree
    paid_users: int  # referrals on this level who brought at least one reward
    earned_kop: int
    earned_days: int
    rewards: int


@dataclass(frozen=True, slots=True)
class PartnerStats:
    levels: list[LevelStats]
    total_users: int
    total_paid_users: int
    total_earned_kop: int  # level rewards + one-time bonuses
    total_earned_days: int
    bonus_kop: int  # one-time bonuses (level 0)
    bonus_days: int
    referral_balance_kop: int
    pending_withdrawal_kop: int
    withdrawn_kop: int


class FraudFlagCode(enum.StrEnum):
    YOUNG_ACCOUNT = "young_account"
    LOW_CONVERSION = "low_conversion"  # many referrals, almost none paid
    REGISTRATION_BURST = "registration_burst"  # many referrals registered on one day
    CONCENTRATED_EARNINGS = "concentrated_earnings"  # almost all earnings come from one referral


@dataclass(frozen=True, slots=True)
class FraudFlag:
    code: FraudFlagCode
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PendingWithdrawal:
    withdrawal: Withdrawal
    user: User | None
    flags: list[FraudFlag]


def withdrawal_ref(withdrawal_id: int) -> str:
    """Ledger ref of a payout request's debit (and of its refund on rejection)."""
    return f"withdrawal:{withdrawal_id}"


# ---------------------------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------------------------
async def transfer_to_main(
    session: AsyncSession,
    user_id: int,
    amount_kop: int,
    *,
    ref: str | None = None,
    actor_id: int | None = None,
) -> TransferResult:
    """Move ``amount_kop`` from the partner balance to the main wallet.

    Writes two ledger rows with the same ref (REFERRAL -amount, MAIN +amount) inside one SAVEPOINT. Pass the same
    ``ref`` to retry safely. Raises :class:`PartnerError` (BAD_AMOUNT, INSUFFICIENT_FUNDS, NOT_FOUND).
    """
    if isinstance(amount_kop, bool) or not isinstance(amount_kop, int) or amount_kop <= 0:
        raise PartnerError(PartnerErrorCode.BAD_AMOUNT)
    ref = ref or f"transfer:{uuid4().hex}"
    try:
        async with session.begin_nested():
            debited = await wallet.apply(
                session,
                user_id,
                -amount_kop,
                TxKind.REFERRAL_TRANSFER,
                ref,
                actor_id=actor_id,
                account=LedgerAccount.REFERRAL,
            )
            credited = await wallet.apply(
                session, user_id, amount_kop, TxKind.REFERRAL_TRANSFER, ref, actor_id=actor_id
            )
    except wallet.InsufficientFunds as exc:
        raise PartnerError(PartnerErrorCode.INSUFFICIENT_FUNDS, str(exc)) from exc
    except LookupError as exc:
        raise PartnerError(PartnerErrorCode.NOT_FOUND, str(exc)) from exc
    applied = debited is not None or credited is not None
    if applied:
        await events.track(session, "partner.transfer", user_id=user_id, amount_kop=amount_kop)
    return TransferResult(ref, amount_kop, applied)


# ---------------------------------------------------------------------------------------------
# Withdrawals
# ---------------------------------------------------------------------------------------------
def withdrawals_available(settings: Settings) -> bool:
    """Payout requests are possible: partner mode with withdrawals enabled and at least one method."""
    return (
        settings.referral_reward_mode == "partner"
        and settings.withdrawals_enabled
        and bool(settings.withdrawal_methods)
    )


async def open_withdrawal(session: AsyncSession, user_id: int) -> Withdrawal | None:
    """The partner's payout request that is still being processed, if any."""
    return await session.scalar(
        select(Withdrawal)
        .where(Withdrawal.user_id == user_id, Withdrawal.status.in_(OPEN_STATUSES))
        .order_by(Withdrawal.id.desc())
        .limit(1)
    )


async def request_withdrawal(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    amount_kop: int,
    method: str,
    details: str,
) -> Withdrawal:
    """Create a payout request and debit the partner balance right away.

    Rules: partner mode with withdrawals enabled; ``amount_kop >= MIN_WITHDRAWAL_RUB``; ``method`` is one of
    ``WITHDRAWAL_METHODS``; non-empty details; at most one open request per partner. Raises :class:`PartnerError`;
    nothing is written then.
    """
    if not withdrawals_available(settings):
        raise PartnerError(PartnerErrorCode.DISABLED)
    if isinstance(amount_kop, bool) or not isinstance(amount_kop, int) or amount_kop <= 0:
        raise PartnerError(PartnerErrorCode.BAD_AMOUNT)
    if amount_kop < settings.min_withdrawal_kop:
        raise PartnerError(PartnerErrorCode.BELOW_MINIMUM)
    if method not in settings.withdrawal_methods:
        raise PartnerError(PartnerErrorCode.BAD_METHOD)
    clean_details = " ".join(details.split())
    if not clean_details or len(clean_details) > MAX_DETAILS_LENGTH:
        raise PartnerError(PartnerErrorCode.BAD_DETAILS)
    # Serialize requests of one partner: the row lock makes "one open request" race-free.
    user = await session.scalar(select(User).where(User.tg_id == user_id).with_for_update())
    if user is None:
        raise PartnerError(PartnerErrorCode.NOT_FOUND)
    if await open_withdrawal(session, user_id) is not None:
        raise PartnerError(PartnerErrorCode.PENDING_EXISTS)
    try:
        async with session.begin_nested():
            withdrawal = Withdrawal(
                user_id=user_id,
                amount_kop=amount_kop,
                method=method,
                details=clean_details,
                status=WithdrawalStatus.PENDING,
            )
            session.add(withdrawal)
            await session.flush()
            await wallet.apply(
                session,
                user_id,
                -amount_kop,
                TxKind.WITHDRAWAL,
                withdrawal_ref(withdrawal.id),
                account=LedgerAccount.REFERRAL,
            )
    except wallet.InsufficientFunds as exc:
        raise PartnerError(PartnerErrorCode.INSUFFICIENT_FUNDS, str(exc)) from exc
    await events.track(session, "withdrawal.requested", user_id=user_id, amount_kop=amount_kop, method=method)
    log.info("partner: user %s requested a payout of %d kop (#%s)", user_id, amount_kop, withdrawal.id)
    return withdrawal


async def _transition(
    session: AsyncSession,
    withdrawal_id: int,
    allowed: tuple[WithdrawalStatus, ...],
    new: WithdrawalStatus,
    admin_id: int,
    comment: str | None,
    now: datetime,
) -> Withdrawal:
    """Conditionally move a request between statuses (a concurrent admin action makes it BAD_STATUS)."""
    values: dict[str, Any] = {"status": new, "admin_id": admin_id, "processed_at": now}
    if comment is not None:
        values["admin_comment"] = comment.strip()[:MAX_COMMENT_LENGTH] or None
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Withdrawal)
            .where(Withdrawal.id == withdrawal_id, Withdrawal.status.in_(allowed))
            .values(**values)
            .execution_options(synchronize_session=False)
        ),
    )
    withdrawal = await session.get(Withdrawal, withdrawal_id, populate_existing=True)
    if withdrawal is None:
        raise PartnerError(PartnerErrorCode.NOT_FOUND)
    if result.rowcount != 1:
        raise PartnerError(PartnerErrorCode.BAD_STATUS, f"withdrawal {withdrawal_id} is {withdrawal.status}")
    return withdrawal


async def approve(
    session: AsyncSession,
    withdrawal_id: int,
    admin_id: int,
    *,
    comment: str | None = None,
    source: str = "bot",
    ip: str | None = None,
    now: datetime | None = None,
) -> Withdrawal:
    """PENDING -> APPROVED (the payout is being made)."""
    withdrawal = await _transition(
        session,
        withdrawal_id,
        (WithdrawalStatus.PENDING,),
        WithdrawalStatus.APPROVED,
        admin_id,
        comment,
        now or utcnow(),
    )
    await audit.log(
        session, admin_id, "withdrawal.approve", "withdrawal", withdrawal_id, source, ip, user_id=withdrawal.user_id
    )
    return withdrawal


async def mark_paid(
    session: AsyncSession,
    withdrawal_id: int,
    admin_id: int,
    *,
    comment: str | None = None,
    source: str = "bot",
    ip: str | None = None,
    now: datetime | None = None,
) -> Withdrawal:
    """PENDING/APPROVED -> PAID (money was sent to the partner)."""
    withdrawal = await _transition(
        session, withdrawal_id, OPEN_STATUSES, WithdrawalStatus.PAID, admin_id, comment, now or utcnow()
    )
    await audit.log(
        session,
        admin_id,
        "withdrawal.paid",
        "withdrawal",
        withdrawal_id,
        source,
        ip,
        user_id=withdrawal.user_id,
        amount_kop=withdrawal.amount_kop,
    )
    await events.track(session, "withdrawal.paid", user_id=withdrawal.user_id, amount_kop=withdrawal.amount_kop)
    return withdrawal


async def reject(
    session: AsyncSession,
    withdrawal_id: int,
    admin_id: int,
    *,
    comment: str | None = None,
    source: str = "bot",
    ip: str | None = None,
    now: datetime | None = None,
) -> Withdrawal:
    """PENDING/APPROVED -> REJECTED and return the amount to the partner balance (idempotent refund)."""
    withdrawal = await _transition(
        session, withdrawal_id, OPEN_STATUSES, WithdrawalStatus.REJECTED, admin_id, comment, now or utcnow()
    )
    await wallet.apply(
        session,
        withdrawal.user_id,
        withdrawal.amount_kop,
        TxKind.REFUND,
        withdrawal_ref(withdrawal.id),
        actor_id=admin_id,
        account=LedgerAccount.REFERRAL,
    )
    await audit.log(
        session,
        admin_id,
        "withdrawal.reject",
        "withdrawal",
        withdrawal_id,
        source,
        ip,
        user_id=withdrawal.user_id,
        amount_kop=withdrawal.amount_kop,
        comment=withdrawal.admin_comment,
    )
    return withdrawal


async def list_pending(
    session: AsyncSession,
    *,
    statuses: tuple[WithdrawalStatus, ...] = OPEN_STATUSES,
    limit: int = 50,
    before_id: int | None = None,
    with_flags: bool = True,
    now: datetime | None = None,
) -> list[PendingWithdrawal]:
    """The payout queue, oldest first, with the partner and anti-fraud flags."""
    stmt = select(Withdrawal).where(Withdrawal.status.in_(statuses)).order_by(Withdrawal.id)
    if before_id is not None:
        stmt = stmt.where(Withdrawal.id < before_id)
    rows = list((await session.scalars(stmt.limit(max(1, min(limit, 200))))).all())
    users = {u.tg_id: u for u in (await session.scalars(select(User).where(User.tg_id.in_({w.user_id for w in rows}))))}
    result: list[PendingWithdrawal] = []
    for withdrawal in rows:
        flags = await fraud_flags(session, withdrawal.user_id, now=now) if with_flags else []
        result.append(PendingWithdrawal(withdrawal, users.get(withdrawal.user_id), flags))
    return result


async def list_for_user(session: AsyncSession, user_id: int, *, limit: int = 20) -> list[Withdrawal]:
    """A partner's payout requests, newest first."""
    stmt = select(Withdrawal).where(Withdrawal.user_id == user_id).order_by(Withdrawal.id.desc())
    return list((await session.scalars(stmt.limit(max(1, min(limit, 200))))).all())


# ---------------------------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------------------------
def _level_query(user_id: int, level: int) -> Select[int]:
    """Ids of the referrals ``level`` steps below ``user_id``."""
    stmt: Select[int] = select(User.tg_id).where(User.referred_by == user_id)
    for _ in range(level - 1):
        stmt = select(User.tg_id).where(User.referred_by.in_(stmt))
    return stmt


async def tree_counts(session: AsyncSession, user_id: int, levels: int) -> list[int]:
    """Number of referrals on each level 1..``levels`` of the partner's tree."""
    counts: list[int] = []
    for level in range(1, max(0, min(levels, MAX_STATS_LEVELS)) + 1):
        sub = _level_query(user_id, level).subquery()
        counts.append(int(await session.scalar(select(func.count()).select_from(sub)) or 0))
    return counts


async def partner_stats(session: AsyncSession, settings: Settings, user_id: int) -> PartnerStats:
    """Per-level tree size and earnings (from ``ReferralReward`` rows), balances and payout totals."""
    rows = (
        await session.execute(
            select(
                ReferralReward.level,
                func.count(ReferralReward.id),
                func.count(func.distinct(ReferralReward.source_user_id)),
                func.coalesce(func.sum(ReferralReward.amount_kop), 0),
                func.coalesce(func.sum(ReferralReward.days), 0),
            )
            .where(ReferralReward.beneficiary_id == user_id)
            .group_by(ReferralReward.level)
        )
    ).all()
    by_level = {int(level): (int(n), int(paid), int(kop), int(days)) for level, n, paid, kop, days in rows}
    depth = max([len(settings.referral_levels_percent), *(lvl for lvl in by_level if lvl > 0)], default=0)
    counts = await tree_counts(session, user_id, depth)
    levels = []
    for index, users_on_level in enumerate(counts, start=1):
        n, paid, kop, days = by_level.get(index, (0, 0, 0, 0))
        levels.append(LevelStats(index, users_on_level, paid, kop, days, n))
    bonus = by_level.get(0, (0, 0, 0, 0))
    totals = await session.execute(
        select(Withdrawal.status, func.coalesce(func.sum(Withdrawal.amount_kop), 0))
        .where(Withdrawal.user_id == user_id)
        .group_by(Withdrawal.status)
    )
    by_status = {WithdrawalStatus(status): int(amount) for status, amount in totals.all()}
    return PartnerStats(
        levels=levels,
        total_users=sum(counts),
        total_paid_users=sum(level.paid_users for level in levels),
        total_earned_kop=sum(level.earned_kop for level in levels) + bonus[2],
        total_earned_days=sum(level.earned_days for level in levels) + bonus[3],
        bonus_kop=bonus[2],
        bonus_days=bonus[3],
        referral_balance_kop=await wallet.balance(session, user_id, LedgerAccount.REFERRAL),
        pending_withdrawal_kop=sum(by_status.get(s, 0) for s in OPEN_STATUSES),
        withdrawn_kop=by_status.get(WithdrawalStatus.PAID, 0),
    )


# ---------------------------------------------------------------------------------------------
# Anti-fraud heuristics
# ---------------------------------------------------------------------------------------------
async def fraud_flags(
    session: AsyncSession, user_id: int, *, settings: Settings | None = None, now: datetime | None = None
) -> list[FraudFlag]:
    """Signals that a partner may be farming rewards. Informational: the admin decides.

    * young account (registered less than :data:`YOUNG_ACCOUNT_DAYS` ago);
    * many direct referrals but almost none of them ever paid;
    * a burst of direct referrals registered on one (local) day;
    * almost all money earned from a single referral.
    """
    now = now or utcnow()
    flags: list[FraudFlag] = []
    created_at = await session.scalar(select(User.created_at).where(User.tg_id == user_id))
    if created_at is not None and now - created_at < timedelta(days=YOUNG_ACCOUNT_DAYS):
        flags.append(FraudFlag(FraudFlagCode.YOUNG_ACCOUNT, {"age_days": (now - created_at).days}))

    referral_dates = list((await session.scalars(select(User.created_at).where(User.referred_by == user_id))).all())
    total = len(referral_dates)
    if total >= LOW_CONVERSION_MIN_REFERRALS:
        paid = int(
            await session.scalar(
                select(func.count(func.distinct(Payment.user_id))).where(
                    Payment.user_id.in_(select(User.tg_id).where(User.referred_by == user_id)),
                    Payment.status == PaymentStatus.SUCCEEDED,
                    Payment.credited_at.is_not(None),
                    Payment.is_donation.is_(False),
                )
            )
            or 0
        )
        if paid / total < LOW_CONVERSION_RATIO:
            flags.append(FraudFlag(FraudFlagCode.LOW_CONVERSION, {"referrals": total, "paid": paid}))
    if total >= BURST_MIN_PER_DAY:
        zone = settings.zone if settings is not None else None
        days = Counter((dt.astimezone(zone) if zone else dt).date() for dt in referral_dates)
        day, count = days.most_common(1)[0]
        if count >= BURST_MIN_PER_DAY:
            flags.append(FraudFlag(FraudFlagCode.REGISTRATION_BURST, {"day": day.isoformat(), "count": count}))

    earned = (
        await session.execute(
            select(ReferralReward.source_user_id, func.coalesce(func.sum(ReferralReward.amount_kop), 0))
            .where(ReferralReward.beneficiary_id == user_id, ReferralReward.amount_kop > 0)
            .group_by(ReferralReward.source_user_id)
        )
    ).all()
    total_earned = sum(int(amount) for _, amount in earned)
    if total_earned >= CONCENTRATION_MIN_EARNED_KOP and len(earned) >= 1:
        top_source, top_amount = max(earned, key=lambda row: int(row[1]))
        if int(top_amount) / total_earned >= CONCENTRATION_SHARE:
            flags.append(
                FraudFlag(
                    FraudFlagCode.CONCENTRATED_EARNINGS,
                    {"source_user_id": int(top_source), "share": round(int(top_amount) / total_earned, 2)},
                )
            )
    return flags
