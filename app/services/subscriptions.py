"""Subscriptions: trials, purchases, grants, auto-renewal, expiry and expiry reminders.

Every subscription has its own client identity, public token and expiry. Extending always adds days to
``max(now, expires_at)``. Every change that affects panels
bumps ``Subscription.revision`` and ensures provision rows exist; :mod:`app.services.provisioning` pushes
the change to the panels outside of any transaction.

Functions taking ``session`` run inside the caller's transaction and never commit; functions taking
``session_factory`` manage their own short transactions.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager
from sqlalchemy.orm.attributes import set_committed_value

from app.config import Settings
from app.db.models import (
    Plan,
    Purchase,
    PurchaseKind,
    Subscription,
    SubscriptionStatus,
    TxKind,
    User,
)
from app.db.session import SessionFactory, read_session
from app.services import audit, events, provisioning, wallet
from app.utils.security import random_token
from app.utils.time import MAX_PERIOD_DAYS, days_left, utcnow

log = logging.getLogger(__name__)

# How long after expiry auto-renew still kicks in (e.g. the balance was topped up a few hours late).
AUTO_RENEW_GRACE = timedelta(hours=24)
# A subscription renewed by Telegram Stars whose period ended this long ago without a renewal charge is treated as
# lapsed (canceled in Telegram, or the charge failed - the bot gets no update for either): the wallet renews it.
# Must stay well below AUTO_RENEW_GRACE.
STARS_LAPSE_GRACE = timedelta(hours=3)
# Subscriptions that expired longer ago than this get no "expired" notice (e.g. right after a migration).
EXPIRED_NOTICE_MAX_AGE = timedelta(days=3)
EXPIRED_NOTICE = 0  # ``last_reminder_days`` value meaning "the expiry notice was sent"

_RENEWABLE_STATUSES = (SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRED)
_GRANT_KINDS = frozenset(
    {PurchaseKind.GIFT_REDEEM, PurchaseKind.PROMO, PurchaseKind.ADMIN_GRANT, PurchaseKind.REFERRAL_REWARD}
)
_BUY_KINDS = frozenset({PurchaseKind.PURCHASE, PurchaseKind.AUTO_RENEW})
_EVENT_BY_KIND = {
    PurchaseKind.TRIAL: "trial.started",
    PurchaseKind.PURCHASE: "purchase",
    PurchaseKind.AUTO_RENEW: "renewal",
}
_GRANT_EVENT = "subscription.granted"


class SubErrorCode(StrEnum):
    NO_PLAN = "no_plan"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    TRIAL_USED = "trial_used"
    TRIAL_DISABLED = "trial_disabled"
    BANNED = "banned"
    DISABLED = "disabled"


class SubscriptionError(Exception):
    """A business rule refused the operation; nothing was written. ``code`` says why."""

    def __init__(self, code: SubErrorCode, message: str | None = None) -> None:
        super().__init__(message or code.value)
        self.code = code


class RenewOutcome(StrEnum):
    """Per-subscription result of :func:`run_auto_renew`."""

    RENEWED = "renewed"
    ALREADY_RENEWED = "already_renewed"  # this period was charged before (ledger idempotency)
    NOT_DUE = "not_due"  # changed between selection and renewal (renewed manually, auto-renew off...)
    INSUFFICIENT_FUNDS = SubErrorCode.INSUFFICIENT_FUNDS.value
    NO_PLAN = SubErrorCode.NO_PLAN.value
    BANNED = SubErrorCode.BANNED.value
    DISABLED = SubErrorCode.DISABLED.value
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class _Period:
    """What is being added to a subscription."""

    days: int
    kind: PurchaseKind
    plan: Plan | None = None
    price_kop: int = 0
    payment_id: str | None = None
    actor_id: int | None = None


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------
async def get_for_user(session: AsyncSession, user_id: int, subscription_id: int | None = None) -> Subscription | None:
    """An owned subscription; old callers address the oldest subscription deterministically."""
    stmt = select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.id).limit(1)
    if subscription_id is not None:
        stmt = stmt.where(Subscription.id == subscription_id)
    return await session.scalar(stmt)


async def list_for_user(session: AsyncSession, user_id: int) -> list[Subscription]:
    return list(
        await session.scalars(select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.id))
    )


def token_for(sub: Subscription | None, user: User) -> str:
    return sub.sub_token if sub is not None and sub.sub_token else user.sub_token


async def resolve_token(session: AsyncSession, token: str) -> tuple[User, Subscription | None] | None:
    sub = await session.scalar(select(Subscription).where(Subscription.sub_token == token))
    if sub is not None:
        user = await session.get(User, sub.user_id)
        return (user, sub) if user is not None else None
    user = await session.scalar(select(User).where(User.sub_token == token))
    if user is None:
        return None
    sub = await get_for_user(session, user.tg_id)
    # A rotated subscription token must never resolve through the old user token.
    if sub is not None and sub.sub_token is not None:
        return None
    return user, sub


async def rotate_token(session: AsyncSession, user: User, sub: Subscription) -> str:
    sub.sub_token = random_token(24)
    primary_id = await session.scalar(
        select(Subscription.id).where(Subscription.user_id == user.tg_id).order_by(Subscription.id).limit(1)
    )
    if primary_id == sub.id:
        user.sub_token = sub.sub_token
    await session.flush()
    return sub.sub_token


async def _lock_for_user(session: AsyncSession, user_id: int) -> Subscription | None:
    """Row-lock and reload the user's subscription; None (and nothing locked) if they have none yet.

    Existence is checked with a plain read first: a locking read that finds no row takes an InnoDB gap lock, and two
    new users whose transactions lock the same gap and then insert their subscriptions deadlock. A missing row is
    created by :func:`_create`, which relies on ``UNIQUE(user_id)`` instead of a lock.
    """
    if await session.scalar(select(Subscription.id).where(Subscription.user_id == user_id)) is None:
        return None
    return await _lock_row(session, user_id)


async def _lock_row(session: AsyncSession, user_id: int, subscription_id: int | None = None) -> Subscription | None:
    """``SELECT ... FOR UPDATE`` the user's subscription; only for a row known to exist (see :func:`_lock_for_user`).

    A current read: it also sees a row committed after a REPEATABLE READ snapshot. The lock is a no-op on SQLite.
    """
    stmt = (
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.id)
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if subscription_id is not None:
        stmt = stmt.where(Subscription.id == subscription_id)
    return await session.scalar(stmt)


async def lock_for_user(session: AsyncSession, user_id: int, subscription_id: int | None = None) -> Subscription | None:
    """Row-lock the user's subscription for an update in the caller's transaction; None if they have none yet.

    Use :func:`create_for_user` to add a missing one (it relies on ``UNIQUE(user_id)``, not on a lock).
    """
    if subscription_id is None:
        return await _lock_for_user(session, user_id)
    if await get_for_user(session, user_id, subscription_id) is None:
        return None
    return await _lock_row(session, user_id, subscription_id)


async def create_for_user(
    session: AsyncSession, user_id: int, now: datetime, *, new_subscription: bool = False, label: str | None = None
) -> tuple[Subscription, bool]:
    """Insert an empty subscription (expires ``now``, no limits) for the user, or lock one created concurrently.

    Returns ``(subscription, created)``. The caller sets the period and limits and calls
    :func:`provisioning.ensure_provisions`; nothing is committed here.
    """
    return await _create(session, user_id, now, new_subscription=new_subscription, label=label)


def renewal_ref(sub: Subscription) -> str:
    """Ledger ref of the auto-renewal of the period ending at ``sub.expires_at`` (one charge per period)."""
    expires = sub.expires_at.astimezone(UTC).replace(microsecond=0)
    return f"renew:{sub.id}:{expires.isoformat()}"


# ---------------------------------------------------------------------------------------------
# Extending
# ---------------------------------------------------------------------------------------------
def _ensure_allowed(user: User) -> None:
    if user.is_banned:
        raise SubscriptionError(SubErrorCode.BANNED)


async def _create(
    session: AsyncSession, user_id: int, now: datetime, *, new_subscription: bool = False, label: str | None = None
) -> tuple[Subscription, bool]:
    """The primary token makes concurrent first creation unique without locking a missing InnoDB row."""
    user = await session.get(User, user_id)
    if user is None:
        raise LookupError(f"user {user_id} not found")
    existing = await get_for_user(session, user_id)
    if existing is not None:
        existing = await _lock_row(session, user_id, existing.id)
    if existing is not None and not new_subscription:
        return existing, False
    sub = Subscription(
        user_id=user_id,
        label=(label or "Подписка").strip()[:64] or "Подписка",
        sub_token=random_token(24) if existing is not None else user.sub_token,
        is_legacy=existing is None,
        client_uuid=str(uuid4()),
        status=SubscriptionStatus.ACTIVE,
        expires_at=now.replace(microsecond=0),
        device_limit=0,
        traffic_limit_gb=0,
        auto_renew=True,
        is_trial=False,
        revision=1,
    )
    try:
        async with session.begin_nested():
            session.add(sub)
    except IntegrityError:
        # Created concurrently, possibly after this transaction's snapshot: lock it with a current read.
        existing = await _lock_row(session, user_id)
        if existing is None:
            raise
        if new_subscription:
            sub.sub_token = random_token(24)
            sub.is_legacy = False
            async with session.begin_nested():
                session.add(sub)
            return sub, True
        return existing, False
    return sub, True


async def _default_limits(session: AsyncSession, settings: Settings) -> tuple[int, int]:
    """Limits for a subscription created without a plan: the first active plan's, else the trial's."""
    plan = await session.scalar(
        select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.id).limit(1)
    )
    if plan is not None:
        return plan.device_limit, plan.traffic_limit_gb
    return settings.trial_device_limit, settings.trial_traffic_gb


async def _apply_limits(
    session: AsyncSession, settings: Settings, sub: Subscription, period: _Period, *, created: bool
) -> None:
    if period.kind == PurchaseKind.TRIAL:
        sub.device_limit, sub.traffic_limit_gb = settings.trial_device_limit, settings.trial_traffic_gb
        sub.is_trial = True
    elif period.plan is not None:
        sub.device_limit, sub.traffic_limit_gb = period.plan.device_limit, period.plan.traffic_limit_gb
        sub.traffic_limit_bytes = None
        sub.plan_id = period.plan.id
        sub.is_trial = False
    elif created:
        sub.device_limit, sub.traffic_limit_gb = await _default_limits(session, settings)
    # A grant without a plan keeps the current limits (and trial flag) of an existing subscription.


async def _track(session: AsyncSession, sub: Subscription, period: _Period) -> None:
    event = _EVENT_BY_KIND.get(period.kind, _GRANT_EVENT)
    await events.track(
        session,
        event,
        user_id=sub.user_id,
        amount_kop=period.price_kop or None,
        kind=period.kind.value,
        days=period.days,
        plan_id=period.plan.id if period.plan else None,
        payment_id=period.payment_id,
        expires_at=sub.expires_at.isoformat(),
    )


async def _extend(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    period: _Period,
    now: datetime,
    sub: Subscription | None,
    *,
    created: bool = False,
) -> tuple[Subscription, Purchase]:
    """Add ``period`` to the (locked) subscription, creating it if needed; record a Purchase.

    ``created`` says ``sub`` was just inserted by :func:`_create`. New expiry = ``max(now, expires_at) + days``. An
    admin-disabled subscription keeps its status. A period with a plan starts a new traffic period
    (``traffic_epoch``): used traffic is reset on the panels.
    """
    if sub is None:
        sub, created = await _create(session, user_id, now)
    expires_before = None if created else sub.expires_at
    base = max(now, sub.expires_at)
    sub.expires_at = (base + timedelta(days=period.days)).replace(microsecond=0)
    await _apply_limits(session, settings, sub, period, created=created)
    if sub.status != SubscriptionStatus.DISABLED:
        sub.status = SubscriptionStatus.ACTIVE
    sub.last_reminder_days = None
    if not created:
        sub.revision += 1
        if period.plan is not None:
            # Plan traffic is a per-period quota: a new paid period resets the panels' usage counters
            # (provisioning compares Provision.traffic_epoch). Free days without a plan keep the period.
            sub.traffic_epoch += 1
    purchase = Purchase(
        user_id=user_id,
        subscription_id=sub.id,
        plan_id=period.plan.id if period.plan else None,
        kind=period.kind,
        price_kop=period.price_kop,
        days=period.days,
        expires_before=expires_before,
        expires_after=sub.expires_at,
        payment_id=period.payment_id,
        actor_id=period.actor_id,
    )
    session.add(purchase)
    await session.flush()
    await provisioning.ensure_provisions(session, sub)
    await _track(session, sub, period)
    return sub, purchase


async def _charge(session: AsyncSession, user_id: int, amount_kop: int, ref: str) -> bool:
    """Charge the wallet once per ``ref``. False if this ref was charged before."""
    try:
        applied = await wallet.apply(session, user_id, -amount_kop, TxKind.PURCHASE, ref)
    except wallet.InsufficientFunds as exc:
        raise SubscriptionError(SubErrorCode.INSUFFICIENT_FUNDS, str(exc)) from exc
    return applied is not None


async def _buy(
    session: AsyncSession,
    settings: Settings,
    user: User,
    plan: Plan | None,
    *,
    kind: PurchaseKind,
    ref: str,
    payment_id: str | None,
    now: datetime,
    subscription_id: int | None = None,
    new_subscription: bool = False,
    label: str | None = None,
    price_kop: int | None = None,
) -> Purchase | None:
    """Validate, charge the plan price from the wallet and extend. None if ``ref`` was charged before.

    Archived plans can still be bought by a payment made for them and renewed (grandfathered).
    """
    _ensure_allowed(user)
    allow_archived = payment_id is not None or kind == PurchaseKind.AUTO_RENEW
    if plan is None or not (plan.is_active or allow_archived):
        raise SubscriptionError(SubErrorCode.NO_PLAN)
    if new_subscription and subscription_id is not None:
        raise ValueError("choose a new subscription or an existing subscription")
    sub = None if new_subscription else await lock_for_user(session, user.tg_id, subscription_id)
    if subscription_id is not None and sub is None:
        raise SubscriptionError(SubErrorCode.NO_PLAN)
    if sub is not None and sub.status == SubscriptionStatus.DISABLED:
        raise SubscriptionError(SubErrorCode.DISABLED)
    price = plan.price_kop if price_kop is None else price_kop
    if isinstance(price, bool) or not isinstance(price, int) or not 0 <= price <= plan.price_kop:
        raise ValueError("price_kop must be an integer discount from the current plan price")
    if not await _charge(session, user.tg_id, price, ref):
        return None
    created = False
    if new_subscription:
        sub, created = await _create(session, user.tg_id, now, new_subscription=True, label=label)
    if sub is not None and stars_lapsed(sub, now):
        # The Stars subscription was canceled or a charge failed (Telegram sends no update for either) and this
        # period is paid otherwise: wallet auto-renew takes over. A late Stars renewal links it again.
        sub.stars_charge_id = None
    period = _Period(plan.duration_days, kind, plan, price, payment_id)
    _, purchase = await _extend(session, settings, user.tg_id, period, now, sub, created=created)
    if price < plan.price_kop:
        from app.services import winback  # local: winback imports this module's models only, keep startup light

        await winback.consume(session, settings, user.tg_id, now)
    from app.services import referrals  # local: referrals grants reward days through this module

    await referrals.on_invitee_purchase(session, settings, purchase)  # REFERRAL_LEVELS_PERCENT
    return purchase


# ---------------------------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------------------------
async def start_trial(
    session: AsyncSession, settings: Settings, user: User, *, now: datetime | None = None
) -> Subscription:
    """Give the trial (TRIAL_DAYS with trial limits) once per user, and only to users without a subscription.

    Raises :class:`SubscriptionError` with TRIAL_DISABLED, BANNED or TRIAL_USED.
    """
    if settings.trial_days <= 0:
        raise SubscriptionError(SubErrorCode.TRIAL_DISABLED)
    _ensure_allowed(user)
    if user.trial_used or await _lock_for_user(session, user.tg_id) is not None:
        raise SubscriptionError(SubErrorCode.TRIAL_USED)
    now = now or utcnow()
    # Claim and creation share a SAVEPOINT, so a refusal below also rolls the claim back (nothing is written).
    async with session.begin_nested():
        # Atomic claim: two concurrent requests cannot both flip the flag.
        claimed = await session.execute(
            update(User).where(User.tg_id == user.tg_id, User.trial_used.is_(False)).values(trial_used=True)
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            raise SubscriptionError(SubErrorCode.TRIAL_USED)
        sub, created = await _create(session, user.tg_id, now)
        if not created:
            # A first purchase committed after the check above (a missing row locks nothing): the trial is only for
            # users without a subscription and must not put trial limits on the paid one.
            raise SubscriptionError(SubErrorCode.TRIAL_USED)
    user.trial_used = True
    period = _Period(settings.trial_days, PurchaseKind.TRIAL)
    sub, _ = await _extend(session, settings, user.tg_id, period, now, sub, created=True)
    return sub


async def purchase_plan(
    session: AsyncSession,
    settings: Settings,
    user: User,
    plan: Plan,
    *,
    kind: PurchaseKind = PurchaseKind.PURCHASE,
    payment_id: str | None = None,
    now: datetime | None = None,
    subscription_id: int | None = None,
    new_subscription: bool = False,
    label: str | None = None,
    price_kop: int | None = None,
) -> Purchase:
    """Buy ``plan`` from the wallet and extend the subscription; the plan's limits replace the current ones.

    Ledger ref is ``f"purchase:{payment_id or uuid4().hex}"``: buying for the same payment twice charges once
    and returns the original Purchase. Raises :class:`SubscriptionError` (BANNED, NO_PLAN, DISABLED,
    INSUFFICIENT_FUNDS); nothing is written then.
    """
    if kind not in _BUY_KINDS:
        raise ValueError(f"purchase_plan does not handle {kind}")
    ref = f"purchase:{payment_id or uuid4().hex}"
    purchase = await _buy(
        session,
        settings,
        user,
        plan,
        kind=kind,
        ref=ref,
        payment_id=payment_id,
        now=now or utcnow(),
        subscription_id=subscription_id,
        new_subscription=new_subscription,
        label=label,
        price_kop=price_kop,
    )
    if purchase is not None:
        return purchase
    existing = await session.scalar(
        select(Purchase)
        .where(Purchase.user_id == user.tg_id, Purchase.payment_id == payment_id)
        .order_by(Purchase.id)
        .limit(1)
    )
    if existing is None:
        raise RuntimeError(f"ledger has {ref} for user {user.tg_id} but no matching purchase")
    return existing


async def grant_days(
    session: AsyncSession,
    settings: Settings,
    user: User,
    days: int,
    *,
    kind: PurchaseKind,
    plan: Plan | None = None,
    actor_id: int | None = None,
    now: datetime | None = None,
    subscription_id: int | None = None,
) -> Purchase:
    """Add free days (admin grant, promo code, gift redeem). With ``plan`` its limits are applied.

    A new subscription without a plan gets the first active plan's limits. Works on admin-disabled
    subscriptions too (the days are added, the block stays). Writes an audit record when ``actor_id`` is set.
    """
    if not 0 < days <= MAX_PERIOD_DAYS:
        raise ValueError(f"days must be 1..{MAX_PERIOD_DAYS}")
    if kind not in _GRANT_KINDS:
        raise ValueError(f"grant_days does not handle {kind}")
    sub = await lock_for_user(session, user.tg_id, subscription_id)
    if subscription_id is not None and sub is None:
        raise SubscriptionError(SubErrorCode.NO_PLAN)
    period = _Period(days, kind, plan, 0, None, actor_id)
    sub, purchase = await _extend(session, settings, user.tg_id, period, now or utcnow(), sub)
    if actor_id is not None:
        await audit.log(
            session,
            actor_id,
            "subscription.grant_days",
            "user",
            user.tg_id,
            days=days,
            kind=kind.value,
            plan_id=plan.id if plan else None,
            expires_at=sub.expires_at.isoformat(),
        )
    return purchase


async def set_status(
    session: AsyncSession,
    sub: Subscription,
    status: SubscriptionStatus,
    actor_id: int,
    *,
    now: datetime | None = None,
) -> SubscriptionStatus:
    """Admin block/unblock. Re-activating a subscription past its expiry makes it EXPIRED.

    Returns the effective status. Writes an audit record when the status changes.
    """
    if status == SubscriptionStatus.ACTIVE and sub.expires_at <= (now or utcnow()):
        status = SubscriptionStatus.EXPIRED
    old = sub.status
    if status == old:
        return status
    sub.status = status
    sub.revision += 1
    await provisioning.ensure_provisions(session, sub)
    await audit.log(session, actor_id, "subscription.set_status", "user", sub.user_id, old=old.value, new=status.value)
    return status


async def set_auto_renew(session: AsyncSession, sub: Subscription, on: bool) -> None:
    """Toggle auto-renewal from the wallet (does not affect panels)."""
    sub.auto_renew = on


# ---------------------------------------------------------------------------------------------
# Auto-renewal
# ---------------------------------------------------------------------------------------------
def _renew_window(settings: Settings, now: datetime) -> tuple[datetime, datetime]:
    """``(after, until)``: subscriptions expiring in ``(after, until]`` are due for auto-renewal."""
    return now - AUTO_RENEW_GRACE, now + timedelta(hours=max(0, settings.auto_renew_hours_before))


def stars_lapsed(sub: Subscription, now: datetime) -> bool:
    """Linked to a Stars subscription that missed its renewal by :data:`STARS_LAPSE_GRACE`."""
    return sub.stars_charge_id is not None and sub.expires_at <= now - STARS_LAPSE_GRACE


def _is_renewable(sub: Subscription, settings: Settings, now: datetime) -> bool:
    after, until = _renew_window(settings, now)
    return (
        sub.auto_renew
        and not sub.is_complimentary
        and (sub.stars_charge_id is None or stars_lapsed(sub, now))
        and sub.plan_id is not None
        and sub.status in _RENEWABLE_STATUSES
        and after < sub.expires_at <= until
    )


async def due_for_auto_renew(session: AsyncSession, settings: Settings, now: datetime) -> list[Subscription]:
    """Subscriptions to renew now: auto-renew on, a plan to renew, inside the window, enough balance.

    The window is ``AUTO_RENEW_HOURS_BEFORE`` before expiry up to :data:`AUTO_RENEW_GRACE` after it.
    Subscriptions paid by Telegram Stars recurring payments (until the Stars subscription lapses, see
    :data:`STARS_LAPSE_GRACE`; the renewal then unlinks it), complimentary subscriptions, banned users and trials
    are excluded.
    """
    after, until = _renew_window(settings, now)
    stmt = (
        select(Subscription)
        .join(User, Subscription.user_id == User.tg_id)
        .join(Plan, Subscription.plan_id == Plan.id)
        .where(
            Subscription.auto_renew.is_(True),
            Subscription.is_complimentary.is_(False),
            or_(Subscription.stars_charge_id.is_(None), Subscription.expires_at <= now - STARS_LAPSE_GRACE),
            Subscription.status.in_(_RENEWABLE_STATUSES),
            Subscription.expires_at > after,
            Subscription.expires_at <= until,
            User.is_banned.is_(False),
            User.balance_kop >= Plan.price_kop,
        )
        .order_by(Subscription.expires_at, Subscription.id)
    )
    return list((await session.scalars(stmt)).all())


async def _renew_one(
    session: AsyncSession, settings: Settings, sub_id: int, now: datetime
) -> tuple[Purchase | None, RenewOutcome]:
    stmt = (
        select(Subscription)
        .where(Subscription.id == sub_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    sub = await session.scalar(stmt)
    if sub is None or not _is_renewable(sub, settings, now):
        return None, RenewOutcome.NOT_DUE
    user = await session.get(User, sub.user_id)
    plan = await session.get(Plan, sub.plan_id) if sub.plan_id is not None else None
    if user is None:
        return None, RenewOutcome.NOT_DUE
    try:
        purchase = await _buy(
            session,
            settings,
            user,
            plan,
            kind=PurchaseKind.AUTO_RENEW,
            ref=renewal_ref(sub),
            payment_id=None,
            now=now,
            subscription_id=sub.id,
        )
    except SubscriptionError as exc:
        return None, RenewOutcome(exc.code.value)
    if purchase is None:
        return None, RenewOutcome.ALREADY_RENEWED
    return purchase, RenewOutcome.RENEWED


def renew_window(settings: Settings, now: datetime) -> tuple[datetime, datetime]:
    """``(after, until)``: subscriptions expiring in ``(after, until]`` are due for auto-renewal."""
    return _renew_window(settings, now)


def is_renewable(sub: Subscription, settings: Settings, now: datetime) -> bool:
    """Auto-renewal applies to ``sub`` now (the balance is not checked)."""
    return _is_renewable(sub, settings, now)


async def renew_from_wallet(
    session: AsyncSession, settings: Settings, sub_id: int, now: datetime
) -> tuple[Purchase | None, RenewOutcome]:
    """Auto-renew one subscription from the wallet inside the caller's transaction (ledger ref :func:`renewal_ref`).

    Used when a saved-card charge for the renewal was credited, so the period is charged at most once whichever
    of the card payment and the auto-renew job gets there first.
    """
    return await _renew_one(session, settings, sub_id, now)


async def run_auto_renew(
    session_factory: SessionFactory, settings: Settings, now: datetime
) -> list[tuple[int, Purchase | None, str]]:
    """Renew every due subscription from its wallet, one short transaction each.

    Idempotent: the ledger ref :func:`renewal_ref` is unique per (subscription, period), so a period is never
    charged twice even if two schedulers race. Returns ``(user_id, purchase, RenewOutcome)`` per attempt.
    """
    async with session_factory.begin() as session:
        due = [(sub.id, sub.user_id) for sub in await due_for_auto_renew(session, settings, now)]
    results: list[tuple[int, Purchase | None, str]] = []
    for sub_id, user_id in due:
        try:
            async with session_factory.begin() as session:
                purchase, outcome = await _renew_one(session, settings, sub_id, now)
        except Exception:
            log.exception("auto-renew of subscription %s failed", sub_id)
            purchase, outcome = None, RenewOutcome.ERROR
        if outcome == RenewOutcome.RENEWED:
            log.info("auto-renewed subscription %s of user %s", sub_id, user_id)
        results.append((user_id, purchase, outcome))
    return results


# ---------------------------------------------------------------------------------------------
# Expiry & reminders
# ---------------------------------------------------------------------------------------------
async def expire_due(session_factory: SessionFactory, now: datetime) -> list[int]:
    """Mark ACTIVE subscriptions with ``expires_at <= now`` as EXPIRED (bumping revision); return user ids."""
    async with session_factory.begin() as session:
        stmt = (
            select(Subscription)
            .where(Subscription.status == SubscriptionStatus.ACTIVE, Subscription.expires_at <= now)
            .order_by(Subscription.id)
            .with_for_update()
        )
        subs = list((await session.scalars(stmt)).all())
        for sub in subs:
            sub.status = SubscriptionStatus.EXPIRED
            sub.revision += 1
            await events.track(
                session, "subscription.expired", user_id=sub.user_id, plan_id=sub.plan_id, was_trial=sub.is_trial
            )
        return [sub.user_id for sub in subs]


def _reminder_thresholds(settings: Settings) -> list[int]:
    return sorted({days for days in settings.expiry_reminder_days if days > 0})


def reminder_for(sub: Subscription, thresholds: list[int], now: datetime) -> int | None:
    """Days-left value to remind about now, or None. 0 = the "expired" notice.

    ``thresholds`` ascending. Before expiry the reminder is due when the smallest threshold >= days left is
    below the last reminder sent for this period (so each threshold fires once, and missed ones collapse).
    """
    last = sub.last_reminder_days
    if sub.expires_at <= now:
        return EXPIRED_NOTICE if last != EXPIRED_NOTICE else None
    left = days_left(sub.expires_at, now)
    bucket = next((threshold for threshold in thresholds if left <= threshold), None)
    if bucket is None or (last is not None and bucket >= last):
        return None
    return left


async def reminders_due(session: AsyncSession, settings: Settings, now: datetime) -> list[tuple[Subscription, int]]:
    """``(subscription, days_left)`` pairs to notify now; ``sub.user`` is loaded. 0 = expired notice.

    Pre-expiry reminders follow ``EXPIRY_REMINDER_DAYS``; the expired notice is always sent once per period
    (not for subscriptions that expired more than :data:`EXPIRED_NOTICE_MAX_AGE` ago). Admin-disabled
    subscriptions, banned users and users who blocked the bot are skipped. Send with no transaction open, then
    call :func:`mark_reminded` in a short one (:func:`send_reminders` does exactly that).
    """
    thresholds = _reminder_thresholds(settings)
    horizon = now + timedelta(days=max(thresholds, default=0))
    stmt = (
        select(Subscription)
        .join(Subscription.user)
        .options(contains_eager(Subscription.user))
        .where(
            Subscription.status.in_(_RENEWABLE_STATUSES),
            Subscription.expires_at <= horizon,
            Subscription.expires_at > now - EXPIRED_NOTICE_MAX_AGE,
            or_(Subscription.last_reminder_days.is_(None), Subscription.last_reminder_days > EXPIRED_NOTICE),
            User.is_banned.is_(False),
            User.bot_blocked.is_(False),
        )
        .order_by(Subscription.expires_at, Subscription.id)
    )
    due: list[tuple[Subscription, int]] = []
    for sub in (await session.scalars(stmt)).all():
        days = reminder_for(sub, thresholds, now)
        if days is not None:
            due.append((sub, days))
    return due


async def mark_reminded(session: AsyncSession, sub: Subscription, days: int) -> bool:
    """Record that the reminder returned by :func:`reminders_due` was sent (reset on every extension).

    ``sub`` may come from an earlier, already closed session. The write is conditional: it is a no-op (False)
    if the period changed meanwhile (a renewal moved ``expires_at`` and reset the reminders) or a reminder
    closer to expiry is already recorded.
    """
    if days < 0:
        raise ValueError("days must be >= 0")
    result = await session.execute(
        update(Subscription)
        .where(
            Subscription.id == sub.id,
            Subscription.expires_at == sub.expires_at,
            or_(Subscription.last_reminder_days.is_(None), Subscription.last_reminder_days > days),
        )
        .values(last_reminder_days=days)
        .execution_options(synchronize_session=False)
    )
    recorded = bool(getattr(result, "rowcount", 0))
    if recorded:
        set_committed_value(sub, "last_reminder_days", days)
    return recorded


ReminderSender = Callable[[Subscription, int], Awaitable[bool]]


async def send_reminders(
    session_factory: SessionFactory, settings: Settings, send: ReminderSender, now: datetime | None = None
) -> int:
    """Send every due reminder; returns how many were delivered and recorded.

    The due list is read first; each ``send(sub, days_left)`` (``sub.user`` loaded) runs with no transaction
    open and returns True once delivered; then the reminder is recorded in a short transaction of its own.
    Undelivered or failed sends are retried on the next run.
    """
    now = now or utcnow()
    async with read_session(session_factory) as session:
        due = await reminders_due(session, settings, now)
    recorded = 0
    for sub, days in due:
        try:
            delivered = await send(sub, days)
        except Exception:
            log.exception("reminder for subscription %s failed", sub.id)
            continue
        if not delivered:
            continue
        async with session_factory.begin() as session:
            if await mark_reminded(session, sub, days):
                recorded += 1
    return recorded
