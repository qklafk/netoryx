"""Advertising campaigns: tracked ``/start c_<code>`` links with registrations, trials, purchases and revenue.

Campaign definitions live in ``kv`` (``campaign:<CODE>`` -> JSON). Attribution is an analytics event
``campaign.joined`` (``data = {"campaign": CODE}``) written once for a newly registered user; every number in the
report is derived from that event joined with purchases and payments, so nothing else has to know about campaigns.
Functions take the caller's ``session`` and never commit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, KeyValue, Payment, PaymentStatus, Purchase, PurchaseKind
from app.services import audit, events, kv
from app.utils.time import utcnow

JOIN_EVENT = "campaign.joined"
DEEP_LINK_PREFIX = "c_"
MAX_NAME_LENGTH = 64
_KEY_PREFIX = "campaign:"
_CODE_PATTERN = re.compile(r"[A-Z0-9_-]{2,32}")
_PAID_KINDS = (PurchaseKind.PURCHASE, PurchaseKind.AUTO_RENEW)


class CampaignExists(ValueError):
    """The campaign code is already taken."""


@dataclass(frozen=True, slots=True)
class Campaign:
    code: str
    name: str
    created_by: int | None
    created_at: datetime
    is_active: bool = True

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "created_by": self.created_by,
                "created_at": self.created_at.isoformat(),
                "is_active": self.is_active,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, code: str, raw: str) -> Campaign:
        data = json.loads(raw)
        return cls(
            code=code,
            name=str(data.get("name") or code),
            created_by=data.get("created_by"),
            created_at=datetime.fromisoformat(data["created_at"]),
            is_active=bool(data.get("is_active", True)),
        )


@dataclass(frozen=True, slots=True)
class CampaignStats:
    code: str
    name: str
    is_active: bool
    registrations: int
    trials: int
    paying_users: int  # users who made at least one paid purchase (first purchases)
    purchases: int  # all paid purchases
    revenue_kop: int  # credited payments (donations excluded)

    @property
    def conversion(self) -> float:
        """Share of registrations that became paying users."""
        return round(self.paying_users / self.registrations, 4) if self.registrations else 0.0


def normalize_code(code: str | None) -> str:
    """Upper-cased code without the ``c_`` deep-link prefix."""
    text = (code or "").strip()
    if text.lower().startswith(DEEP_LINK_PREFIX):
        text = text[len(DEEP_LINK_PREFIX) :]
    return text.upper()


def deep_link_payload(code: str) -> str:
    return f"{DEEP_LINK_PREFIX}{normalize_code(code)}"


def _key(code: str) -> str:
    return f"{_KEY_PREFIX}{code}"


def _event_data(code: str) -> str:
    """``Event.data`` of an attribution (same encoding as :func:`app.services.events.track`)."""
    return json.dumps({"campaign": code}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _code_from_data(data: str | None) -> str | None:
    if not data:
        return None
    try:
        value = json.loads(data).get("campaign")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


async def create(
    session: AsyncSession, code: str, name: str, actor_id: int, *, source: str = "bot", ip: str | None = None
) -> Campaign:
    """Create a campaign. Raises ``ValueError`` for a bad code and :class:`CampaignExists`."""
    normalized = normalize_code(code)
    if not _CODE_PATTERN.fullmatch(normalized):
        raise ValueError("code must be 2-32 characters: letters, digits, '_' or '-'")
    campaign = Campaign(normalized, (name or normalized).strip()[:MAX_NAME_LENGTH], actor_id, utcnow())
    if not await kv.add(session, _key(normalized), campaign.to_json()):
        raise CampaignExists(normalized)
    await audit.log(session, actor_id, "campaign.create", "campaign", normalized, source, ip, name=campaign.name)
    return campaign


async def get(session: AsyncSession, code: str) -> Campaign | None:
    normalized = normalize_code(code)
    if not normalized:
        return None
    raw = await kv.get(session, _key(normalized))
    return Campaign.from_json(normalized, raw) if raw else None


async def list_campaigns(session: AsyncSession) -> list[Campaign]:
    """All campaigns, newest first."""
    rows = await session.execute(select(KeyValue.key, KeyValue.value).where(KeyValue.key.startswith(_KEY_PREFIX)))
    items = [Campaign.from_json(key[len(_KEY_PREFIX) :], value) for key, value in rows.all()]
    return sorted(items, key=lambda c: c.created_at, reverse=True)


async def set_active(
    session: AsyncSession, code: str, active: bool, actor_id: int, *, source: str = "bot", ip: str | None = None
) -> Campaign | None:
    """Pause or resume attribution for a campaign (its statistics stay)."""
    campaign = await get(session, code)
    if campaign is None:
        return None
    updated = Campaign(campaign.code, campaign.name, campaign.created_by, campaign.created_at, active)
    await kv.set(session, _key(campaign.code), updated.to_json())
    await audit.log(session, actor_id, "campaign.set_active", "campaign", campaign.code, source, ip, active=active)
    return updated


async def campaign_of(session: AsyncSession, user_id: int) -> str | None:
    """The campaign a user came from, if any."""
    data = await session.scalar(
        select(Event.data).where(Event.type == JOIN_EVENT, Event.user_id == user_id).order_by(Event.id).limit(1)
    )
    return _code_from_data(data)


async def attribute(session: AsyncSession, user_id: int, code: str | None, *, is_new: bool) -> bool:
    """Attribute a just-registered user to an active campaign (``/start c_<code>``). Once per user."""
    if not is_new:
        return False
    campaign = await get(session, code or "")
    if campaign is None or not campaign.is_active:
        return False
    if await campaign_of(session, user_id) is not None:
        return False
    await events.track(session, JOIN_EVENT, user_id=user_id, campaign=campaign.code)
    return True


def _joined(code: str | None, since: datetime | None) -> Any:
    stmt: Select[int | None, str | None] = select(Event.user_id.label("user_id"), Event.data.label("data")).where(
        Event.type == JOIN_EVENT, Event.user_id.is_not(None)
    )
    if code is not None:
        stmt = stmt.where(Event.data == _event_data(code))
    if since is not None:
        stmt = stmt.where(Event.created_at >= since)
    return stmt.subquery()


async def _grouped(session: AsyncSession, stmt: Select[Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for data, value in (await session.execute(stmt)).all():
        code = _code_from_data(data)
        if code is not None:
            result[code] = result.get(code, 0) + int(value or 0)
    return result


async def stats(
    session: AsyncSession, *, code: str | None = None, since: datetime | None = None
) -> list[CampaignStats]:
    """Funnel per campaign (all campaigns, or one ``code``); ``since`` limits by attribution time."""
    normalized = normalize_code(code) if code is not None else None
    joined = _joined(normalized, since)
    data = joined.c.data
    registrations = await _grouped(session, select(data, func.count()).group_by(data))
    trials = await _grouped(
        session,
        select(data, func.count(func.distinct(Purchase.user_id)))
        .join(Purchase, Purchase.user_id == joined.c.user_id)
        .where(Purchase.kind == PurchaseKind.TRIAL)
        .group_by(data),
    )
    paid_filter = (Purchase.kind.in_(_PAID_KINDS), Purchase.price_kop > 0)
    paying = await _grouped(
        session,
        select(data, func.count(func.distinct(Purchase.user_id)))
        .join(Purchase, Purchase.user_id == joined.c.user_id)
        .where(*paid_filter)
        .group_by(data),
    )
    purchases = await _grouped(
        session,
        select(data, func.count(Purchase.id))
        .join(Purchase, Purchase.user_id == joined.c.user_id)
        .where(*paid_filter)
        .group_by(data),
    )
    revenue = await _grouped(
        session,
        select(data, func.coalesce(func.sum(Payment.amount_kop), 0))
        .join(Payment, Payment.user_id == joined.c.user_id)
        .where(
            Payment.status == PaymentStatus.SUCCEEDED,
            Payment.credited_at.is_not(None),
            Payment.is_donation.is_(False),
        )
        .group_by(data),
    )
    campaigns = {c.code: c for c in await list_campaigns(session)}
    codes = [normalized] if normalized is not None else sorted(set(campaigns) | set(registrations))
    result = []
    for item in codes:
        campaign = campaigns.get(item)
        result.append(
            CampaignStats(
                code=item,
                name=campaign.name if campaign else item,
                is_active=campaign.is_active if campaign else False,
                registrations=registrations.get(item, 0),
                trials=trials.get(item, 0),
                paying_users=paying.get(item, 0),
                purchases=purchases.get(item, 0),
                revenue_kop=revenue.get(item, 0),
            )
        )
    result.sort(key=lambda s: (s.registrations, s.revenue_kop), reverse=True)
    return result
