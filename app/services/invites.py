"""Invitation links (``/start inv_<code>``): free access for family and friends.

An invite carries an access preset: ``days`` (``None`` = unlimited), device and traffic limits. Redeeming it creates
or extends the user's subscription with a ``Purchase`` of kind INVITE. Unlimited access is a complimentary
subscription that expires on :data:`UNLIMITED_EXPIRES_AT` (shown as "no expiry"): no payment reminders, no
auto-renewal, excluded from sales analytics.

A user can redeem each invite once; uses are claimed with a conditional UPDATE so ``max_uses`` holds under
concurrency. Functions take the caller's ``session`` and never commit.
"""

from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Invite, Purchase, PurchaseKind, Subscription, SubscriptionStatus, User
from app.services import audit, events, kv, provisioning, subscriptions
from app.utils.security import random_code
from app.utils.time import MAX_PERIOD_DAYS, utcnow

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)

UNLIMITED_EXPIRES_AT = datetime(2099, 12, 31, tzinfo=UTC)
CODE_LENGTH = 10
MAX_NOTE_LENGTH = 128
DEEP_LINK_PREFIX = "inv_"
_CODE_PATTERN = re.compile(r"[A-Z0-9_-]{3,32}")
MEMBER_REMOVAL_PREFIX = "member_removed:"  # kv marker of a member whose removal is in progress
_MAX_CODE_ATTEMPTS = 5


def member_removal_key(user_id: int) -> str:
    return f"{MEMBER_REMOVAL_PREFIX}{user_id}"


async def is_member(session: AsyncSession, user_id: int) -> bool:
    """Personal mode: the user has a subscription that is not being removed."""
    if await session.scalar(select(Subscription.id).where(Subscription.user_id == user_id)) is None:
        return False
    return await kv.get(session, member_removal_key(user_id)) is None


class InviteOutcome(enum.StrEnum):
    REDEEMED = "redeemed"
    NOT_FOUND = "not_found"
    INACTIVE = "inactive"  # revoked
    EXPIRED = "expired"  # the link's validity ended
    EXHAUSTED = "exhausted"  # max_uses reached
    ALREADY_USED = "already_used"  # this user redeemed this invite before
    BANNED = "banned"


class InviteCodeExists(ValueError):
    """A custom invite code is already taken."""


@dataclass(frozen=True, slots=True)
class RedeemResult:
    outcome: InviteOutcome
    invite: Invite | None = None
    subscription: Subscription | None = None
    purchase: Purchase | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == InviteOutcome.REDEEMED


def normalize_code(code: str | None) -> str:
    """Upper-cased code without the ``inv_`` deep-link prefix."""
    text = (code or "").strip()
    if text.lower().startswith(DEEP_LINK_PREFIX):
        text = text[len(DEEP_LINK_PREFIX) :]
    return text.upper()


def deep_link_payload(invite: Invite) -> str:
    """``/start`` payload of the invite (``t.me/<bot>?start=<payload>``)."""
    return f"{DEEP_LINK_PREFIX}{invite.code}"


def is_unlimited(sub: Subscription) -> bool:
    """The subscription never expires (granted by an unlimited invite)."""
    return sub.expires_at >= UNLIMITED_EXPIRES_AT


def redeem_ref(invite_id: int) -> str:
    """``Purchase.payment_id`` marking that a user redeemed this invite."""
    return f"invite:{invite_id}"


def _validate(days: int | None, device_limit: int, traffic_limit_gb: int, max_uses: int) -> None:
    if days is not None and not 0 < days <= MAX_PERIOD_DAYS:
        raise ValueError(f"days must be 1..{MAX_PERIOD_DAYS} or None (unlimited)")
    if device_limit < 0 or traffic_limit_gb < 0 or max_uses < 0:
        raise ValueError("limits and max_uses cannot be negative")


async def _insert(session: AsyncSession, values: dict[str, Any]) -> int | None:
    try:
        async with session.begin_nested():
            result = await session.execute(insert(Invite).values(**values))
    except IntegrityError:
        return None
    key = cast(CursorResult[Any], result).inserted_primary_key
    return int(key[0]) if key else None


async def create(
    session: AsyncSession,
    created_by: int,
    *,
    days: int | None = None,
    device_limit: int = 3,
    traffic_limit_gb: int = 0,
    max_uses: int = 1,
    expires_at: datetime | None = None,
    note: str | None = None,
    code: str | None = None,
    source: str = "bot",
    ip: str | None = None,
) -> Invite:
    """Create an invite (random code unless ``code`` is given). ``max_uses=0`` means unlimited uses.

    Raises ``ValueError`` on bad parameters and :class:`InviteCodeExists` for a taken custom code.
    """
    _validate(days, device_limit, traffic_limit_gb, max_uses)
    if expires_at is not None and expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware")
    values: dict[str, Any] = {
        "created_by": created_by,
        "note": (note or "").strip()[:MAX_NOTE_LENGTH] or None,
        "max_uses": max_uses,
        "uses": 0,
        "days": days,
        "device_limit": device_limit,
        "traffic_limit_gb": traffic_limit_gb,
        "expires_at": expires_at,
        "is_active": True,
        "created_at": utcnow(),
    }
    invite_id: int | None = None
    if code is not None:
        normalized = normalize_code(code)
        if not _CODE_PATTERN.fullmatch(normalized):
            raise ValueError("code must be 3-32 characters: letters, digits, '_' or '-'")
        invite_id = await _insert(session, {**values, "code": normalized})
        if invite_id is None:
            raise InviteCodeExists(normalized)
    else:
        for _ in range(_MAX_CODE_ATTEMPTS):
            invite_id = await _insert(session, {**values, "code": random_code(CODE_LENGTH)})
            if invite_id is not None:
                break
        if invite_id is None:  # pragma: no cover - 31^10 codes
            raise RuntimeError("could not allocate a unique invite code")
    invite = await session.get(Invite, invite_id)
    assert invite is not None
    await audit.log(
        session,
        created_by,
        "invite.create",
        "invite",
        invite.id,
        source,
        ip,
        days=days,
        device_limit=device_limit,
        traffic_limit_gb=traffic_limit_gb,
        max_uses=max_uses,
    )
    return invite


async def get_by_code(session: AsyncSession, code: str | None) -> Invite | None:
    normalized = normalize_code(code)
    if not normalized:
        return None
    return await session.scalar(select(Invite).where(Invite.code == normalized))


async def list_invites(
    session: AsyncSession, *, active_only: bool = False, limit: int = 50, offset: int = 0
) -> list[Invite]:
    """Invites, newest first."""
    stmt = select(Invite).order_by(Invite.id.desc()).limit(max(1, min(limit, 200))).offset(max(0, offset))
    if active_only:
        stmt = stmt.where(Invite.is_active.is_(True))
    return list((await session.scalars(stmt)).all())


async def revoke(
    session: AsyncSession, invite_id: int, actor_id: int, *, source: str = "bot", ip: str | None = None
) -> bool:
    """Deactivate an invite (already granted access stays). False if it was not active."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Invite)
            .where(Invite.id == invite_id, Invite.is_active.is_(True))
            .values(is_active=False)
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount != 1:
        return False
    await audit.log(session, actor_id, "invite.revoke", "invite", invite_id, source, ip)
    return True


async def redeemed_by(session: AsyncSession, invite_id: int) -> list[int]:
    """Users who redeemed the invite."""
    stmt = select(Purchase.user_id).where(
        Purchase.kind == PurchaseKind.INVITE, Purchase.payment_id == redeem_ref(invite_id)
    )
    return list((await session.scalars(stmt.order_by(Purchase.id))).all())


def _generous(current: int, offered: int) -> int:
    """The more generous of two limits where 0 means unlimited."""
    if current == 0 or offered == 0:
        return 0
    return max(current, offered)


async def _already_redeemed(session: AsyncSession, user_id: int, invite_id: int) -> bool:
    stmt = (
        select(Purchase.id)
        .where(
            Purchase.user_id == user_id,
            Purchase.kind == PurchaseKind.INVITE,
            Purchase.payment_id == redeem_ref(invite_id),
        )
        .with_for_update(read=True)
    )
    return await session.scalar(stmt) is not None


async def _claim(session: AsyncSession, invite_id: int, now: datetime) -> bool:
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Invite)
            .where(
                Invite.id == invite_id,
                Invite.is_active.is_(True),
                or_(Invite.expires_at.is_(None), Invite.expires_at > now),
                or_(Invite.max_uses == 0, Invite.uses < Invite.max_uses),
            )
            .values(uses=Invite.uses + 1)
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount == 1


async def redeem(
    session: AsyncSession, settings: Settings, user: User, code: str | None, *, now: datetime | None = None
) -> RedeemResult:
    """Give the invite's access to ``user``: create the subscription or extend it.

    Unlimited invites make the subscription complimentary until :data:`UNLIMITED_EXPIRES_AT`; invites with days
    add them to ``max(now, expires_at)``. Limits: a new (or trial) subscription gets the invite's; an existing one
    keeps the more generous of both. Auto-renewal is turned off (nothing to pay for).
    """
    now = (now or utcnow()).replace(microsecond=0)
    invite = await get_by_code(session, code)
    if invite is None:
        return RedeemResult(InviteOutcome.NOT_FOUND)
    if not invite.is_active:
        return RedeemResult(InviteOutcome.INACTIVE, invite)
    if invite.expires_at is not None and invite.expires_at <= now:
        return RedeemResult(InviteOutcome.EXPIRED, invite)
    if user.is_banned:
        return RedeemResult(InviteOutcome.BANNED, invite)
    sub = await subscriptions.lock_for_user(session, user.tg_id)
    if await _already_redeemed(session, user.tg_id, invite.id):
        return RedeemResult(InviteOutcome.ALREADY_USED, invite)
    if not await _claim(session, invite.id, now):
        await session.refresh(invite)
        if not invite.is_active:
            return RedeemResult(InviteOutcome.INACTIVE, invite)
        if invite.expires_at is not None and invite.expires_at <= now:
            return RedeemResult(InviteOutcome.EXPIRED, invite)
        return RedeemResult(InviteOutcome.EXHAUSTED, invite)

    created = False
    if sub is None:
        sub, created = await subscriptions.create_for_user(session, user.tg_id, now)
    expires_before = None if created else sub.expires_at
    base = max(now, sub.expires_at)
    fresh_limits = created or sub.is_trial or sub.expires_at <= now
    if invite.days is None:
        new_expiry = UNLIMITED_EXPIRES_AT
        sub.is_complimentary = True
    else:
        new_expiry = min(base + timedelta(days=invite.days), UNLIMITED_EXPIRES_AT)
    granted_days = max(1, (new_expiry - base).days) if new_expiry > base else 0
    sub.expires_at = max(sub.expires_at, new_expiry)
    if fresh_limits:
        sub.device_limit, sub.traffic_limit_gb = invite.device_limit, invite.traffic_limit_gb
    else:
        sub.device_limit = _generous(sub.device_limit, invite.device_limit)
        sub.traffic_limit_gb = _generous(sub.traffic_limit_gb, invite.traffic_limit_gb)
    sub.is_trial = False
    sub.auto_renew = False
    if sub.status != SubscriptionStatus.DISABLED:
        sub.status = SubscriptionStatus.ACTIVE
    sub.last_reminder_days = None
    if not created:
        sub.revision += 1
    purchase = Purchase(
        user_id=user.tg_id,
        plan_id=None,
        kind=PurchaseKind.INVITE,
        price_kop=0,
        days=granted_days,
        expires_before=expires_before,
        expires_after=sub.expires_at,
        payment_id=redeem_ref(invite.id),
        actor_id=invite.created_by,
        created_at=now,
    )
    session.add(purchase)
    await session.flush()
    await provisioning.ensure_provisions(session, sub)
    await events.track(
        session,
        "invite.redeemed",
        user_id=user.tg_id,
        invite_id=invite.id,
        days=invite.days,
        unlimited=invite.days is None,
    )
    await session.refresh(invite)
    log.info("invite %s redeemed by user %s", invite.id, user.tg_id)
    return RedeemResult(InviteOutcome.REDEEMED, invite, sub, purchase)
