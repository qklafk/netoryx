"""Members of a private VPN (personal mode): everyone who has a subscription, and what the owner can do to them.

Every change is made in a short transaction, audited, and then pushed to the servers right away (no transaction is
open during panel calls). Removing a member takes three steps: the subscription is marked for removal (desired state
"absent" on every panel), the accounts are deleted from the panels, and the subscription row is dropped once nothing
is left on any panel - :func:`finalize_removals` retries the last step for panels that were down.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    KeyValue,
    Provision,
    ProvisionState,
    PurchaseKind,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.db.session import read_session
from app.services import audit, devices, invites, kv, provisioning, subscriptions, tg_proxy, tg_socks, users
from app.utils.time import MAX_PERIOD_DAYS, utcnow

if TYPE_CHECKING:
    from app.config import Settings
    from app.db.session import SessionFactory
    from app.services.provisioning import PanelSource

log = logging.getLogger(__name__)

REMOVAL_KEY_PREFIX = invites.MEMBER_REMOVAL_PREFIX


class MemberNotFound(LookupError):
    """The user has no subscription (is not a member)."""


@dataclass(frozen=True, slots=True)
class Member:
    user: User
    sub: Subscription

    @property
    def unlimited(self) -> bool:
        return invites.is_unlimited(self.sub)

    def state(self, now: datetime) -> str:
        """``active`` / ``paused`` / ``expired``."""
        if self.sub.status == SubscriptionStatus.DISABLED:
            return "paused"
        if self.sub.status == SubscriptionStatus.EXPIRED or self.sub.expires_at <= now:
            return "expired"
        return "active"


def removal_key(user_id: int) -> str:
    return invites.member_removal_key(user_id)


def _removing() -> ColumnElement[bool]:
    return KeyValue.key.startswith(REMOVAL_KEY_PREFIX, autoescape=True)


async def list_members(session_factory: SessionFactory, *, limit: int, offset: int) -> tuple[list[Member], int]:
    """One row per member (newest first), without users whose access is being removed."""
    async with read_session(session_factory) as session:
        removing = [
            int(k[len(REMOVAL_KEY_PREFIX) :]) for k in await session.scalars(select(KeyValue.key).where(_removing()))
        ]
        representative = (
            select(func.min(Subscription.id))
            .where(Subscription.user_id == User.tg_id)
            .correlate(User)
            .scalar_subquery()
        )
        stmt = select(User, Subscription).join(Subscription, Subscription.id == representative)
        if removing:
            stmt = stmt.where(User.tg_id.not_in(removing))
        total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            await session.execute(stmt.order_by(Subscription.created_at.desc(), User.tg_id).limit(limit).offset(offset))
        ).all()
    return [Member(user, sub) for user, sub in rows], int(total or 0)


async def get_member(session_factory: SessionFactory, user_id: int) -> Member | None:
    async with read_session(session_factory) as session:
        row = (
            await session.execute(
                select(User, Subscription)
                .join(Subscription, Subscription.user_id == User.tg_id)
                .where(User.tg_id == user_id)
                .order_by(Subscription.id)
                .limit(1)
            )
        ).first()
        if row is None or await kv.get(session, removal_key(user_id)) is not None:
            return None
    return Member(row[0], row[1])


async def _push(session_factory: SessionFactory, registry: PanelSource, settings: Settings, sub_id: int) -> None:
    try:
        result = await provisioning.sync_subscription(session_factory, registry, settings, sub_id)
    except Exception:  # the reconciler converges it shortly
        log.exception("members: pushing subscription %s failed", sub_id)
        return
    if result.failed:
        log.info("members: subscription %s pushed with errors: %s", sub_id, "; ".join(result.errors[:3]))


async def _locked(session: AsyncSession, user_id: int) -> Subscription:
    sub = await subscriptions.lock_for_user(session, user_id)
    if sub is None:
        raise MemberNotFound(user_id)
    return sub


async def _locked_all(session: AsyncSession, user_id: int) -> list[Subscription]:
    rows = list(
        await session.scalars(
            select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.id).with_for_update()
        )
    )
    if not rows:
        raise MemberNotFound(user_id)
    return rows


async def extend(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    user_id: int,
    days: int | None,
    actor_id: int,
    *,
    source: str = "bot",
    ip: str | None = None,
) -> Subscription:
    """Add ``days`` (from today or from the current expiry, whichever is later); ``None`` = access with no end."""
    async with session_factory.begin() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise MemberNotFound(user_id)
        sub = await _locked(session, user_id)
        if days is None:
            sub.expires_at = invites.UNLIMITED_EXPIRES_AT
            sub.is_complimentary = True
            sub.auto_renew = False
            if sub.status != SubscriptionStatus.DISABLED:
                sub.status = SubscriptionStatus.ACTIVE
            sub.last_reminder_days = None
            sub.revision += 1
            await provisioning.ensure_provisions(session, sub)
            await audit.log(session, actor_id, "member.unlimited", "user", user_id, source, ip)
        else:
            if not 0 < days <= MAX_PERIOD_DAYS:
                raise ValueError(f"days must be 1..{MAX_PERIOD_DAYS}")
            await subscriptions.grant_days(
                session, settings, user, days, kind=PurchaseKind.ADMIN_GRANT, actor_id=actor_id
            )
            sub = await _locked(session, user_id)
        sub_id = sub.id
    await _push(session_factory, registry, settings, sub_id)
    async with read_session(session_factory) as session:
        fresh = await session.get(Subscription, sub_id)
    assert fresh is not None
    return fresh


async def set_paused(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    user_id: int,
    paused: bool,
    actor_id: int,
) -> SubscriptionStatus:
    """Pause (accounts disabled on every server) or resume the member's access. Returns the new status."""
    async with session_factory.begin() as session:
        sub = await _locked(session, user_id)
        wanted = SubscriptionStatus.DISABLED if paused else SubscriptionStatus.ACTIVE
        status = await subscriptions.set_status(session, sub, wanted, actor_id)
        sub_id = sub.id
    await _push(session_factory, registry, settings, sub_id)
    return status


async def set_limits(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    user_id: int,
    actor_id: int,
    *,
    device_limit: int | None = None,
    traffic_limit_gb: int | None = None,
    source: str = "bot",
    ip: str | None = None,
) -> Subscription:
    """Change the device and/or monthly traffic limit (0 = unlimited)."""
    if (device_limit is not None and device_limit < 0) or (traffic_limit_gb is not None and traffic_limit_gb < 0):
        raise ValueError("limits cannot be negative")
    async with session_factory.begin() as session:
        sub = await _locked(session, user_id)
        before = {"device_limit": sub.device_limit, "traffic_limit_gb": sub.traffic_limit_gb}
        if device_limit is not None:
            sub.device_limit = device_limit
        if traffic_limit_gb is not None:
            sub.traffic_limit_gb = traffic_limit_gb
        after = {"device_limit": sub.device_limit, "traffic_limit_gb": sub.traffic_limit_gb}
        if after != before:
            sub.revision += 1
            await audit.log(session, actor_id, "member.limits", "user", user_id, source, ip, before=before, after=after)
        sub_id, changed = sub.id, after != before
    if changed:
        await _push(session_factory, registry, settings, sub_id)
    async with read_session(session_factory) as session:
        fresh = await session.get(Subscription, sub_id)
    assert fresh is not None
    return fresh


async def reset_devices(
    session_factory: SessionFactory,
    registry: PanelSource,
    user_id: int,
    actor_id: int,
    *,
    source: str = "bot",
    ip: str | None = None,
) -> devices.ResetResult:
    async with read_session(session_factory) as session:
        sub_id = await session.scalar(select(Subscription.id).where(Subscription.user_id == user_id))
    if sub_id is None:
        raise MemberNotFound(user_id)
    return await devices.reset(session_factory, registry, sub_id, actor_id=actor_id, source=source, ip=ip)


async def rotate_link(
    session_factory: SessionFactory, user_id: int, actor_id: int, *, source: str = "bot", ip: str | None = None
) -> str:
    """New subscription URL for the member (the old one stops working at once)."""
    async with session_factory.begin() as session:
        user = await session.get(User, user_id, with_for_update=True)
        if user is None:
            raise MemberNotFound(user_id)
        token = await users.rotate_sub_token(session, user)
        await audit.log(session, actor_id, "member.rotate_link", "user", user_id, source, ip)
    return token


async def remove(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    user_id: int,
    actor_id: int,
    *,
    now: datetime | None = None,
    source: str = "bot",
    ip: str | None = None,
) -> bool:
    """Take a member's access away: delete every subscription account, then its rows.

    Returns True when the removal is complete; False if some server still holds an account (retried by
    :func:`finalize_removals`). The user stays in the database (history, payments) but is no longer a member.
    """
    now = now or utcnow()
    retention = timedelta(days=max(0, settings.expired_client_retention_days) + 1)
    async with session_factory.begin() as session:
        subs = await _locked_all(session, user_id)
        for sub in subs:
            sub.status = SubscriptionStatus.DISABLED
            sub.expires_at = min(sub.expires_at, now) - retention  # desired state "absent" on every panel
            sub.is_complimentary = False
            sub.auto_renew = False
            sub.revision += 1
        await kv.set(session, removal_key(user_id), now.isoformat())
        await audit.log(session, actor_id, "member.remove", "user", user_id, source, ip)
        sub_ids = [sub.id for sub in subs]
    for sub_id in sub_ids:
        await _push(session_factory, registry, settings, sub_id)
    proxy = await tg_proxy.remove_user(session_factory, registry, user_id)
    if proxy.failed:
        log.info("members: proxy accounts of user %s not removed yet: %s", user_id, "; ".join(proxy.errors[:3]))
    socks = await tg_socks.remove_user(session_factory, registry, user_id)
    if socks.failed:
        log.info("members: SOCKS5 accounts of user %s not removed yet: %s", user_id, "; ".join(socks.errors[:3]))
    return await finalize(session_factory, user_id)


async def finalize(session_factory: SessionFactory, user_id: int) -> bool:
    """Drop all subscriptions of a member being removed once no panel holds an account any more."""
    async with session_factory.begin() as session:
        if await kv.get(session, removal_key(user_id), fresh=True) is None:
            return True
        subs = list(
            await session.scalars(
                select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.id).with_for_update()
            )
        )
        if subs:
            sub_ids = [sub.id for sub in subs]
            remaining = await session.scalar(
                select(func.count())
                .select_from(Provision)
                .where(Provision.subscription_id.in_(sub_ids), Provision.actual_state != ProvisionState.ABSENT)
            )
            if remaining:
                return False
            await session.execute(delete(Subscription).where(Subscription.id.in_(sub_ids)))
        await kv.delete(session, removal_key(user_id))
    log.info("members: user %s removed", user_id)
    return True


async def finalize_removals(session_factory: SessionFactory) -> int:
    """Finish removals left pending because a panel was unreachable; returns how many completed."""
    async with read_session(session_factory) as session:
        keys = list(await session.scalars(select(KeyValue.key).where(_removing())))
    done = 0
    for key in keys:
        try:
            user_id = int(key[len(REMOVAL_KEY_PREFIX) :])
        except ValueError:
            continue
        if await finalize(session_factory, user_id):
            done += 1
    return done
