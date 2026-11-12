"""Stopping a native Telegram Stars subscription (30-day recurring invoice) linked to a user's subscription.

Telegram keeps charging such a subscription until the bot cancels it with ``editUserStarSubscription``; switching
off auto-renewal in the bot alone would not stop the charges.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from app.db.models import Subscription
from app.db.session import SessionFactory, read_session
from app.services.payments import stars

log = logging.getLogger(__name__)


async def linked_charge(session_factory: SessionFactory, user_id: int) -> str | None:
    """Charge id of the Stars subscription that renews the user's subscription, if any."""
    async with read_session(session_factory) as session:
        return await session.scalar(select(Subscription.stars_charge_id).where(Subscription.user_id == user_id))


async def cancel_stars_renewal(
    bot: Any, session_factory: SessionFactory, user_id: int, subscription_id: int | None = None
) -> bool:
    """Cancel the user's Stars subscription and unlink it. False if Telegram could not be told (try again later).

    Must be called outside of a database transaction (it calls the Bot API).
    """
    async with read_session(session_factory) as session:
        stmt = select(Subscription.stars_charge_id).where(
            Subscription.user_id == user_id, Subscription.stars_charge_id.is_not(None)
        )
        if subscription_id is not None:
            stmt = stmt.where(Subscription.id == subscription_id)
        charges = list(await session.scalars(stmt))
    for charge_id in charges:
        if charge_id is None:
            continue
        try:
            await bot.edit_user_star_subscription(
                user_id=user_id, telegram_payment_charge_id=charge_id, is_canceled=True
            )
        except TelegramBadRequest as exc:
            log.warning("stars: subscription %s of user %s was not canceled: %s", charge_id, user_id, exc.message)
        except Exception:
            log.exception("stars: canceling subscription %s of user %s failed", charge_id, user_id)
            return False
        await stars.unlink_recurring(session_factory, user_id, charge_id)
        log.info("stars: subscription %s of user %s canceled", charge_id, user_id)
    return True
