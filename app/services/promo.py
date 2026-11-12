"""Promo codes (created by admins) and gift codes (a plan bought by a user for someone else).

Redemption locks the code row, consumes a use with a conditional UPDATE (``max_uses`` can never be exceeded,
even by concurrent redemptions), credits money via the wallet and records a ``PromoActivation`` (one per user and
code) - all inside one SAVEPOINT. Bonus *days* are applied by the caller (``subscriptions.grant_days``) because
they touch subscriptions and panels.

Lock order (InnoDB): purchases and auto-renewals lock the subscription row, then the user row. A redemption with
bonus days is followed by ``grant_days`` in the same transaction, so it locks the subscription row first too;
the wallet UPDATE (exclusive user-row lock) runs before the activation insert, whose foreign-key check would
otherwise take a shared lock on the same row first.
"""

from __future__ import annotations

import enum
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, exists, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.db.models import (
    Payment,
    PaymentStatus,
    Plan,
    PromoActivation,
    PromoCode,
    PromoGroup,
    PromoGroupMember,
    PromoKind,
    Purchase,
    PurchaseKind,
    Subscription,
    TxKind,
    User,
    WinbackOffer,
)
from app.services import events, kv, wallet
from app.services.subscriptions import SubErrorCode, SubscriptionError
from app.utils.security import random_code, sha256_hex
from app.utils.time import MAX_PERIOD_DAYS, utcnow

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)

GENERATED_CODE_LENGTH = 10
GIFT_CODE_LENGTH = 12
_CODE_PATTERN = re.compile(r"[\w-]{3,64}")  # letters (incl. Cyrillic), digits, "_" and "-"
_MAX_CODE_ATTEMPTS = 5
_PAID_PURCHASE_KINDS = (PurchaseKind.PURCHASE, PurchaseKind.AUTO_RENEW)


class PromoOutcome(enum.StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"  # unknown or deactivated code
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"  # max_uses reached
    ALREADY_USED = "already_used"  # this user has redeemed it before
    ONLY_NEW = "only_new"  # code is for users who have never paid


class PromoCodeExists(ValueError):
    """A promo code with this text already exists."""


@dataclass(frozen=True, slots=True)
class PersonalDiscount:
    """The highest active group discount applicable to one buyer."""

    percent: int = 0
    group_id: int | None = None

    def price(self, full_price_kop: int) -> int:
        if self.percent <= 0 or full_price_kop <= 0:
            return full_price_kop
        return max(1, full_price_kop * (100 - self.percent) // 100)


async def personal_discount(session: AsyncSession, user_id: int, *, now: datetime | None = None) -> PersonalDiscount:
    """Return the best active group membership; expired groups affect no newly created invoice."""
    moment = now or utcnow()
    group = (
        await session.execute(
            select(PromoGroup.id, PromoGroup.discount_percent)
            .join(PromoGroupMember, PromoGroupMember.group_id == PromoGroup.id)
            .where(
                PromoGroupMember.user_id == user_id,
                PromoGroup.is_active.is_(True),
                (PromoGroup.expires_at.is_(None)) | (PromoGroup.expires_at > moment),
            )
            .order_by(PromoGroup.discount_percent.desc(), PromoGroup.id)
            .limit(1)
        )
    ).first()
    offer = await session.scalar(
        select(WinbackOffer.discount_percent)
        .where(WinbackOffer.user_id == user_id, WinbackOffer.expires_at > moment)
        .order_by(WinbackOffer.discount_percent.desc(), WinbackOffer.id)
        .limit(1)
    )
    group_discount = int(group.discount_percent) if group else 0
    offer_discount = int(offer) if offer is not None else 0
    if offer_discount > group_discount:
        return PersonalDiscount(percent=offer_discount)
    return PersonalDiscount(percent=group_discount, group_id=int(group.id)) if group else PersonalDiscount()


class _Abort(Exception):
    """Internal: rolls the redemption SAVEPOINT back with the given outcome."""

    def __init__(self, outcome: PromoOutcome) -> None:
        super().__init__(outcome)
        self.outcome = outcome


def normalize_code(code: str | None) -> str:
    """Codes are case-insensitive and stored upper-case."""
    return (code or "").strip().upper()


async def get_by_code(session: AsyncSession, code: str) -> PromoCode | None:
    """Promo or gift code by its text (case-insensitive)."""
    normalized = normalize_code(code)
    if not normalized:
        return None
    return await session.scalar(select(PromoCode).where(PromoCode.code == normalized))


async def is_new_customer(session: AsyncSession, user_id: int) -> bool:
    """True if the user has never paid: no succeeded payment and no wallet-paid purchase or renewal."""
    paid = exists().where(Payment.user_id == user_id, Payment.status == PaymentStatus.SUCCEEDED)
    bought = exists().where(Purchase.user_id == user_id, Purchase.kind.in_(_PAID_PURCHASE_KINDS))
    return not await session.scalar(select(paid | bought))


# ---------------------------------------------------------------------------------------------
# Redemption
# ---------------------------------------------------------------------------------------------
async def redeem(
    session: AsyncSession, settings: Settings, user: User, code: str
) -> tuple[PromoOutcome, PromoCode | None]:
    """Redeem ``code`` for ``user``. Returns the outcome and the code (``None`` only for NOT_FOUND).

    On ``OK`` the money bonus (``TxKind.PROMO``, ref = promo id) is already on the wallet; if
    ``promo.bonus_days > 0`` the caller must extend the subscription (``PurchaseKind.PROMO``, or
    ``GIFT_REDEEM`` with ``promo.plan_id`` for gifts).
    """
    normalized = normalize_code(code)
    if not normalized:
        return PromoOutcome.NOT_FOUND, None
    promo = await session.scalar(
        select(PromoCode)
        .where(PromoCode.code == normalized)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if promo is None or not promo.is_active:
        return PromoOutcome.NOT_FOUND, None
    outcome = await _precheck(session, promo, user.tg_id)
    if outcome is not PromoOutcome.OK:
        return outcome, promo
    if promo.bonus_days > 0:
        await _lock_subscription(session, user.tg_id)
    try:
        async with session.begin_nested():
            await _consume(session, promo, user.tg_id)
    except _Abort as abort:
        return abort.outcome, promo
    except IntegrityError:  # concurrent redemption by the same user won the activation insert
        return PromoOutcome.ALREADY_USED, promo
    await _sync_uses(session, promo)
    await events.track(
        session,
        "gift.redeemed" if promo.kind is PromoKind.GIFT else "promo.redeemed",
        user_id=user.tg_id,
        amount_kop=promo.bonus_kop or None,
        promo_id=promo.id,
        bonus_days=promo.bonus_days,
    )
    log.info("promo %s redeemed by user %s", promo.id, user.tg_id)
    return PromoOutcome.OK, promo


async def _precheck(session: AsyncSession, promo: PromoCode, user_id: int) -> PromoOutcome:
    """Cheap checks before writing; the conditional UPDATE in :func:`_consume` is the real guard."""
    if promo.expires_at is not None and promo.expires_at <= utcnow():
        return PromoOutcome.EXPIRED
    if await _activated(session, promo.id, user_id):
        return PromoOutcome.ALREADY_USED
    if promo.only_new_users and not await is_new_customer(session, user_id):
        return PromoOutcome.ONLY_NEW
    if promo.max_uses > 0 and promo.uses >= promo.max_uses:
        return PromoOutcome.EXHAUSTED
    return PromoOutcome.OK


async def _activated(session: AsyncSession, promo_id: int, user_id: int) -> bool:
    stmt = select(exists().where(PromoActivation.promo_id == promo_id, PromoActivation.user_id == user_id))
    return bool(await session.scalar(stmt))


async def _lock_subscription(session: AsyncSession, user_id: int) -> None:
    """Row-lock the user's subscription (if any) before anything locks the user row - see the module docstring.

    A missing row is not locked: a locking read that finds nothing takes an InnoDB gap lock, and two new users
    locking the same gap and then inserting their subscriptions deadlock (``grant_days`` creates it via UNIQUE).
    """
    sub_id = await session.scalar(select(Subscription.id).where(Subscription.user_id == user_id))
    if sub_id is not None:
        await session.execute(select(Subscription.id).where(Subscription.id == sub_id).with_for_update())


async def _consume(session: AsyncSession, promo: PromoCode, user_id: int) -> None:
    """Take one use, credit money and record the activation. Raises :class:`_Abort` to roll back."""
    stmt = (
        update(PromoCode)
        .where(
            PromoCode.id == promo.id,
            PromoCode.is_active.is_(True),
            (PromoCode.max_uses == 0) | (PromoCode.uses < PromoCode.max_uses),
        )
        .values(uses=PromoCode.uses + 1)
        .execution_options(synchronize_session=False)
    )
    if cast(CursorResult[Any], await session.execute(stmt)).rowcount != 1:
        raise _Abort(PromoOutcome.EXHAUSTED)
    if promo.bonus_kop > 0:  # before the activation insert: the UPDATE takes the user row lock first
        applied = await wallet.apply(session, user_id, promo.bonus_kop, TxKind.PROMO, str(promo.id))
        if applied is None:
            raise _Abort(PromoOutcome.ALREADY_USED)
    await session.execute(insert(PromoActivation).values(promo_id=promo.id, user_id=user_id, created_at=utcnow()))


async def _sync_uses(session: AsyncSession, promo: PromoCode) -> None:
    uses = await session.scalar(select(PromoCode.uses).where(PromoCode.id == promo.id))
    set_committed_value(promo, "uses", int(uses or 0))


# ---------------------------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------------------------
async def create_promo(
    session: AsyncSession,
    *,
    code: str | None = None,
    bonus_kop: int = 0,
    bonus_days: int = 0,
    max_uses: int = 1,
    only_new_users: bool = False,
    expires_at: datetime | None = None,
    created_by: int | None = None,
) -> PromoCode:
    """Create an admin promo code (random code if ``code`` is None). Audit logging is the caller's job.

    Raises ``ValueError`` for invalid parameters and :class:`PromoCodeExists` for a taken code.
    """
    if bonus_kop < 0 or bonus_days < 0 or (bonus_kop == 0 and bonus_days == 0):
        raise ValueError("a promo code needs a positive bonus_kop and/or bonus_days")
    if bonus_days > MAX_PERIOD_DAYS:
        raise ValueError(f"bonus_days must be at most {MAX_PERIOD_DAYS}")
    if max_uses < 0:
        raise ValueError("max_uses must be >= 0 (0 = unlimited)")
    if expires_at is not None and expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware")
    fixed = None
    if code is not None:
        fixed = normalize_code(code)
        if not _CODE_PATTERN.fullmatch(fixed):
            raise ValueError("code must be 3..64 letters, digits, '_' or '-'")

    def build(text: str) -> PromoCode:
        return PromoCode(
            code=text,
            kind=PromoKind.PROMO,
            bonus_kop=bonus_kop,
            bonus_days=bonus_days,
            max_uses=max_uses,
            uses=0,
            only_new_users=only_new_users,
            created_by=created_by,
            expires_at=expires_at,
            is_active=True,
        )

    if fixed is not None:
        promo = await _insert_promo(session, build, fixed)
        if promo is None:
            raise PromoCodeExists(fixed)
        return promo
    return await _insert_with_random_code(session, build, GENERATED_CODE_LENGTH)


async def _insert_promo(session: AsyncSession, build: Callable[[str], PromoCode], code: str) -> PromoCode | None:
    promo = build(code)
    try:
        async with session.begin_nested():
            session.add(promo)
            await session.flush()
    except IntegrityError:
        return None
    return promo


async def _insert_with_random_code(session: AsyncSession, build: Callable[[str], PromoCode], length: int) -> PromoCode:
    for _ in range(_MAX_CODE_ATTEMPTS):
        promo = await _insert_promo(session, build, random_code(length))
        if promo is not None:
            return promo
    raise RuntimeError("could not generate a unique promo code")


def _gift_ref_key(ref: str) -> str:
    return "gift_ref:" + sha256_hex(ref)[:40]


async def create_gift(
    session: AsyncSession,
    settings: Settings,
    buyer: User,
    plan: Plan,
    ref: str,
    *,
    allow_archived: bool = False,
) -> PromoCode:
    """Charge ``plan.price_kop`` from the buyer's wallet and issue a single-use gift code for the plan.

    Idempotent per ``ref`` (e.g. the payment id): a repeated call returns the gift created the first time.
    Enforces the same rules as :func:`app.services.subscriptions.purchase_plan` and raises the same
    :class:`~app.services.subscriptions.SubscriptionError` (nothing written): BANNED for a banned buyer, NO_PLAN
    for an archived plan, INSUFFICIENT_FUNDS if the balance is too low. Pass ``allow_archived=True`` when the gift
    was paid by a payment made for this plan (``Payment.is_gift``): like a paid purchase, it is grandfathered.
    Whether gifts are offered at all (``GIFTS_ENABLED``) is decided by the caller.
    """
    if not ref:
        raise ValueError("gift ref is required")
    if buyer.is_banned:
        raise SubscriptionError(SubErrorCode.BANNED)
    if not (plan.is_active or allow_archived):
        raise SubscriptionError(SubErrorCode.NO_PLAN)
    existing = await _gift_for_ref(session, ref, fresh=False)
    if existing is not None:
        return existing

    def build(text: str) -> PromoCode:
        return PromoCode(
            code=text,
            kind=PromoKind.GIFT,
            bonus_kop=0,
            bonus_days=plan.duration_days,
            plan_id=plan.id,
            max_uses=1,
            uses=0,
            only_new_users=False,
            created_by=buyer.tg_id,
            is_active=True,
        )

    try:
        async with session.begin_nested():
            gift = await _insert_with_random_code(session, build, GIFT_CODE_LENGTH)
            try:
                charged = await wallet.apply(session, buyer.tg_id, -plan.price_kop, TxKind.GIFT_PURCHASE, f"gift:{ref}")
            except wallet.InsufficientFunds as exc:
                raise SubscriptionError(SubErrorCode.INSUFFICIENT_FUNDS, str(exc)) from exc
            if charged is None:  # a concurrent call with the same ref committed first
                raise _Abort(PromoOutcome.ALREADY_USED)
            # Insert-only: the key is new by construction, and an UPDATE-first upsert would take an InnoDB gap
            # lock that deadlocks two concurrent gift purchases. A taken key means another call won this ref.
            if not await kv.add(session, _gift_ref_key(ref), str(gift.id)):
                raise _Abort(PromoOutcome.ALREADY_USED)
    except _Abort:
        existing = await _gift_for_ref(session, ref, fresh=True)
        if existing is None:
            raise RuntimeError(f"gift ref {ref!r} was charged but its code is unknown") from None
        return existing
    await events.track(
        session, "gift.purchased", user_id=buyer.tg_id, amount_kop=plan.price_kop, plan_id=plan.id, promo_id=gift.id
    )
    log.info("gift %s (plan %s) bought by user %s", gift.id, plan.id, buyer.tg_id)
    return gift


async def _gift_for_ref(session: AsyncSession, ref: str, *, fresh: bool) -> PromoCode | None:
    promo_id = await kv.get(session, _gift_ref_key(ref), fresh=fresh)
    if not promo_id:
        return None
    lock = {"read": True} if fresh else None
    return await session.get(PromoCode, int(promo_id), with_for_update=lock, populate_existing=fresh)


# ---------------------------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------------------------
async def list_codes(
    session: AsyncSession, *, kind: PromoKind | None = PromoKind.PROMO, active_only: bool = False, limit: int = 50
) -> list[PromoCode]:
    """Newest-first codes for the admin UI."""
    stmt = select(PromoCode).order_by(PromoCode.id.desc()).limit(max(1, min(limit, 500)))
    if kind is not None:
        stmt = stmt.where(PromoCode.kind == kind)
    if active_only:
        stmt = stmt.where(PromoCode.is_active.is_(True))
    return list((await session.scalars(stmt)).all())


async def deactivate(session: AsyncSession, promo: PromoCode) -> None:
    """Disable a code; existing activations are kept."""
    promo.is_active = False
    await session.flush()
