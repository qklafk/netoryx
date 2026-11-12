"""Bot-side device limit based on the ``x-hwid`` header that subscription clients send.

Used for panels that cannot count devices themselves (3x-ui, Marzban, Marzneshin). Nothing identifying the
customer's network is involved: the hardware id is stored only as a keyed hash (HMAC-SHA256 under a key derived from
``SECRET_KEY``), together with the model / platform strings the client announces. Client addresses are never read.

:func:`check_device` runs in its own short transactions and never touches the network.
"""

from __future__ import annotations

import enum
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.db.models import Subscription, SubscriptionDevice
from app.db.session import SessionFactory, read_session
from app.utils.crypto import SecretBox
from app.utils.time import utcnow

log = logging.getLogger(__name__)

LAST_SEEN_REFRESH = timedelta(hours=1)  # last_seen_at is written at most this often per device
HWID_MAX_LEN = 256
_APP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]*")
_DEVICE_FIELDS = (("platform", "x-device-os", 32), ("os_version", "x-ver-os", 32), ("model", "x-device-model", 64))


class DeviceVerdict(enum.StrEnum):
    ALLOWED = "allowed"
    LIMIT_REACHED = "limit_reached"  # a new device, and the limit is used up
    UNSUPPORTED = "unsupported"  # the app sends no HWID and the operator requires one


@dataclass(frozen=True, slots=True)
class DeviceCheck:
    verdict: DeviceVerdict
    registered: bool = False  # the device was added by this call


def hwid_digest(secret_box: SecretBox, hwid: str) -> str:
    """Keyed hash (hex) under which a device is stored; the raw hardware id is never persisted."""
    return secret_box.sign("hwid:" + hwid[:HWID_MAX_LEN])


def _clip(value: str | None, limit: int) -> str | None:
    text = " ".join((value or "").split())[:limit].strip()
    return text or None


def parse_app(user_agent: str | None) -> str | None:
    """Client application name from a user agent such as ``Happ/2.4.1`` (None if it does not look like one)."""
    if not user_agent:
        return None
    token = user_agent.strip().split("/", 1)[0].split(" ", 1)[0]
    match = _APP_NAME.fullmatch(token)
    return _clip(match.group(0), 64) if match else None


def device_fields(headers: Mapping[str, str]) -> dict[str, str | None]:
    """Platform / OS version / model / app announced by the client (lowercase header names)."""
    fields = {name: _clip(headers.get(header), size) for name, header, size in _DEVICE_FIELDS}
    fields["app"] = parse_app(headers.get("user-agent"))
    return fields


async def check_device(
    session_factory: SessionFactory,
    secret_box: SecretBox,
    subscription_id: int,
    limit: int,
    headers: Mapping[str, str],
    *,
    require_hwid: bool = False,
    now: datetime | None = None,
) -> DeviceCheck:
    """Decide whether the requesting device may receive servers; registers a new one while there is room."""
    hwid = headers.get("x-hwid", "").strip()
    if not hwid:
        return DeviceCheck(DeviceVerdict.UNSUPPORTED if require_hwid else DeviceVerdict.ALLOWED)
    if limit <= 0:
        return DeviceCheck(DeviceVerdict.ALLOWED)
    now = now or utcnow()
    digest = hwid_digest(secret_box, hwid)
    async with read_session(session_factory) as session:
        seen = await session.scalar(
            select(SubscriptionDevice.last_seen_at).where(
                SubscriptionDevice.subscription_id == subscription_id, SubscriptionDevice.hwid_hash == digest
            )
        )
    if seen is not None and now - seen < LAST_SEEN_REFRESH:
        return DeviceCheck(DeviceVerdict.ALLOWED)
    fields = device_fields(headers)
    async with session_factory.begin() as session:
        row = await session.scalar(
            select(SubscriptionDevice).where(
                SubscriptionDevice.subscription_id == subscription_id, SubscriptionDevice.hwid_hash == digest
            )
        )
        if row is not None:
            row.last_seen_at = now
            for name, value in fields.items():
                if value:
                    setattr(row, name, value)
            return DeviceCheck(DeviceVerdict.ALLOWED)
        # Serialise new devices of one subscription so that concurrent requests cannot both take the last slot.
        lock = select(Subscription.id).where(Subscription.id == subscription_id).with_for_update()
        locked = await session.scalar(lock)
        if locked is None:
            return DeviceCheck(DeviceVerdict.ALLOWED)
        count = int(
            await session.scalar(
                select(func.count())
                .select_from(SubscriptionDevice)
                .where(SubscriptionDevice.subscription_id == subscription_id)
            )
            or 0
        )
        if count >= limit:
            return DeviceCheck(DeviceVerdict.LIMIT_REACHED)
        try:
            async with session.begin_nested():
                session.add(
                    SubscriptionDevice(
                        subscription_id=subscription_id,
                        hwid_hash=digest,
                        first_seen_at=now,
                        last_seen_at=now,
                        **fields,
                    )
                )
        except IntegrityError:
            return DeviceCheck(DeviceVerdict.ALLOWED)  # the same device registered concurrently
    return DeviceCheck(DeviceVerdict.ALLOWED, registered=True)


async def purge_stale(session_factory: SessionFactory, ttl_days: int, now: datetime | None = None) -> int:
    """Forget devices not seen for ``ttl_days`` (0 = keep forever); returns how many were removed."""
    if ttl_days <= 0:
        return 0
    cutoff = (now or utcnow()) - timedelta(days=ttl_days)
    async with session_factory.begin() as session:
        result = await session.execute(delete(SubscriptionDevice).where(SubscriptionDevice.last_seen_at < cutoff))
    return int(getattr(result, "rowcount", 0) or 0)
