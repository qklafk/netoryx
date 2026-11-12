"""Cost sharing for a private VPN: once a month every member is reminded of their share of the server bill.

Configuration (read leniently, the feature is off unless enabled): ``COST_SHARING_ENABLED``,
``COST_SHARING_TOTAL_RUB`` (monthly bill), ``COST_SHARING_DAY`` (1-28, day of the reminder),
``COST_SHARING_DETAILS`` (how to pay: card, phone...), ``COST_SHARING_INCLUDE_OWNER`` (the owner pays a share too).

Each month is a record in ``kv`` (``cost:<YYYY-MM>``): the share, who was asked and who marked the payment as done.
Nothing here moves money: members pay the owner directly and press "I paid".
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.models import KeyValue, Subscription, SubscriptionStatus, User
from app.db.session import read_session
from app.services import kv

if TYPE_CHECKING:
    from app.config import Settings
    from app.db.session import SessionFactory

KEY_PREFIX = "cost:"


@dataclass(frozen=True, slots=True)
class Config:
    enabled: bool = False
    total_kop: int = 0
    day: int = 1
    details: str = ""
    include_owner: bool = True

    @property
    def ready(self) -> bool:
        return self.enabled and self.total_kop > 0


def config_from(settings: Settings) -> Config:
    details = settings.cost_sharing_details
    return Config(
        enabled=settings.cost_sharing_enabled,
        total_kop=max(0, settings.cost_sharing_total_rub) * 100,
        day=min(max(settings.cost_sharing_day, 1), 28),
        details=details.strip() if details else "",
        include_owner=settings.cost_sharing_include_owner,
    )


def period_of(moment: datetime, settings: Settings) -> str:
    local = moment.astimezone(settings.zone)
    return f"{local.year:04d}-{local.month:02d}"


def share_kop(total_kop: int, people: int) -> int:
    """Equal share rounded up to whole rubles (the owner never ends up short)."""
    if people <= 0 or total_kop <= 0:
        return 0
    return int(math.ceil(total_kop / people / 100) * 100)


@dataclass(slots=True)
class Period:
    period: str
    total_kop: int
    share_kop: int
    members: list[int] = field(default_factory=list)
    paid: list[int] = field(default_factory=list)

    @property
    def unpaid(self) -> list[int]:
        return [m for m in self.members if m not in self.paid]

    def to_json(self) -> str:
        return json.dumps(
            {"total": self.total_kop, "share": self.share_kop, "members": self.members, "paid": self.paid},
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, period: str, raw: str | None) -> Period | None:
        try:
            data: Any = json.loads(raw) if raw else None
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None

        def ids(value: Any) -> list[int]:
            return [int(v) for v in value if isinstance(v, int)] if isinstance(value, list) else []

        return cls(
            period,
            int(data.get("total") or 0),
            int(data.get("share") or 0),
            ids(data.get("members")),
            ids(data.get("paid")),
        )


def key_of(period: str) -> str:
    return f"{KEY_PREFIX}{period}"


async def members_to_ask(session_factory: SessionFactory, settings: Settings, now: datetime) -> list[int]:
    """Members with access right now (owners from ``ADMIN_IDS`` excluded: they collect the money)."""
    async with read_session(session_factory) as session:
        rows = await session.scalars(
            select(User.tg_id)
            .join(Subscription, Subscription.user_id == User.tg_id)
            .where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at > now,
                User.is_banned.is_(False),
            )
            .order_by(User.tg_id)
        )
        owners = set(settings.admin_ids)
        return [uid for uid in rows if uid not in owners]


async def get_period(session_factory: SessionFactory, period: str) -> Period | None:
    async with read_session(session_factory) as session:
        return Period.from_json(period, await kv.get(session, key_of(period)))


async def start_period(
    session_factory: SessionFactory, settings: Settings, config: Config, period: str, now: datetime
) -> Period | None:
    """Create the month's record (once); None if it already exists or there is nobody to ask."""
    members = await members_to_ask(session_factory, settings, now)
    if not members or not config.ready:
        return None
    people = len(members) + (1 if config.include_owner else 0)
    record = Period(period, config.total_kop, share_kop(config.total_kop, people), members)
    async with session_factory.begin() as session:
        if not await kv.add(session, key_of(period), record.to_json()):
            return None
    return record


async def mark_paid(session_factory: SessionFactory, period: str, user_id: int, paid: bool = True) -> Period | None:
    """Record (or undo) a member's payment; None if the month or the member is unknown."""
    async with session_factory.begin() as session:
        row = await session.get(KeyValue, key_of(period), with_for_update=True)
        record = Period.from_json(period, row.value if row is not None else None)
        if row is None or record is None or user_id not in record.members:
            return None
        if paid and user_id not in record.paid:
            record.paid.append(user_id)
        elif not paid and user_id in record.paid:
            record.paid.remove(user_id)
        row.value = record.to_json()
    return record


async def names(session_factory: SessionFactory, ids: list[int]) -> dict[int, User]:
    if not ids:
        return {}
    async with read_session(session_factory) as session:
        return {u.tg_id: u for u in await session.scalars(select(User).where(User.tg_id.in_(ids)))}
