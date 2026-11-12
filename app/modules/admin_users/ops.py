"""Admin operations on one user that the shared services do not cover: the user card, removing days, plan and
limit changes, flags (ban, complimentary), deletion with panel cleanup and search.

Functions taking ``session`` never commit; functions taking ``session_factory`` own short transactions and do panel
I/O only after the commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    AdminRole,
    Inbound,
    Panel,
    PanelKind,
    Payment,
    PaymentStatus,
    Plan,
    Provision,
    ProvisionState,
    Subscription,
    SubscriptionStatus,
    TgProxyAccess,
    TgSocksAccess,
    User,
)
from app.db.session import SessionFactory
from app.services import admins, audit, bans, campaigns, provisioning, referrals, users
from app.services.panels.base import PanelError
from app.services.panels.mtproto import MtprotoProxyBackend
from app.services.panels.socks import SocksProxyBackend
from app.services.provisioning import PanelSource
from app.utils.time import MAX_PERIOD_DAYS, utcnow

log = logging.getLogger(__name__)

SEARCH_LIMIT = 20
_MAX_ERROR = 200


class UserOpError(Exception):
    """The operation was refused; ``code`` is an i18n suffix (``admin_users.error.<code>``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class UserCard:
    user: User
    subscription: Subscription | None
    plan: Plan | None
    role: AdminRole | None
    invited: int
    campaign: str | None
    payments_count: int
    payments_sum_kop: int
    last_payment_at: datetime | None


async def load_card(session: AsyncSession, settings: Settings, user_id: int) -> UserCard | None:
    user = await session.get(User, user_id)
    if user is None:
        return None
    sub = await session.scalar(select(Subscription).where(Subscription.user_id == user_id))
    plan = await session.get(Plan, sub.plan_id) if sub is not None and sub.plan_id is not None else None
    invited, _earned = await referrals.stats(session, user_id)
    count, total, last = (
        await session.execute(
            select(
                func.count(Payment.id), func.coalesce(func.sum(Payment.amount_kop), 0), func.max(Payment.credited_at)
            ).where(
                Payment.user_id == user_id,
                Payment.status == PaymentStatus.SUCCEEDED,
                Payment.credited_at.is_not(None),
            )
        )
    ).one()
    return UserCard(
        user=user,
        subscription=sub,
        plan=plan,
        role=await admins.resolve_role(session, settings, user_id),
        invited=invited,
        campaign=await campaigns.campaign_of(session, user_id),
        payments_count=int(count or 0),
        payments_sum_kop=int(total or 0),
        last_payment_at=last,
    )


async def search(session: AsyncSession, query: str, *, limit: int = SEARCH_LIMIT) -> list[User]:
    """Users matching an id, @username, t.me link or a part of the first name (most recently seen first)."""
    exact = await users.find(session, query)
    if exact is not None:
        return [exact]
    text = " ".join(query.split()).lstrip("@")
    if len(text) < 2 or text.isdigit():
        return []
    # SQLite folds case only for ASCII: match the usual capitalizations of non-Latin names explicitly.
    variants = {text, text.lower(), text.upper(), text.capitalize(), text.title()}
    patterns = ["%" + v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for v in variants]
    conditions = [User.first_name.like(p, escape="\\") for p in patterns]
    conditions += [func.lower(User.first_name).like(p.lower(), escape="\\") for p in patterns]
    conditions.append(User.username.like(patterns[0].lower(), escape="\\"))
    stmt = (
        select(User).where(or_(*conditions)).order_by(User.last_seen_at.desc(), User.tg_id.desc()).limit(max(1, limit))
    )
    return list((await session.scalars(stmt)).all())


async def _locked_subscription(session: AsyncSession, user_id: int) -> Subscription:
    if await session.scalar(select(Subscription.id).where(Subscription.user_id == user_id)) is None:
        raise UserOpError("no_subscription")
    sub = await session.scalar(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if sub is None:
        raise UserOpError("no_subscription")
    return sub


async def remove_days(
    session: AsyncSession, user_id: int, days: int, actor_id: int, *, now: datetime | None = None
) -> Subscription:
    """Move the expiry ``days`` back (never before now: the subscription then simply ends)."""
    if not 0 < days <= MAX_PERIOD_DAYS:
        raise ValueError(f"days must be 1..{MAX_PERIOD_DAYS}")
    now = now or utcnow()
    sub = await _locked_subscription(session, user_id)
    before = sub.expires_at
    sub.expires_at = max(now, sub.expires_at - timedelta(days=days)).replace(microsecond=0)
    if sub.expires_at <= now and sub.status == SubscriptionStatus.ACTIVE:
        sub.status = SubscriptionStatus.EXPIRED
    sub.revision += 1
    await provisioning.ensure_provisions(session, sub)
    await audit.log(
        session,
        actor_id,
        "subscription.remove_days",
        "user",
        user_id,
        days=days,
        expires_before=before.isoformat(),
        expires_at=sub.expires_at.isoformat(),
    )
    return sub


async def set_plan(session: AsyncSession, user_id: int, plan: Plan, actor_id: int) -> Subscription:
    """Switch the subscription to ``plan`` (its limits apply now; the expiry date stays)."""
    sub = await _locked_subscription(session, user_id)
    old = sub.plan_id
    sub.plan_id = plan.id
    sub.device_limit = plan.device_limit
    sub.traffic_limit_gb = plan.traffic_limit_gb
    sub.is_trial = False
    sub.revision += 1
    await provisioning.ensure_provisions(session, sub)
    await audit.log(session, actor_id, "subscription.set_plan", "user", user_id, old_plan_id=old, plan_id=plan.id)
    return sub


async def set_limits(
    session: AsyncSession,
    user_id: int,
    actor_id: int,
    *,
    device_limit: int | None = None,
    traffic_limit_gb: int | None = None,
) -> Subscription:
    """Override the subscription's device and/or traffic limit (0 = unlimited)."""
    for value in (device_limit, traffic_limit_gb):
        if value is not None and value < 0:
            raise ValueError("limits must be >= 0")
    sub = await _locked_subscription(session, user_id)
    changes: dict[str, int] = {}
    if device_limit is not None and device_limit != sub.device_limit:
        changes["device_limit"] = device_limit
        sub.device_limit = device_limit
    if traffic_limit_gb is not None and traffic_limit_gb != sub.traffic_limit_gb:
        changes["traffic_limit_gb"] = traffic_limit_gb
        sub.traffic_limit_gb = traffic_limit_gb
    if changes:
        sub.revision += 1
        await provisioning.ensure_provisions(session, sub)
        await audit.log(session, actor_id, "subscription.set_limits", "user", user_id, "bot", None, **changes)
    return sub


async def set_auto_renew(session: AsyncSession, user_id: int, on: bool, actor_id: int) -> Subscription:
    sub = await _locked_subscription(session, user_id)
    if sub.auto_renew != on:
        sub.auto_renew = on
        await audit.log(session, actor_id, "subscription.set_auto_renew", "user", user_id, on=on)
    return sub


async def set_complimentary(session: AsyncSession, user_id: int, on: bool, actor_id: int) -> Subscription:
    """Complimentary subscriptions are free: no reminders, no auto-renewal, not counted as revenue."""
    sub = await _locked_subscription(session, user_id)
    if sub.is_complimentary != on:
        sub.is_complimentary = on
        if on:
            sub.auto_renew = False
        await audit.log(session, actor_id, "subscription.set_complimentary", "user", user_id, on=on)
    return sub


async def set_banned(session: AsyncSession, settings: Settings, user_id: int, banned: bool, actor_id: int) -> User:
    """Ban or unban; panels disable / re-enable the accounts on the next sync. Administrators cannot be banned."""
    user = await session.scalar(select(User).where(User.tg_id == user_id).with_for_update())
    if user is None:
        raise UserOpError("not_found")
    if banned and await admins.resolve_role(session, settings, user_id) is not None:
        raise UserOpError("is_admin")
    if await bans.set_banned_manually(session, user, banned):
        await audit.log(session, actor_id, "user.ban" if banned else "user.unban", "user", user_id)
    return user


@dataclass(frozen=True, slots=True)
class _RemoteAccount:
    panel_id: int
    target: int | None
    name: str
    uuid: str
    proxy: bool  # Telegram proxy account (MTProto backend) instead of a VPN client
    socks: bool = False  # Telegram SOCKS5 account on a mixed inbound


@dataclass(slots=True)
class DeleteResult:
    deleted: bool = False
    accounts: int = 0  # panel accounts we tried to remove
    removed: int = 0
    errors: list[str] = field(default_factory=list)


async def _remote_accounts(session: AsyncSession, user_id: int) -> list[_RemoteAccount]:
    vpn = (
        await session.execute(
            select(Provision.panel_id, Panel.kind, Inbound.remote_id, Provision.remote_email, Subscription.client_uuid)
            .join(Subscription, Subscription.id == Provision.subscription_id)
            .join(Panel, Panel.id == Provision.panel_id)
            .outerjoin(Inbound, Inbound.id == Provision.inbound_id)
            .where(
                Subscription.user_id == user_id,
                Provision.remote_email.is_not(None),
                Provision.actual_state != ProvisionState.ABSENT,
            )
        )
    ).all()
    proxies = (
        await session.execute(
            select(TgProxyAccess.panel_id, Inbound.remote_id, TgProxyAccess.remote_name)
            .outerjoin(Inbound, Inbound.id == TgProxyAccess.inbound_id)
            .where(TgProxyAccess.user_id == user_id, TgProxyAccess.actual_state != ProvisionState.ABSENT)
        )
    ).all()
    accounts = [
        _RemoteAccount(panel_id, remote_id if kind == PanelKind.XUI else None, email or "", uuid, False)
        for panel_id, kind, remote_id, email, uuid in vpn
    ]
    accounts += [_RemoteAccount(panel_id, remote_id, name, "", True) for panel_id, remote_id, name in proxies]
    socks = (
        await session.execute(
            select(TgSocksAccess.panel_id, Inbound.remote_id, TgSocksAccess.username)
            .join(Inbound, Inbound.id == TgSocksAccess.inbound_id)
            .where(TgSocksAccess.user_id == user_id, TgSocksAccess.actual_state != ProvisionState.ABSENT)
        )
    ).all()
    accounts += [_RemoteAccount(panel_id, remote_id, name, "", False, True) for panel_id, remote_id, name in socks]
    return accounts


async def delete_user(
    session_factory: SessionFactory, registry: PanelSource, settings: Settings, user_id: int, actor_id: int
) -> DeleteResult:
    """Delete the user with all their data and remove their accounts from the panels.

    The database rows go first (one transaction); panel accounts are removed afterwards. An account that could not
    be removed is left to the orphan cleanup of the reconciler (``DELETE_ORPHAN_CLIENTS``).
    """
    result = DeleteResult()
    async with session_factory.begin() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise UserOpError("not_found")
        if await admins.resolve_role(session, settings, user_id) is not None:
            raise UserOpError("is_admin")
        accounts = await _remote_accounts(session, user_id)
        await audit.log(
            session,
            actor_id,
            "user.delete",
            "user",
            user_id,
            username=user.username,
            balance_kop=user.balance_kop,
            referral_balance_kop=user.referral_balance_kop,
            accounts=len(accounts),
        )
        await session.execute(delete(User).where(User.tg_id == user_id))
    result.deleted = True
    result.accounts = len(accounts)
    for account in accounts:
        try:
            backend = await registry.get(account.panel_id)
            if account.socks:
                if not isinstance(backend, SocksProxyBackend):
                    raise PanelError("not a SOCKS5 proxy panel")
                await backend.delete_socks_user(account.target, account.name)
            elif account.proxy:
                if not isinstance(backend, MtprotoProxyBackend):
                    raise PanelError("not a Telegram proxy panel")
                await backend.delete_proxy_user(account.target, account.name)
            else:
                await backend.delete_account(account.target, account.name, account.uuid)
            result.removed += 1
        except (PanelError, LookupError) as exc:
            message = (str(exc) or type(exc).__name__)[:_MAX_ERROR]
            result.errors.append(f"panel {account.panel_id}: {message}")
            log.warning("user %s deleted, but panel %s cleanup failed: %s", user_id, account.panel_id, exc)
    return result
