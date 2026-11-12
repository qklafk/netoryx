"""Devices of a subscription: list them and unlink one or all (with a cooldown for users).

What a "device" is depends on the panel:

* Remnawave - HWID devices registered by client apps (list, delete one, delete all);
* 3x-ui, Marzban, Marzneshin, Hiddify - devices the bot saw on the subscription link (see
  :mod:`app.services.hwid`); they are listed under the first such server and can be removed one by one. With
  ``XUI_DEVICE_LIMIT=ip`` 3x-ui also reports the client IPs recorded for its ``limitIp`` counter (clear all).

Listing and deleting single devices are optional backend capabilities (:class:`DeviceLister`,
:class:`DeviceRemover`); panels without them report ``supported=False``. Resetting all devices uses
:meth:`PanelBackend.reset_devices`, which every backend has. Panels are called with no DB transaction open.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sqlalchemy import delete, select

from app.db.models import Inbound, Panel, PanelKind, Provision, ProvisionState, Subscription, SubscriptionDevice
from app.db.session import read_session
from app.services import audit, kv
from app.services.panels.base import PanelBackend, PanelError, RemoteAccount
from app.services.provisioning import PanelSource
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.config import Settings
    from app.db.session import SessionFactory

log = logging.getLogger(__name__)

DEFAULT_COOLDOWN = timedelta(minutes=30)
_COOLDOWN_KEY = "devices:reset:{sub_id}"
_MAX_ERROR = 200


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """One device as reported by a panel. ``id`` is what :meth:`DeviceRemover.delete_device` expects."""

    id: str
    platform: str | None = None
    model: str | None = None
    os_version: str | None = None
    user_agent: str | None = None
    ip: str | None = None
    created_at: datetime | None = None
    last_seen_at: datetime | None = None


@runtime_checkable
class DeviceLister(Protocol):
    async def list_devices(self, account: RemoteAccount) -> list[DeviceInfo]: ...


@runtime_checkable
class DeviceRemover(Protocol):
    async def delete_device(self, account: RemoteAccount, device_id: str) -> None: ...


@dataclass(slots=True)
class PanelDevices:
    panel_id: int
    panel_name: str
    kind: PanelKind
    supported: bool  # the panel can list devices
    can_delete_one: bool
    devices: list[DeviceInfo] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class ResetResult:
    reset: int = 0  # accounts whose devices were cleared (or the one device deleted)
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.reset > 0 and self.failed == 0


class DeviceError(Exception):
    pass


class ResetCooldown(DeviceError):
    """Devices were reset recently; ``retry_after`` says when it is allowed again."""

    def __init__(self, retry_after: datetime) -> None:
        super().__init__(f"devices were reset recently; retry after {retry_after.isoformat()}")
        self.retry_after = retry_after


class DeviceNotSupported(DeviceError):
    pass


@dataclass(frozen=True, slots=True)
class _Account:
    panel_id: int
    panel_name: str
    kind: PanelKind
    target: int | None
    account: RemoteAccount


# Panels whose devices are tracked by the bot itself.
_BOT_TRACKED = (PanelKind.XUI, PanelKind.MARZBAN, PanelKind.MARZNESHIN, PanelKind.HIDDIFY)


async def _tracked_owner(session_factory: SessionFactory, subscription_id: int) -> tuple[int, str, PanelKind] | None:
    """The panel under which bot-tracked devices are shown: the first active one the subscription has an account on."""
    async with read_session(session_factory) as session:
        row = (
            await session.execute(
                select(Panel.id, Panel.name, Panel.kind)
                .join(Provision, Provision.panel_id == Panel.id)
                .where(
                    Provision.subscription_id == subscription_id,
                    Provision.actual_state != ProvisionState.ABSENT,
                    Provision.remote_email.is_not(None),
                    Panel.is_active.is_(True),
                    Panel.kind.in_(_BOT_TRACKED),
                )
                .order_by(Panel.id)
                .limit(1)
            )
        ).first()
    return (row[0], row[1], row[2]) if row else None


async def _tracked_devices(session_factory: SessionFactory, subscription_id: int) -> list[DeviceInfo]:
    async with read_session(session_factory) as session:
        rows = (
            await session.scalars(
                select(SubscriptionDevice)
                .where(SubscriptionDevice.subscription_id == subscription_id)
                .order_by(SubscriptionDevice.first_seen_at, SubscriptionDevice.id)
            )
        ).all()
    return [
        DeviceInfo(
            id=str(row.id),
            platform=row.platform,
            model=row.model,
            os_version=row.os_version,
            created_at=row.first_seen_at,
            last_seen_at=row.last_seen_at,
            user_agent=row.app,
        )
        for row in rows
    ]


async def _forget_tracked(session_factory: SessionFactory, subscription_id: int, device_id: str | None) -> int:
    stmt = delete(SubscriptionDevice).where(SubscriptionDevice.subscription_id == subscription_id)
    if device_id is not None:
        if not device_id.isdecimal():
            return 0
        stmt = stmt.where(SubscriptionDevice.id == int(device_id))
    async with session_factory.begin() as session:
        result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


def cooldown_from(settings: Settings) -> timedelta:
    """Reset cooldown for users (``DEVICE_RESET_COOLDOWN_MIN``, default 30 minutes; 0 disables it)."""
    return timedelta(minutes=max(0, settings.device_reset_cooldown_min))


async def _accounts(session_factory: SessionFactory, subscription_id: int) -> list[_Account]:
    async with read_session(session_factory) as session:
        stmt = (
            select(Provision, Panel, Inbound.remote_id, Subscription.client_uuid)
            .join(Panel, Panel.id == Provision.panel_id)
            .join(Subscription, Subscription.id == Provision.subscription_id)
            .outerjoin(Inbound, Inbound.id == Provision.inbound_id)
            .where(
                Provision.subscription_id == subscription_id,
                Provision.actual_state != ProvisionState.ABSENT,
                Provision.remote_email.is_not(None),
                Panel.is_active.is_(True),
                Panel.kind.in_((PanelKind.XUI, PanelKind.REMNAWAVE)),
            )
            .order_by(Panel.id, Provision.id)
        )
        rows = (await session.execute(stmt)).all()
    result = []
    for provision, panel, remote_id, client_uuid in rows:
        account = RemoteAccount(
            email=provision.remote_email or "",
            uuid=client_uuid,
            enabled=True,
            expires_at=None,
            device_limit=0,
            traffic_limit_bytes=0,
            remote_id=provision.remote_id,
            short_id=provision.remote_short_id,
        )
        target = None if panel.kind == PanelKind.REMNAWAVE else remote_id
        result.append(_Account(panel.id, panel.name, panel.kind, target, account))
    return result


def _short(exc: BaseException) -> str:
    return (str(exc) or type(exc).__name__)[:_MAX_ERROR]


async def list_devices(
    session_factory: SessionFactory, registry: PanelSource, subscription_id: int
) -> list[PanelDevices]:
    """Devices per panel of the subscription. Never raises because of a panel (see ``error``)."""
    by_panel: dict[int, PanelDevices] = {}
    for item in await _accounts(session_factory, subscription_id):
        entry = by_panel.get(item.panel_id)
        try:
            backend: PanelBackend = await registry.get(item.panel_id)
        except (PanelError, LookupError) as exc:
            by_panel.setdefault(
                item.panel_id, PanelDevices(item.panel_id, item.panel_name, item.kind, False, False, error=_short(exc))
            )
            continue
        if entry is None:
            entry = by_panel[item.panel_id] = PanelDevices(
                item.panel_id,
                item.panel_name,
                item.kind,
                supported=isinstance(backend, DeviceLister),
                can_delete_one=isinstance(backend, DeviceRemover),
            )
        if not entry.supported:
            continue
        try:
            devices = await backend.list_devices(item.account)  # type: ignore[attr-defined]
        except PanelError as exc:
            entry.error = _short(exc)
            continue
        known = {d.id for d in entry.devices}
        entry.devices.extend(d for d in devices if d.id not in known)
    owner = await _tracked_owner(session_factory, subscription_id)
    if owner is not None:
        entry = by_panel.get(owner[0]) or by_panel.setdefault(
            owner[0], PanelDevices(owner[0], owner[1], owner[2], supported=True, can_delete_one=True)
        )
        if entry.supported or entry.error is None:  # a panel that could not be reached at all stays unsupported
            entry.supported = entry.can_delete_one = True
            entry.devices[:0] = await _tracked_devices(session_factory, subscription_id)
    return list(by_panel.values())


def _cooldown_key(subscription_id: int) -> str:
    return _COOLDOWN_KEY.format(sub_id=subscription_id)


async def last_reset(session_factory: SessionFactory, subscription_id: int) -> datetime | None:
    async with read_session(session_factory) as session:
        raw = await kv.get(session, _cooldown_key(subscription_id))
    return datetime.fromisoformat(raw) if raw else None


async def _claim(
    session_factory: SessionFactory, subscription_id: int, cooldown: timedelta, now: datetime
) -> str | None:
    """Check and record the reset time; returns the previous value (to restore if nothing was reset)."""
    async with session_factory.begin() as session:
        key = _cooldown_key(subscription_id)
        previous = await kv.get(session, key)
        if previous and cooldown > timedelta(0):
            ready_at = datetime.fromisoformat(previous) + cooldown
            if ready_at > now:
                raise ResetCooldown(ready_at)
        await kv.set(session, key, now.isoformat())
    return previous


async def _restore(session_factory: SessionFactory, subscription_id: int, previous: str | None) -> None:
    async with session_factory.begin() as session:
        key = _cooldown_key(subscription_id)
        if previous is None:
            await kv.delete(session, key)
        else:
            await kv.set(session, key, previous)


async def reset(
    session_factory: SessionFactory,
    registry: PanelSource,
    subscription_id: int,
    *,
    device_id: str | None = None,
    panel_id: int | None = None,
    cooldown: timedelta = DEFAULT_COOLDOWN,
    actor_id: int | None = None,
    source: str = "bot",
    ip: str | None = None,
    now: datetime | None = None,
) -> ResetResult:
    """Unlink all devices of the subscription (or one ``device_id`` on ``panel_id``).

    Users are limited by ``cooldown`` (:class:`ResetCooldown`); admins (``actor_id`` set) bypass it and are
    audited. Raises :class:`DeviceNotSupported` when deleting one device is not possible on that panel.
    """
    now = now or utcnow()
    if device_id is not None and panel_id is None:
        raise ValueError("deleting one device needs panel_id")
    accounts = [a for a in await _accounts(session_factory, subscription_id) if panel_id in (None, a.panel_id)]
    owner = await _tracked_owner(session_factory, subscription_id)
    tracked_one = device_id is not None and owner is not None and owner[0] == panel_id and device_id.isdecimal()
    if tracked_one:
        accounts = []  # a device the bot registered: nothing to delete on the panel
    previous = await _claim(session_factory, subscription_id, timedelta(0) if actor_id else cooldown, now)
    result = ResetResult()
    for item in accounts:
        try:
            backend = await registry.get(item.panel_id)
            if device_id is None:
                await backend.reset_devices(item.account)
            elif isinstance(backend, DeviceRemover):
                await backend.delete_device(item.account, device_id)
            else:
                await _restore(session_factory, subscription_id, previous)
                raise DeviceNotSupported(f"panel {item.panel_id} cannot delete a single device")
            result.reset += 1
        except (PanelError, LookupError) as exc:
            result.failed += 1
            result.errors.append(f"panel {item.panel_id}: {_short(exc)}")
            log.warning("device reset for subscription %s on panel %s failed: %s", subscription_id, item.panel_id, exc)
    covers_tracked = tracked_one or (device_id is None and owner is not None and panel_id in (None, owner[0]))
    # Devices the bot registered are forgotten along with the panels' own; on their own they count as a reset.
    if covers_tracked and (tracked_one or not accounts or result.reset > 0):
        await _forget_tracked(session_factory, subscription_id, device_id if tracked_one else None)
        if tracked_one or not accounts:
            result.reset += 1
    if result.reset == 0:
        await _restore(session_factory, subscription_id, previous)
    if actor_id is not None:
        async with session_factory.begin() as session:
            await audit.log(
                session,
                actor_id,
                "subscription.reset_devices",
                "subscription",
                subscription_id,
                source,
                ip,
                device_id=device_id,
                panel_id=panel_id,
                reset=result.reset,
                failed=result.failed,
            )
    return result
