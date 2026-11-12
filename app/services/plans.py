"""Tariff plans. Admin mutations must be audited by the caller (``app.services.audit.log``)."""

from __future__ import annotations

import hmac
import logging
import math
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, PaymentStatus, Plan, Purchase, PurchaseKind, User
from app.utils.time import MAX_PERIOD_DAYS

if TYPE_CHECKING:
    from app.config import Settings
    from app.utils.crypto import SecretBox

log = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 64
_UPDATABLE = frozenset(
    {
        "title",
        "duration_days",
        "price_kop",
        "price_stars",
        "device_limit",
        "traffic_limit_gb",
        "availability_rule",
        "allowed_user_ids",
        "is_active",
        "sort_order",
    }
)


class AvailabilityRule(StrEnum):
    ALL = "all"
    NEW = "new"
    EXISTING = "existing"
    INVITED = "invited"
    ALLOWLIST = "allowlist"


_DISPLAY_ORDER = (Plan.sort_order, Plan.duration_days, Plan.id)


async def list_active(session: AsyncSession) -> list[Plan]:
    """Plans offered to users, in display order."""
    return list((await session.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(*_DISPLAY_ORDER))).all())


async def list_available(session: AsyncSession, user: User) -> list[Plan]:
    """Active plans the user may buy, excluding hidden plans handled by their signed links."""
    plans = await list_active(session)
    paid = await has_paid_before(session, user.tg_id)
    return [plan for plan in plans if not plan.is_hidden and available_to(plan, user, paid=paid)]


async def list_all(session: AsyncSession) -> list[Plan]:
    """All plans including archived ones (admin view), in display order."""
    return list((await session.scalars(select(Plan).order_by(*_DISPLAY_ORDER))).all())


async def get(session: AsyncSession, plan_id: int) -> Plan | None:
    """Plan by id (archived plans included: old payments and gifts still reference them)."""
    return await session.get(Plan, plan_id)


def validate(values: dict[str, Any]) -> None:
    """Raise ``ValueError`` if any provided plan field is invalid."""
    title = values.get("title")
    if "title" in values and (not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_LENGTH):
        raise ValueError(f"title must be 1..{MAX_TITLE_LENGTH} characters")
    checks = {
        "duration_days": lambda v: 0 < v <= MAX_PERIOD_DAYS,
        "price_kop": lambda v: v >= 0,
        "device_limit": lambda v: v >= 0,
        "traffic_limit_gb": lambda v: v >= 0,
    }
    for field, ok in checks.items():
        if field in values and (not _is_int(values[field]) or not ok(values[field])):
            raise ValueError(f"invalid {field}: {values[field]!r}")
    stars = values.get("price_stars")
    if stars is not None and (not _is_int(stars) or stars <= 0):
        raise ValueError(f"invalid price_stars: {stars!r}")
    rule = values.get("availability_rule")
    if rule is not None:
        try:
            AvailabilityRule(rule)
        except ValueError as exc:
            raise ValueError(f"invalid availability_rule: {rule!r}") from exc
    ids = values.get("allowed_user_ids")
    if ids is not None and (
        not isinstance(ids, list)
        or len(ids) > 10_000
        or any(not _is_int(item) or item <= 0 for item in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("allowed_user_ids must contain up to 10000 unique positive Telegram IDs")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


async def create(
    session: AsyncSession,
    *,
    title: str,
    duration_days: int,
    price_kop: int,
    device_limit: int = 3,
    traffic_limit_gb: int = 0,
    price_stars: int | None = None,
    sort_order: int | None = None,
    is_active: bool = True,
    availability_rule: str = AvailabilityRule.ALL,
    allowed_user_ids: list[int] | None = None,
) -> Plan:
    """Create a plan; ``sort_order`` defaults to after the last plan."""
    values: dict[str, Any] = {
        "title": title.strip(),
        "duration_days": duration_days,
        "price_kop": price_kop,
        "device_limit": device_limit,
        "traffic_limit_gb": traffic_limit_gb,
        "price_stars": price_stars,
        "is_active": is_active,
        "availability_rule": availability_rule,
        "allowed_user_ids": allowed_user_ids or [],
    }
    validate(values)
    if sort_order is None:
        sort_order = int(await session.scalar(select(func.coalesce(func.max(Plan.sort_order), -1))) or 0) + 1
    plan = Plan(**values, sort_order=sort_order)
    session.add(plan)
    await session.flush()
    return plan


async def has_paid_before(session: AsyncSession, user_id: int) -> bool:
    """Whether the user has a completed paid purchase or provider payment."""
    payment = exists().where(Payment.user_id == user_id, Payment.status == PaymentStatus.SUCCEEDED)
    purchase = exists().where(
        Purchase.user_id == user_id,
        Purchase.kind.in_((PurchaseKind.PURCHASE, PurchaseKind.AUTO_RENEW)),
    )
    return bool(await session.scalar(select(payment | purchase)))


def available_to(plan: Plan, user: User, *, paid: bool) -> bool:
    """Evaluate a tariff audience rule without exposing account data to other users."""
    try:
        rule = AvailabilityRule(plan.availability_rule)
    except ValueError:
        return False
    return (
        rule is AvailabilityRule.ALL
        or (rule is AvailabilityRule.NEW and not paid)
        or (rule is AvailabilityRule.EXISTING and paid)
        or (rule is AvailabilityRule.INVITED and user.referred_by is not None)
        or (rule is AvailabilityRule.ALLOWLIST and user.tg_id in plan.allowed_user_ids)
    )


async def update(session: AsyncSession, plan: Plan, **fields: Any) -> Plan:
    """Change plan fields (any of: title, duration_days, price_kop, price_stars, device_limit,
    traffic_limit_gb, is_active, sort_order). Existing subscriptions keep their snapshotted limits."""
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"unknown plan fields: {', '.join(sorted(unknown))}")
    if isinstance(fields.get("title"), str):
        fields["title"] = fields["title"].strip()
    validate(fields)
    for key, value in fields.items():
        setattr(plan, key, value)
    await session.flush()
    return plan


async def archive(session: AsyncSession, plan: Plan) -> Plan:
    """Hide a plan from users (kept for history, payments and gifts that reference it)."""
    return await update(session, plan, is_active=False)


async def seed_defaults(session: AsyncSession, settings: Settings) -> int:
    """Create ``settings.default_plans`` if the table is empty; returns the number of plans created."""
    if await session.scalar(select(func.count()).select_from(Plan)):
        return 0
    for index, seed in enumerate(settings.default_plans):
        await create(
            session,
            title=seed.title,
            duration_days=seed.days,
            price_kop=seed.price_rub * 100,
            device_limit=seed.devices,
            traffic_limit_gb=seed.traffic_gb,
            sort_order=index,
        )
    log.info("seeded %d default plans", len(settings.default_plans))
    return len(settings.default_plans)


def stars_price(plan: Plan, settings: Settings) -> int:
    """Price in Telegram Stars: ``plan.price_stars`` or ``ceil(price_rub / STARS_RUB_PER_STAR)``.

    Free plans cost 0 Stars (callers should not offer a Stars invoice for them).
    """
    if plan.price_stars:
        return plan.price_stars
    return stars_price_for_kop(plan.price_kop, settings)


def stars_price_for_kop(price_kop: int, settings: Settings) -> int:
    """Convert a rouble price to Stars for a dynamically discounted tariff."""
    if price_kop <= 0:
        return 0
    rub_per_star = Decimal(str(settings.stars_rub_per_star))
    return max(1, math.ceil(Decimal(price_kop) / (rub_per_star * 100)))


def discounted_stars_price(plan: Plan, price_kop: int, settings: Settings) -> int:
    """Stars price for a discounted tariff, preserving a configured Stars price proportionally."""
    if price_kop <= 0:
        return 0
    if plan.price_stars and plan.price_kop > 0:
        return max(1, math.ceil(Decimal(plan.price_stars) * price_kop / plan.price_kop))
    return stars_price_for_kop(price_kop, settings)


def monthly_price_kop(plan: Plan) -> int:
    """Price normalized to 30 days (for MRR and "per month" labels), rounded to the nearest kopeck."""
    return round(plan.price_kop * 30 / plan.duration_days)


# --- links to plans --------------------------------------------------------------------------------------------------
LINK_PREFIX = "plan_"
LINK_TOKEN_LENGTH = 12


def link_token(box: SecretBox, plan_id: int) -> str:
    """The unguessable part of a plan's ``/start`` link: hidden plans open only with it."""
    return box.sign(f"plan-link:{plan_id}")[:LINK_TOKEN_LENGTH]


def link_payload(box: SecretBox, plan_id: int) -> str:
    """``/start`` payload that opens plan ``plan_id``: ``plan_<id>-<token>``."""
    return f"{LINK_PREFIX}{plan_id}-{link_token(box, plan_id)}"


def link_token_valid(box: SecretBox, plan_id: int, token: str) -> bool:
    return bool(token) and hmac.compare_digest(token, link_token(box, plan_id))
