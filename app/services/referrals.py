"""Referral program with anti-abuse rules.

* A code only counts on the invitee's very first registration (``is_new``) and only once (``referred_by``
  is set with a conditional UPDATE, so a double tap cannot apply it twice).
* Self-referral and codes of banned users are rejected.
* One-time bonuses on registration: rubles (``REFERRAL_INVITEE_BONUS_RUB`` / ``REFERRAL_INVITER_BONUS_RUB``) or,
  in ``REFERRAL_REWARD_MODE=days``, subscription days (``REFERRAL_*_BONUS_DAYS``). With
  ``REFERRAL_INVITER_REQUIRES_PAYMENT`` the inviter's bonus, and with ``REFERRAL_INVITEE_REQUIRES_PAYMENT`` the
  invitee's money bonus, wait for the invitee's first payment that was paid in full and is at least
  ``MIN_TOPUP_RUB``, which makes farming with fake accounts pointless.
* Recurring rewards (``REFERRAL_LEVELS_PERCENT``, one percentage per level up the ``referred_by`` chain):
  modes ``balance`` (main wallet) and ``partner`` (partner ``REFERRAL`` account) pay that share of every credited
  REAL payment (:func:`on_invitee_payment`; never donations, never wallet-funded purchases, so bonus, promo, admin
  or commission money earns nothing). Mode ``days`` grants that share of the days of a paid plan purchase
  (``PURCHASE`` / ``AUTO_RENEW`` with ``price_kop > 0``; never trials, promo, gifts or admin grants) via
  :func:`on_invitee_purchase`, which ``subscriptions`` calls for every new purchase.
* The legacy single-level ``REFERRAL_INVITER_PERCENT`` still pays a share of every credited payment.
* Idempotency: money goes through :func:`app.services.wallet.apply` with deterministic refs (invitee
  ``"invitee"``, inviter fixed ``f"fixed:{invitee_id}"``, legacy percent ``f"pct:{payment_id}"``, level commission
  ``f"L{level}:{payment_id}"``); every reward also writes a ``ReferralReward`` row whose
  ``UNIQUE(source_ref, level)`` guards day grants and money alike, so a reward is never given twice in different
  forms after ``REFERRAL_REWARD_MODE`` changes.
* Lock order (InnoDB): like purchases, a day grant locks the beneficiary's subscription row before anything locks
  that user's row.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from app.db.models import (
    BalanceTransaction,
    LedgerAccount,
    Purchase,
    PurchaseKind,
    ReferralReward,
    Subscription,
    TxKind,
    User,
)
from app.services import events, subscriptions, wallet

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)

INVITEE_REF = "invitee"
# Ledger kinds counted as the inviter's referral earnings (fixed/percent bonuses and partner commissions).
_EARNING_KINDS = (TxKind.REFERRAL_INVITER, TxKind.REFERRAL_COMMISSION)
# Optional prefixes of deep-link payloads ("/start ref_ABCD2345"); never part of a code ("_" is not in codes).
_PAYLOAD_PREFIXES = ("REF_", "REF-", "R_")
# Purchases that earn level rewards: real plan purchases paid from the wallet.
_REWARDED_PURCHASE_KINDS = frozenset({PurchaseKind.PURCHASE, PurchaseKind.AUTO_RENEW})
BONUS_LEVEL = 0  # ReferralReward.level of one-time bonuses

# (source_user_id, level, source_ref) of a ReferralReward
_Source = tuple[int, int, str]


class ReferralOutcome(enum.StrEnum):
    APPLIED = "applied"
    DISABLED = "disabled"  # referral program is off
    NOT_FOUND = "not_found"  # no such code (or the inviter is banned)
    SELF = "self"  # the user's own code
    NOT_NEW = "not_new"  # not a first registration, or a referral is already set


@dataclass(frozen=True, slots=True)
class ReferralResult:
    outcome: ReferralOutcome
    inviter_id: int | None = None
    invitee_bonus_kop: int = 0
    inviter_bonus_kop: int = 0  # granted now (0 when it waits for the invitee's first payment)
    invitee_bonus_days: int = 0  # "days" mode
    inviter_bonus_days: int = 0


def fixed_ref(invitee_id: int) -> str:
    """Ledger ref of the inviter's one-time bonus for ``invitee_id``."""
    return f"fixed:{invitee_id}"


def percent_ref(payment_id: str) -> str:
    """Ledger ref of the inviter's percentage bonus for one invitee payment."""
    return f"pct:{payment_id}"


def bonus_source_ref(role: str, invitee_id: int) -> str:
    """``ReferralReward.source_ref`` of a one-time bonus (``role`` is ``"invitee"`` or ``"inviter"``)."""
    return f"bonus:{role}:{invitee_id}"


def purchase_source_ref(purchase_id: int) -> str:
    """``ReferralReward.source_ref`` of the level rewards for one purchase."""
    return f"purchase:{purchase_id}"


def payment_source_ref(payment_id: str) -> str:
    """``ReferralReward.source_ref`` of the level commissions for one payment."""
    return f"payment:{payment_id}"


def commission_ref(level: int, payment_id: str) -> str:
    """Ledger ref of a level commission for one payment."""
    return f"L{level}:{payment_id}"


def normalize_code(code: str | None) -> str:
    """Upper-cased code with an optional ``ref_`` deep-link prefix removed (``""`` if empty)."""
    normalized = (code or "").strip().upper()
    for prefix in _PAYLOAD_PREFIXES:
        if normalized.startswith(prefix):
            return normalized[len(prefix) :]
    return normalized


def _days_mode(settings: Settings) -> bool:
    return settings.referral_reward_mode == "days"


async def apply_on_registration(
    session: AsyncSession,
    settings: Settings,
    invitee: User,
    code: str | None,
    *,
    is_new: bool,
) -> ReferralResult:
    """Link ``invitee`` to the owner of ``code`` and grant the registration bonuses (rubles, or days in days mode).

    Call right after :func:`app.services.users.get_or_create` with its ``created`` flag as ``is_new``. In days mode
    the invitee's bonus days create their subscription (like a trial would).
    """
    normalized = normalize_code(code)
    if not normalized:
        return ReferralResult(ReferralOutcome.NOT_FOUND)
    if not settings.referral_enabled:
        return ReferralResult(ReferralOutcome.DISABLED)
    if not is_new or invitee.referred_by is not None:
        return ReferralResult(ReferralOutcome.NOT_NEW)
    days_mode = _days_mode(settings)
    inviter_now = not settings.referral_inviter_requires_payment
    if days_mode and inviter_now and settings.referral_inviter_bonus_days > 0:
        # The inviter's days are granted below: lock their subscription before their user row (purchase order).
        owner_id = await session.scalar(select(User.tg_id).where(User.referral_code == normalized))
        if owner_id is not None:
            await _lock_subscription(session, owner_id)
    # Lock the inviter first: concurrent sign-ups via one link then queue up instead of deadlocking (InnoDB)
    # between the referred_by foreign-key check and the inviter's balance update.
    inviter = await session.scalar(select(User).where(User.referral_code == normalized).with_for_update())
    if inviter is None or inviter.is_banned:
        return ReferralResult(ReferralOutcome.NOT_FOUND)
    if inviter.tg_id == invitee.tg_id:
        return ReferralResult(ReferralOutcome.SELF)
    if not await _link(session, invitee, inviter.tg_id):
        return ReferralResult(ReferralOutcome.NOT_NEW)

    invitee_source = (invitee.tg_id, BONUS_LEVEL, bonus_source_ref("invitee", invitee.tg_id))
    inviter_source = (invitee.tg_id, BONUS_LEVEL, bonus_source_ref("inviter", invitee.tg_id))
    invitee_kop = inviter_kop = invitee_days = inviter_days = 0
    if days_mode:
        invitee_days = await _grant_days(
            session, settings, invitee, settings.referral_invitee_bonus_days, invitee_source
        )
        if inviter_now:
            inviter_days = await _grant_days(
                session, settings, inviter, settings.referral_inviter_bonus_days, inviter_source
            )
    else:
        if not settings.referral_invitee_requires_payment:
            invitee_kop = await _credit(
                session,
                invitee.tg_id,
                settings.referral_invitee_bonus_kop,
                TxKind.REFERRAL_INVITEE,
                INVITEE_REF,
                invitee_source,
            )
        if inviter_now:
            inviter_kop = await _credit(
                session,
                inviter.tg_id,
                settings.referral_inviter_bonus_kop,
                TxKind.REFERRAL_INVITER,
                fixed_ref(invitee.tg_id),
                inviter_source,
            )
    await events.track(
        session,
        "referral.applied",
        user_id=invitee.tg_id,
        amount_kop=invitee_kop,
        inviter_id=inviter.tg_id,
        inviter_bonus_kop=inviter_kop,
        invitee_bonus_days=invitee_days,
        inviter_bonus_days=inviter_days,
    )
    log.info("referral: user %s invited by %s", invitee.tg_id, inviter.tg_id)
    return ReferralResult(ReferralOutcome.APPLIED, inviter.tg_id, invitee_kop, inviter_kop, invitee_days, inviter_days)


async def _link(session: AsyncSession, invitee: User, inviter_id: int) -> bool:
    """Set ``referred_by`` only if still empty (atomic); keeps the loaded object in sync."""
    stmt = (
        update(User)
        .where(User.tg_id == invitee.tg_id, User.referred_by.is_(None))
        .values(referred_by=inviter_id)
        .execution_options(synchronize_session=False)
    )
    result = cast(CursorResult[Any], await session.execute(stmt))
    if result.rowcount != 1:
        return False
    set_committed_value(invitee, "referred_by", inviter_id)
    return True


async def _lock_subscription(session: AsyncSession, user_id: int) -> None:
    """Row-lock the user's subscription (if any); see the lock-order note in the module docstring.

    A missing row is not locked: a locking read that finds nothing takes an InnoDB gap lock, and two new users
    locking the same gap and then inserting their subscriptions deadlock (``grant_days`` creates it via UNIQUE).
    """
    sub_id = await session.scalar(select(Subscription.id).where(Subscription.user_id == user_id))
    if sub_id is not None:
        await session.execute(select(Subscription.id).where(Subscription.id == sub_id).with_for_update())


async def _record(
    session: AsyncSession,
    beneficiary_id: int,
    source: _Source,
    *,
    amount_kop: int = 0,
    days: int = 0,
    account: LedgerAccount | None = None,
) -> ReferralReward | None:
    """Insert the ``ReferralReward`` row once per ``(source_ref, level)``; ``None`` if it already exists."""
    source_user_id, level, source_ref = source
    reward = ReferralReward(
        beneficiary_id=beneficiary_id,
        source_user_id=source_user_id,
        level=level,
        source_ref=source_ref,
        amount_kop=amount_kop,
        days=days,
        account=account,
    )
    try:
        async with session.begin_nested():
            session.add(reward)
    except IntegrityError:
        return None
    return reward


async def _credit(
    session: AsyncSession,
    user_id: int,
    amount_kop: int,
    kind: TxKind,
    ref: str,
    source: _Source,
    account: LedgerAccount = LedgerAccount.MAIN,
) -> int:
    """Credit a money reward once and record it; returns the amount granted now (0 if none).

    Both the wallet ref and the ``ReferralReward`` key must be new: a reward already given in another form
    (days, or the other ledger account after a reward mode change) is not paid again. The balance is updated
    before the reward row is inserted (same lock order as the other money paths).
    """
    if amount_kop <= 0:
        return 0
    try:
        async with session.begin_nested():
            if await wallet.apply(session, user_id, amount_kop, kind, ref, account=account) is None:
                return 0
            if await _record(session, user_id, source, amount_kop=amount_kop, account=account) is None:
                raise _AlreadyRewarded
    except _AlreadyRewarded:
        await _reload_balances(session, user_id)
        return 0
    return amount_kop


class _AlreadyRewarded(Exception):
    """Rolls back a wallet credit whose ``ReferralReward`` key was already used."""


async def _reload_balances(session: AsyncSession, user_id: int) -> None:
    """Re-read the balances of a loaded ``User`` after a rolled-back savepoint changed them in place."""
    user = session.sync_session.identity_map.get(identity_key(User, user_id))
    if user is not None:
        await session.refresh(user, ["balance_kop", "referral_balance_kop"])


async def _grant_days(session: AsyncSession, settings: Settings, beneficiary: User, days: int, source: _Source) -> int:
    """Grant reward days once per ``(source_ref, level)``; returns the days granted now (0 if none)."""
    if days <= 0:
        return 0
    await _lock_subscription(session, beneficiary.tg_id)
    if await _record(session, beneficiary.tg_id, source, days=days) is None:
        return 0
    await subscriptions.grant_days(session, settings, beneficiary, days, kind=PurchaseKind.REFERRAL_REWARD)
    return days


async def on_invitee_payment(
    session: AsyncSession,
    settings: Settings,
    invitee_id: int,
    payment_id: str,
    amount_kop: int,
    *,
    expected_kop: int | None = None,
) -> tuple[int, int] | None:
    """Reward the upline for a credited real (non-donation) invitee payment of ``amount_kop``.

    * The deferred one-time inviter bonus (``REFERRAL_INVITER_REQUIRES_PAYMENT``; rubles, or days in days mode) and
      the invitee's deferred money bonus (``REFERRAL_INVITEE_REQUIRES_PAYMENT``), only for a payment paid in full
      (``amount_kop >= expected_kop``, the invoice amount) of at least ``MIN_TOPUP_RUB``; until then they stay
      locked, so a later full payment still unlocks them.
    * ``REFERRAL_LEVELS_PERCENT`` commissions up the chain (balance / partner mode; days mode rewards purchases,
      see :func:`on_invitee_purchase`), each rounded down.
    * The legacy ``REFERRAL_INVITER_PERCENT`` of ``amount_kop`` (rounded down).

    Returns ``(inviter_id, kop)`` with the money the direct inviter got from this call (0 when only days were
    granted), or ``None`` if the inviter got nothing. Repeated calls for a payment grant nothing.
    """
    if not settings.referral_enabled:
        return None
    inviter_id = await session.scalar(select(User.referred_by).where(User.tg_id == invitee_id))
    if inviter_id is None:
        return None
    level_one = 0
    if not _days_mode(settings) and amount_kop > 0:
        level_one = await _payment_commissions(session, settings, invitee_id, payment_id, amount_kop)
    paid_in_full = expected_kop is None or amount_kop >= expected_kop
    qualifies = paid_in_full and amount_kop >= settings.min_topup_kop
    if settings.referral_invitee_requires_payment and qualifies and not _days_mode(settings):
        await _credit(
            session,
            invitee_id,
            settings.referral_invitee_bonus_kop,
            TxKind.REFERRAL_INVITEE,
            INVITEE_REF,
            (invitee_id, BONUS_LEVEL, bonus_source_ref("invitee", invitee_id)),
        )
    inviter = await session.get(User, inviter_id)
    if inviter is None or inviter.is_banned:
        return None

    total, days = level_one, 0
    if settings.referral_inviter_requires_payment and qualifies:
        source = (invitee_id, BONUS_LEVEL, bonus_source_ref("inviter", invitee_id))
        if _days_mode(settings):
            days = await _grant_days(session, settings, inviter, settings.referral_inviter_bonus_days, source)
        else:
            total += await _credit(
                session,
                inviter_id,
                settings.referral_inviter_bonus_kop,
                TxKind.REFERRAL_INVITER,
                fixed_ref(invitee_id),
                source,
            )
    percent = settings.referral_inviter_percent
    bonus = amount_kop * percent // 100 if percent > 0 and amount_kop > 0 else 0
    if bonus > 0:
        total += await wallet.apply(session, inviter_id, bonus, TxKind.REFERRAL_INVITER, percent_ref(payment_id)) or 0
    if total == 0 and days == 0:
        return None
    log.info("referral: inviter %s earned %d kop / %d days from payment %s", inviter_id, total, days, payment_id)
    return inviter_id, total


async def _payment_commissions(
    session: AsyncSession, settings: Settings, invitee_id: int, payment_id: str, amount_kop: int
) -> int:
    """Credit ``REFERRAL_LEVELS_PERCENT`` of a real payment up the chain; returns what level 1 got now."""
    levels = settings.referral_levels_percent
    if not any(levels):
        return 0
    account = LedgerAccount.REFERRAL if settings.referral_reward_mode == "partner" else LedgerAccount.MAIN
    source_ref = payment_source_ref(payment_id)
    level_one = 0
    for level, beneficiary in enumerate(await _upline(session, invitee_id, len(levels)), start=1):
        percent = levels[level - 1]
        if percent <= 0 or beneficiary.is_banned:
            continue
        granted = await _credit(
            session,
            beneficiary.tg_id,
            amount_kop * percent // 100,
            TxKind.REFERRAL_COMMISSION,
            commission_ref(level, payment_id),
            (invitee_id, level, source_ref),
            account,
        )
        if not granted:
            continue
        if level == 1:
            level_one = granted
        await _track_reward(session, beneficiary.tg_id, level, source_ref, amount_kop=granted)
    return level_one


async def on_invitee_purchase(session: AsyncSession, settings: Settings, purchase: Purchase) -> list[ReferralReward]:
    """Days mode: grant ``REFERRAL_LEVELS_PERCENT`` of a paid plan purchase's days up the chain (once per purchase).

    Level ``n`` (1 = the buyer's inviter) gets ``levels[n-1]`` % of ``purchase.days``, rounded down. Free
    (``price_kop <= 0``) purchases earn nothing. Banned beneficiaries are skipped (the chain goes on). In balance /
    partner mode commissions come from credited payments instead (:func:`on_invitee_payment`), so this is a no-op.
    Runs in the caller's transaction; returns the rewards granted by this call.
    """
    levels = settings.referral_levels_percent
    if (
        not settings.referral_enabled
        or not _days_mode(settings)
        or purchase.kind not in _REWARDED_PURCHASE_KINDS
        or purchase.price_kop <= 0
        or not any(levels)
    ):
        return []
    source_ref = purchase_source_ref(purchase.id)
    rewards: list[ReferralReward] = []
    for level, beneficiary in enumerate(await _upline(session, purchase.user_id, len(levels)), start=1):
        percent = levels[level - 1]
        if percent <= 0 or beneficiary.is_banned:
            continue
        source = (purchase.user_id, level, source_ref)
        if await _grant_days(session, settings, beneficiary, purchase.days * percent // 100, source) <= 0:
            continue
        reward = await session.scalar(
            select(ReferralReward).where(ReferralReward.source_ref == source_ref, ReferralReward.level == level)
        )
        if reward is not None:
            await _track_reward(session, beneficiary.tg_id, level, source_ref, days=reward.days)
            rewards.append(reward)
    return rewards


async def _upline(session: AsyncSession, user_id: int, depth: int) -> list[User]:
    """The inviter, the inviter's inviter, ... (at most ``depth``; stops at a cycle)."""
    chain: list[User] = []
    seen = {user_id}
    current = await session.scalar(select(User.referred_by).where(User.tg_id == user_id))
    while current is not None and current not in seen and len(chain) < depth:
        inviter = await session.get(User, current)
        if inviter is None:
            break
        chain.append(inviter)
        seen.add(current)
        current = inviter.referred_by
    return chain


async def _track_reward(
    session: AsyncSession, user_id: int, level: int, source_ref: str, *, amount_kop: int = 0, days: int = 0
) -> None:
    await events.track(
        session,
        "referral.reward",
        user_id=user_id,
        amount_kop=amount_kop or None,
        level=level,
        days=days,
        source_ref=source_ref,
    )
    log.info("referral: user %s earned a level-%d reward for %s", user_id, level, source_ref)


async def stats(session: AsyncSession, user_id: int) -> tuple[int, int]:
    """``(invited_count, earned_kop)`` for a user's referral screen."""
    invited = await session.scalar(select(func.count()).select_from(User).where(User.referred_by == user_id))
    earned = await session.scalar(
        select(func.coalesce(func.sum(BalanceTransaction.amount_kop), 0)).where(
            BalanceTransaction.user_id == user_id,
            BalanceTransaction.kind.in_(_EARNING_KINDS),
        )
    )
    return int(invited or 0), int(earned or 0)


async def top(session: AsyncSession, limit: int = 10) -> list[tuple[User, int]]:
    """Users with the most invitees, most first (ties: lower id first)."""
    invited = func.count().label("invited")
    counts = (
        select(User.referred_by.label("inviter_id"), invited)
        .where(User.referred_by.is_not(None))
        .group_by(User.referred_by)
        .subquery()
    )
    stmt = (
        select(User, counts.c.invited)
        .join(counts, counts.c.inviter_id == User.tg_id)
        .order_by(counts.c.invited.desc(), User.tg_id)
        .limit(max(1, limit))
    )
    return [(user, int(count)) for user, count in (await session.execute(stmt)).all()]
