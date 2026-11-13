"""Create temporary personal offers for users who did not convert or have churned."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Plan, Purchase, PurchaseKind, Subscription, SubscriptionStatus, User, WinbackOffer
from app.db.session import SessionFactory
from app.utils.time import utcnow


async def issue(
    session_factory: SessionFactory, settings: Settings, *, now: datetime | None = None
) -> list[WinbackOffer]:
    """Issue or renew eligible offers and return only offers that should be announced now."""
    if not settings.winback_enabled or not 1 <= settings.winback_discount_percent <= 99:
        return []
    moment = now or utcnow()
    expires_at = moment + timedelta(days=max(1, settings.winback_offer_days))
    created: list[WinbackOffer] = []
    async with session_factory.begin() as session:
        for kind, user_ids in (
            ("trial", await _trial_users(session, moment, settings.winback_trial_wait_days)),
            ("churned", await _churned_users(session, moment, settings.winback_churn_wait_days)),
        ):
            for user_id in user_ids:
                offer = await session.scalar(
                    select(WinbackOffer).where(WinbackOffer.user_id == user_id, WinbackOffer.kind == kind)
                )
                if offer is not None and offer.expires_at > moment:
                    continue
                if offer is None:
                    offer = WinbackOffer(
                        user_id=user_id,
                        kind=kind,
                        discount_percent=settings.winback_discount_percent,
                        expires_at=expires_at,
                        sent_at=moment,
                    )
                    session.add(offer)
                else:
                    offer.discount_percent = settings.winback_discount_percent
                    offer.expires_at = expires_at
                    offer.sent_at = moment
                created.append(offer)
        await session.flush()
    return created


async def consume(session: AsyncSession, settings: Settings, user_id: int, now: datetime) -> None:
    """Use up the user's active offers after a discounted purchase when offers are single-use.

    Runs in the caller's transaction. A user who churns again later becomes eligible for a new offer.
    """
    if settings.winback_usage != "once":
        return
    offers = await session.scalars(
        select(WinbackOffer).where(WinbackOffer.user_id == user_id, WinbackOffer.expires_at > now)
    )
    for offer in offers:
        offer.expires_at = now


async def invoice_price(session: AsyncSession, settings: Settings, user_id: int, plan: Plan, locked_kop: int) -> int:
    """The price to charge for a paid invoice whose price was locked at ``locked_kop`` when it was created.

    A single-use offer is spent by the first discounted purchase. Several invoices created while it was active must not
    all get it: once a discounted purchase followed the offer, an invoice priced below what the buyer is entitled to now
    (their group discount, if any) is charged that price instead (the payment stays on the balance if it does not
    cover it). An offer that merely ran out of time does not take the discount away from an invoice made in time.
    """
    from app.services import promo  # local: promo imports the models this module uses

    if settings.winback_usage != "once" or locked_kop >= plan.price_kop:
        return locked_kop
    entitled = (await promo.personal_discount(session, user_id)).price(plan.price_kop)
    if locked_kop >= entitled:
        return locked_kop
    offer_sent = await session.scalar(select(func.max(WinbackOffer.sent_at)).where(WinbackOffer.user_id == user_id))
    if offer_sent is None:
        return locked_kop
    discounted_since = await session.scalar(
        select(
            exists().where(
                Purchase.user_id == user_id,
                Purchase.created_at >= offer_sent,
                Purchase.price_kop > 0,
                Purchase.plan_id == Plan.id,
                Purchase.price_kop < Plan.price_kop,
            )
        )
    )
    return entitled if discounted_since else locked_kop


async def _trial_users(session: AsyncSession, now: datetime, wait_days: int) -> list[int]:
    paid = exists().where(
        Purchase.user_id == User.tg_id,
        Purchase.kind.in_((PurchaseKind.PURCHASE, PurchaseKind.AUTO_RENEW)),
    )
    has_sub = exists().where(Subscription.user_id == User.tg_id)
    rows = await session.scalars(
        select(User.tg_id).where(
            User.created_at <= now - timedelta(days=max(0, wait_days)),
            User.trial_used.is_(False),
            User.is_banned.is_(False),
            User.bot_blocked.is_(False),
            ~paid,
            ~has_sub,
        )
    )
    return list(rows)


async def _churned_users(session: AsyncSession, now: datetime, wait_days: int) -> list[int]:
    cutoff = now - timedelta(days=max(0, wait_days))
    ended = exists().where(
        Subscription.user_id == User.tg_id,
        Subscription.is_trial.is_(False),
        Subscription.expires_at <= cutoff,
    )
    active = exists().where(
        Subscription.user_id == User.tg_id,
        Subscription.status == SubscriptionStatus.ACTIVE,
        Subscription.expires_at > now,
    )
    rows = await session.scalars(
        select(User.tg_id).where(User.is_banned.is_(False), User.bot_blocked.is_(False), ended, ~active)
    )
    return list(rows)
