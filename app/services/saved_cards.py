"""Cards users allowed to be charged for auto-renewal.

A card is stored only when all of these hold: saving cards is enabled (``SAVED_CARDS_ENABLED``), the user ticked
"save card" for that very payment (``Payment.save_method``) and the provider reported the reusable method in a
verified status (a re-fetched payment, never an unverified webhook body). The stored value is the provider's token,
never a card number, and it is never logged.

Removing a card deactivates the row; a later redelivery of the payment that saved it does not bring it back, a new
payment with consent does.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Payment, SavedPaymentMethod, Subscription
from app.db.session import SessionFactory
from app.services.payments.base import SavedMethod
from app.services.subscriptions import renewal_ref

log = logging.getLogger(__name__)

_AUTOPAY_NAMESPACE = uuid.UUID("6f1c2a52-8d0e-4c1e-9a55-3b7f2f1d9c41")


def _now() -> datetime:
    return datetime.now(UTC)


def autopay_payment_id(sub: Subscription) -> str:
    """Payment id of the card charge for the period ending at ``sub.expires_at``: one charge per period."""
    return str(uuid.uuid5(_AUTOPAY_NAMESPACE, renewal_ref(sub)))


async def remember(session_factory: SessionFactory, settings: Settings, payment: Payment, saved: SavedMethod) -> bool:
    """Store the card a paid payment saved, if the user agreed to it. Returns whether a row is active now."""
    if not settings.saved_cards_enabled or not payment.save_method or not saved.token:
        return False
    token = saved.token[:255]
    async with session_factory.begin() as s:
        existing = await s.scalar(
            select(SavedPaymentMethod)
            .where(SavedPaymentMethod.provider == payment.provider, SavedPaymentMethod.token == token)
            .with_for_update()
        )
        if existing is None:
            try:
                async with s.begin_nested():
                    s.add(
                        SavedPaymentMethod(
                            user_id=payment.user_id,
                            provider=payment.provider,
                            token=token,
                            title=saved.title[:64] or payment.provider,
                        )
                    )
            except IntegrityError:
                return False  # stored concurrently
            log.info("payment %s: saved a %s card for user %s", payment.id, payment.provider, payment.user_id)
            return True
        if existing.user_id != payment.user_id:
            log.warning("payment %s: a saved %s card belongs to another user", payment.id, payment.provider)
            return False
        if (
            not existing.is_active
            and existing.deactivated_at is not None
            and existing.deactivated_at < payment.created_at
        ):
            existing.is_active = True  # saved again with fresh consent after it was removed
            existing.deactivated_at = None
            existing.title = saved.title[:64] or existing.title
        return existing.is_active


async def list_active(session: AsyncSession, user_id: int) -> list[SavedPaymentMethod]:
    rows = await session.scalars(
        select(SavedPaymentMethod)
        .where(SavedPaymentMethod.user_id == user_id, SavedPaymentMethod.is_active.is_(True))
        .order_by(SavedPaymentMethod.created_at.desc(), SavedPaymentMethod.id.desc())
    )
    return list(rows.all())


async def pick(session: AsyncSession, user_id: int, providers: list[str]) -> SavedPaymentMethod | None:
    """The card to charge: the most recently used (else saved) active card of an available provider."""
    for method in sorted(
        await list_active(session, user_id),
        key=lambda m: (m.last_used_at or m.created_at, m.id),
        reverse=True,
    ):
        if method.provider in providers:
            return method
    return None


async def deactivate(session: AsyncSession, user_id: int, method_id: int) -> bool:
    """Remove one of the user's cards (the row is kept inactive). False if it is not theirs or already removed."""
    result = await session.execute(
        update(SavedPaymentMethod)
        .where(
            SavedPaymentMethod.id == method_id,
            SavedPaymentMethod.user_id == user_id,
            SavedPaymentMethod.is_active.is_(True),
        )
        .values(is_active=False, deactivated_at=_now())
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def deactivate_invalid(session_factory: SessionFactory, method_id: int) -> None:
    """The provider will never accept this card again (revoked, expired, lost)."""
    async with session_factory.begin() as s:
        await s.execute(
            update(SavedPaymentMethod)
            .where(SavedPaymentMethod.id == method_id, SavedPaymentMethod.is_active.is_(True))
            .values(is_active=False, deactivated_at=_now())
        )


async def mark_used(session_factory: SessionFactory, method_id: int) -> None:
    async with session_factory.begin() as s:
        await s.execute(
            update(SavedPaymentMethod).where(SavedPaymentMethod.id == method_id).values(last_used_at=_now())
        )
