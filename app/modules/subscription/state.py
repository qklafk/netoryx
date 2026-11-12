"""What the subscription screens show: the user, their subscription and plan, read in one short session."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import exists, select

from app.config import Settings
from app.db.models import Endpoint, EndpointProfile, Plan, Subscription, SubscriptionStatus, User
from app.db.session import SessionFactory, read_session
from app.services import kv, subscriptions
from app.services import plans as plans_service
from app.services.invites import UNLIMITED_EXPIRES_AT
from app.utils.time import days_left, utcnow


class Status(StrEnum):
    NONE = "none"  # never had a subscription
    ACTIVE = "active"
    EXPIRED = "expired"
    DISABLED = "disabled"  # blocked by an admin (or the user is banned)


@dataclass(frozen=True, slots=True)
class SubState:
    user: User
    sub: Subscription | None
    plan: Plan | None
    now: datetime
    total_subscriptions: int = 0

    @property
    def token(self) -> str:
        return subscriptions.token_for(self.sub, self.user)

    @property
    def status(self) -> Status:
        sub = self.sub
        if sub is None:
            return Status.NONE
        if self.user.is_banned or sub.status == SubscriptionStatus.DISABLED:
            return Status.DISABLED
        if sub.expires_at <= self.now:
            return Status.EXPIRED
        return Status.ACTIVE

    @property
    def active(self) -> bool:
        return self.status == Status.ACTIVE

    @property
    def unlimited(self) -> bool:
        """Never expires (an unlimited invitation)."""
        return self.sub is not None and self.sub.expires_at >= UNLIMITED_EXPIRES_AT

    @property
    def days_left(self) -> int:
        return days_left(self.sub.expires_at, self.now) if self.sub is not None else 0

    @property
    def complimentary(self) -> bool:
        return self.sub is not None and self.sub.is_complimentary

    def trial_available(self, settings: Settings) -> bool:
        """The free trial can be started: shop mode, enabled, never used, no subscription yet."""
        return (
            settings.app_mode == "shop"
            and settings.trial_days > 0
            and not self.user.trial_used
            and not self.user.is_banned
            and self.sub is None
        )

    def can_auto_renew(self, settings: Settings) -> bool:
        """Auto-renewal makes sense: shop mode, a plan to renew, not a free (complimentary or unlimited) one."""
        return (
            settings.app_mode == "shop"
            and self.sub is not None
            and self.sub.plan_id is not None
            and not self.complimentary
            and not self.unlimited
        )


def selection_key(user_id: int) -> str:
    return f"bot:selected_sub:{user_id}"


async def select_subscription(session_factory: SessionFactory, user_id: int, subscription_id: int) -> bool:
    """Persist a selection only after checking ownership."""
    async with session_factory.begin() as session:
        if await subscriptions.get_for_user(session, user_id, subscription_id) is None:
            return False
        await kv.set(session, selection_key(user_id), str(subscription_id))
    return True


async def load_state(
    session_factory: SessionFactory,
    user_id: int,
    now: datetime | None = None,
    *,
    subscription_id: int | None = None,
) -> SubState | None:
    """The user's subscription state; ``None`` for an unknown user."""
    async with read_session(session_factory) as session, session.begin():
        user = await session.get(User, user_id)
        if user is None:
            return None
        all_subs = await subscriptions.list_for_user(session, user_id)
        selected = subscription_id
        if selected is None:
            stored = await kv.get(session, selection_key(user_id))
            selected = int(stored) if stored and stored.isdecimal() else None
        sub = next((item for item in all_subs if item.id == selected), None)
        if sub is None and subscription_id is None:
            sub = all_subs[0] if all_subs else None
        plan = await session.get(Plan, sub.plan_id) if sub is not None and sub.plan_id is not None else None
    return SubState(user=user, sub=sub, plan=plan, now=now or utcnow(), total_subscriptions=len(all_subs))


async def offered_plans(session_factory: SessionFactory, user_id: int) -> list[Plan]:
    """Active, visible plans the given user is eligible to buy."""
    async with read_session(session_factory) as session, session.begin():
        user = await session.get(User, user_id)
        return await plans_service.list_available(session, user) if user is not None else []


async def get_plan(session_factory: SessionFactory, plan_id: int) -> Plan | None:
    """An active plan (hidden ones included); ``None`` for archived or unknown plans."""
    async with read_session(session_factory) as session, session.begin():
        plan = await plans_service.get(session, plan_id)
    return plan if plan is not None and plan.is_active else None


async def has_lte_endpoints(session_factory: SessionFactory) -> bool:
    """Whether subscriptions carry "LTE" (mobile white list) entries."""
    stmt = select(
        exists().where(
            Endpoint.profile == EndpointProfile.WHITELIST,
            Endpoint.is_active.is_(True),
            Endpoint.auto_hidden.is_(False),
        )
    )
    async with read_session(session_factory) as session, session.begin():
        return bool(await session.scalar(stmt))
