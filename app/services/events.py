"""Business analytics events (append-only ``events`` table): funnel, conversions, churn.

Event types in use: ``user.registered``, ``referral.applied``, ``referral.reward``, ``promo.redeemed``,
``gift.purchased``, ``gift.redeemed``, ``trial.started``, ``purchase``, ``renewal``, ``payment.succeeded``,
``subscription.expired``.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event
from app.services import webhooks
from app.utils.time import utcnow

MAX_TYPE_LENGTH = 48


def _encode(data: dict[str, Any]) -> str | None:
    if not data:
        return None
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


async def track(
    session: AsyncSession,
    type: str,
    user_id: int | None = None,
    amount_kop: int | None = None,
    **data: Any,
) -> Event:
    """Record an event in the caller's transaction. Extra keyword arguments are stored as JSON.

    Non-JSON values (datetimes, enums, Decimals) are stored via ``str()``. Never put secrets in ``data``.
    Matching outgoing webhooks are queued in the same transaction (see :mod:`app.services.webhooks`).
    """
    if not type or len(type) > MAX_TYPE_LENGTH:
        raise ValueError(f"event type must be 1..{MAX_TYPE_LENGTH} characters")
    now = utcnow()
    event = Event(type=type, user_id=user_id, amount_kop=amount_kop, data=_encode(data), created_at=now)
    session.add(event)
    await webhooks.enqueue_event(session, type, created_at=now, user_id=user_id, amount_kop=amount_kop, data=data)
    return event
