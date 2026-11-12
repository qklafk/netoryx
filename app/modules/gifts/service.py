"""Gift subscriptions: buy a plan as a gift code (from the balance, or through a payment), list one's gifts, and the
public data of a gift for its landing page (``/gift/<code>``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.db.models import Plan, PromoCode, PromoKind, User
from app.db.session import read_session
from app.services import promo
from app.services.subscriptions import SubErrorCode, SubscriptionError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.context import AppContext

MY_GIFTS_LIMIT = 10


@dataclass(frozen=True, slots=True)
class GiftInfo:
    """What anyone holding a gift code may see about it."""

    code: str
    plan_title: str | None
    days: int
    redeemed: bool
    active: bool


@dataclass(frozen=True, slots=True)
class OwnGift:
    code: str
    plan_title: str | None
    days: int
    redeemed: bool


def gift_link(bot_username: str | None, code: str) -> str:
    """Deep link that redeems the gift (``/start gift_<code>``); the bare code if the bot username is unknown."""
    return f"https://t.me/{bot_username}?start=gift_{code}" if bot_username else code


async def gift_plans(session: AsyncSession) -> list[Plan]:
    """Plans that can be given as a gift: active, listed and not free."""
    rows = await session.scalars(
        select(Plan)
        .where(Plan.is_active.is_(True), Plan.is_hidden.is_(False), Plan.price_kop > 0)
        .order_by(Plan.sort_order, Plan.duration_days, Plan.id)
    )
    return list(rows.all())


async def buy_from_balance(ctx: AppContext, user_id: int, plan_id: int, token: str) -> tuple[PromoCode, Plan]:
    """Charge the plan's price from the wallet and issue the gift code.

    ``token`` makes a repeated tap of the same button return the same gift. Raises ``SubscriptionError``
    (NO_PLAN, INSUFFICIENT_FUNDS, BANNED).
    """
    async with ctx.session_factory.begin() as session:
        user = await session.get(User, user_id)
        plan = await session.get(Plan, plan_id)
        if user is None or plan is None or plan.price_kop <= 0 or plan.is_hidden:
            raise SubscriptionError(SubErrorCode.NO_PLAN)
        gift = await promo.create_gift(session, ctx.settings, user, plan, f"balance:{user_id}:{token}")
    return gift, plan


async def own_gifts(ctx: AppContext, user_id: int, limit: int = MY_GIFTS_LIMIT) -> list[OwnGift]:
    """The user's latest gift codes, newest first."""
    async with read_session(ctx.session_factory) as session:
        rows = await session.execute(
            select(PromoCode, Plan.title)
            .outerjoin(Plan, Plan.id == PromoCode.plan_id)
            .where(PromoCode.kind == PromoKind.GIFT, PromoCode.created_by == user_id)
            .order_by(PromoCode.id.desc())
            .limit(max(1, limit))
        )
        return [OwnGift(code.code, title, code.bonus_days, code.uses > 0) for code, title in rows.all()]


async def gift_info(session: AsyncSession, code: str) -> GiftInfo | None:
    """Public data of a gift code (None for unknown codes and for promo codes)."""
    row = await promo.get_by_code(session, code)
    if row is None or row.kind is not PromoKind.GIFT:
        return None
    plan = await session.get(Plan, row.plan_id) if row.plan_id is not None else None
    return GiftInfo(
        code=row.code,
        plan_title=plan.title if plan is not None else None,
        days=row.bonus_days,
        redeemed=row.max_uses > 0 and row.uses >= row.max_uses,
        active=row.is_active,
    )
