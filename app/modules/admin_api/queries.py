"""Read helpers shared by the user and subscription sections."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Inbound,
    Panel,
    Payment,
    PaymentStatus,
    Plan,
    Provision,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.modules.admin_api.deps import not_found
from app.modules.admin_api.schemas.users import (
    ProvisionOut,
    SubscriptionOut,
    UserFilter,
    UserListItem,
)
from app.services import subscriptions, users
from app.services.provisioning import is_traffic_exhausted
from app.utils.time import days_left


def paid_exists() -> ColumnElement[bool]:
    """SQL: the user has at least one credited payment that was not a donation."""
    return exists(
        select(Payment.id).where(
            Payment.user_id == User.tg_id,
            Payment.status == PaymentStatus.SUCCEEDED,
            Payment.is_donation.is_(False),
        )
    )


def user_filter(kind: UserFilter, now: datetime) -> ColumnElement[bool] | None:
    """Condition for a user list filter (``Subscription`` must be outer-joined)."""
    active = and_(Subscription.status == SubscriptionStatus.ACTIVE, Subscription.expires_at > now)
    conditions: dict[UserFilter, ColumnElement[bool]] = {
        UserFilter.ACTIVE: active,
        UserFilter.EXPIRED: or_(
            Subscription.status == SubscriptionStatus.EXPIRED,
            and_(Subscription.status == SubscriptionStatus.ACTIVE, Subscription.expires_at <= now),
        ),
        UserFilter.TRIAL: and_(active, Subscription.is_trial.is_(True)),
        UserFilter.DISABLED: Subscription.status == SubscriptionStatus.DISABLED,
        UserFilter.COMPLIMENTARY: Subscription.is_complimentary.is_(True),
        UserFilter.NO_SUBSCRIPTION: Subscription.id.is_(None),
        UserFilter.NEVER_PAID: ~paid_exists(),
        UserFilter.PAID: paid_exists(),
        UserFilter.BANNED: User.is_banned.is_(True),
        UserFilter.BLOCKED: User.bot_blocked.is_(True),
    }
    condition = conditions.get(kind)
    if kind == UserFilter.NO_SUBSCRIPTION:
        return ~exists(select(Subscription.id).where(Subscription.user_id == User.tg_id).correlate(User))
    if kind in {UserFilter.ACTIVE, UserFilter.EXPIRED, UserFilter.TRIAL, UserFilter.DISABLED, UserFilter.COMPLIMENTARY}:
        assert condition is not None
        return exists(select(Subscription.id).where(Subscription.user_id == User.tg_id, condition).correlate(User))
    return condition


def search_condition(query: str) -> ColumnElement[bool] | None:
    """Match by Telegram id, @username or a part of the name/username."""
    text = query.strip()
    if not text:
        return None
    key = users.parse_query(text)
    parts: list[ColumnElement[bool]] = []
    if isinstance(key, int):
        parts.append(User.tg_id == key)
    elif isinstance(key, str):
        parts.append(User.username.contains(key, autoescape=True))
    raw = text.lstrip("@")[:64]
    if raw:
        # SQLite lowercases ASCII only: also try the text as typed and capitalized (Cyrillic names).
        parts.append(func.lower(User.first_name).contains(raw.lower(), autoescape=True))
        for variant in {raw, raw.capitalize()}:
            parts.append(User.first_name.contains(variant, autoescape=True))
    return or_(*parts) if parts else None


def list_item(user: User, sub: Subscription | None) -> UserListItem:
    return UserListItem(
        tg_id=user.tg_id,
        username=user.username,
        first_name=user.first_name,
        language=user.language,
        balance_kop=user.balance_kop,
        referral_balance_kop=user.referral_balance_kop,
        is_banned=user.is_banned,
        bot_blocked=user.bot_blocked,
        created_at=user.created_at,
        last_seen_at=user.last_seen_at,
        sub_status=sub.status if sub else None,
        sub_expires_at=sub.expires_at if sub else None,
        sub_is_trial=bool(sub and sub.is_trial),
        sub_is_complimentary=bool(sub and sub.is_complimentary),
        sub_plan_id=sub.plan_id if sub else None,
    )


async def get_user(session: AsyncSession, user_id: int, *, lock: bool = False) -> User:
    stmt: Any = select(User).where(User.tg_id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    user = await session.scalar(stmt)
    if user is None:
        raise not_found("User not found")
    return user


async def get_subscription(
    session: AsyncSession, user_id: int, *, lock: bool = False, subscription_id: int | None = None
) -> Subscription | None:
    stmt: Any = select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.id).limit(1)
    if subscription_id is not None:
        stmt = stmt.where(Subscription.id == subscription_id)
    if lock:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt.execution_options(populate_existing=True))


async def require_subscription(
    session: AsyncSession, user_id: int, *, lock: bool = False, subscription_id: int | None = None
) -> Subscription:
    sub = await get_subscription(session, user_id, lock=lock, subscription_id=subscription_id)
    if sub is None:
        raise not_found("The user has no subscription")
    return sub


async def subscription_view(
    session: AsyncSession, sub: Subscription, now: datetime, *, with_provisions: bool = True
) -> SubscriptionOut:
    plan_title = await session.scalar(select(Plan.title).where(Plan.id == sub.plan_id)) if sub.plan_id else None
    provisions: list[ProvisionOut] = []
    if with_provisions:
        rows = (
            await session.execute(
                select(Provision, Panel.name, Inbound.name)
                .join(Panel, Panel.id == Provision.panel_id)
                .outerjoin(Inbound, Inbound.id == Provision.inbound_id)
                .where(Provision.subscription_id == sub.id)
                .order_by(Provision.panel_id, Provision.id)
            )
        ).all()
        provisions = [
            ProvisionOut(
                id=p.id,
                panel_id=p.panel_id,
                panel_name=panel_name,
                inbound_id=p.inbound_id,
                inbound_name=inbound_name,
                desired_state=p.desired_state,
                actual_state=p.actual_state,
                synced_revision=p.synced_revision,
                attempts=p.attempts,
                last_error=p.last_error,
                updated_at=p.updated_at,
            )
            for p, panel_name, inbound_name in rows
        ]
    active = sub.status == SubscriptionStatus.ACTIVE and sub.expires_at > now
    return SubscriptionOut(
        id=sub.id,
        label=sub.label,
        status=sub.status,
        active=active,
        plan_id=sub.plan_id,
        plan_title=plan_title,
        expires_at=sub.expires_at,
        days_left=days_left(sub.expires_at, now) if sub.expires_at > now else 0,
        device_limit=sub.device_limit,
        traffic_limit_gb=sub.traffic_limit_gb,
        auto_renew=sub.auto_renew,
        is_trial=sub.is_trial,
        is_complimentary=sub.is_complimentary,
        stars_recurring=sub.stars_charge_id is not None,
        revision=sub.revision,
        traffic_exhausted=is_traffic_exhausted(sub),
        created_at=sub.created_at,
        provisions=provisions,
    )


def subscription_url(settings: Any, user: User, sub: Subscription | None = None) -> str | None:
    base = settings.public_base_url
    return f"{base.rstrip('/')}/sub/{subscriptions.token_for(sub, user)}" if base else None
