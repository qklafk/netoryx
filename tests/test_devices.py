"""Devices: listing per panel, reset all / one, cooldown."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import AdminAuditLog, PanelKind, Provision, ProvisionState
from app.db.session import SessionFactory
from app.services import devices
from app.services.devices import DeviceInfo, DeviceNotSupported, ResetCooldown
from app.services.panels.base import PanelError, RemoteAccount
from tests.conftest import make_settings
from tests.fakes import FakePanel
from tests.test_subs_support import NOW, FakeRegistry, add_rw_panel, add_sub, add_user, add_xui_panel, days

ADMIN = 1000


@dataclass
class HwidPanel(FakePanel):
    """Remnawave-like backend with HWID device listing and deletion."""

    devices: dict[str, list[DeviceInfo]] = field(default_factory=dict)
    fail_list: bool = False

    async def list_devices(self, account: RemoteAccount) -> list[DeviceInfo]:
        if self.fail_list:
            raise PanelError("hwid api down")
        return list(self.devices.get(account.email, []))

    async def delete_device(self, account: RemoteAccount, device_id: str) -> None:
        self.calls.append(("delete_device", None, f"{account.email}:{device_id}"))
        self.devices[account.email] = [d for d in self.devices.get(account.email, []) if d.id != device_id]


async def _world(session_factory: SessionFactory) -> tuple[FakeRegistry, dict[str, int]]:
    async with session_factory.begin() as s:
        xui, (inbound,) = await add_xui_panel(s)
        rw = await add_rw_panel(s)
        await add_user(s, 1)
        sub = await add_sub(s, 1, expires_at=NOW + days(10))
        for panel_id, inbound_id, email, state in (
            (xui.id, inbound.id, "tg1-1", ProvisionState.ENABLED),
            (rw.id, None, "tg_1", ProvisionState.ENABLED),
        ):
            s.add(
                Provision(
                    subscription_id=sub.id,
                    panel_id=panel_id,
                    inbound_id=inbound_id,
                    inbound_key=inbound_id or 0,
                    remote_email=email,
                    remote_id="7" if inbound_id is None else None,
                    desired_state=state,
                    actual_state=state,
                )
            )
    rw_fake = HwidPanel(kind=PanelKind.REMNAWAVE)
    rw_fake.devices = {"tg_1": [DeviceInfo("hw-1", platform="iOS"), DeviceInfo("hw-2", platform="Android")]}
    registry = FakeRegistry({xui.id: FakePanel(), rw.id: rw_fake})
    return registry, {"xui": xui.id, "rw": rw.id, "sub": sub.id}


async def test_list_devices_per_panel(session_factory: SessionFactory) -> None:
    registry, ids = await _world(session_factory)
    result = {item.panel_id: item for item in await devices.list_devices(session_factory, registry, ids["sub"])}
    xui, rw = result[ids["xui"]], result[ids["rw"]]
    assert (xui.supported, xui.can_delete_one, xui.devices) == (True, True, [])  # the bot tracks 3x-ui devices
    assert (rw.supported, rw.can_delete_one) == (True, True)
    assert [d.id for d in rw.devices] == ["hw-1", "hw-2"]

    registry.panels[ids["rw"]].fail_list = True  # type: ignore[attr-defined]
    registry.broken.add(ids["xui"])
    result = {item.panel_id: item for item in await devices.list_devices(session_factory, registry, ids["sub"])}
    assert result[ids["rw"]].error == "hwid api down"
    assert result[ids["xui"]].error == "cannot build backend" and not result[ids["xui"]].supported


async def test_reset_all_with_cooldown(session_factory: SessionFactory) -> None:
    registry, ids = await _world(session_factory)
    cooldown = timedelta(minutes=30)
    result = await devices.reset(session_factory, registry, ids["sub"], cooldown=cooldown, now=NOW)
    assert result.ok and result.reset == 2
    for panel_id in (ids["xui"], ids["rw"]):
        assert ("reset_devices", None, "tg1-1" if panel_id == ids["xui"] else "tg_1") in registry.panels[panel_id].calls
    with pytest.raises(ResetCooldown) as exc:
        await devices.reset(session_factory, registry, ids["sub"], cooldown=cooldown, now=NOW + timedelta(minutes=10))
    assert exc.value.retry_after == NOW + cooldown
    assert await devices.last_reset(session_factory, ids["sub"]) == NOW
    # admins bypass the cooldown and are audited
    admin = await devices.reset(
        session_factory, registry, ids["sub"], cooldown=cooldown, actor_id=ADMIN, now=NOW + timedelta(minutes=11)
    )
    assert admin.reset == 2
    later = await devices.reset(session_factory, registry, ids["sub"], cooldown=cooldown, now=NOW + timedelta(hours=1))
    assert later.ok
    async with session_factory() as s:
        actions = (await s.scalars(select(AdminAuditLog.action))).all()
    assert actions == ["subscription.reset_devices"]


async def test_failed_reset_does_not_start_cooldown(session_factory: SessionFactory) -> None:
    registry, ids = await _world(session_factory)
    registry.broken.update({ids["xui"], ids["rw"]})
    result = await devices.reset(session_factory, registry, ids["sub"], now=NOW)
    assert (result.reset, result.failed, result.ok) == (0, 2, False)
    assert await devices.last_reset(session_factory, ids["sub"]) is None
    registry.broken.clear()
    assert (await devices.reset(session_factory, registry, ids["sub"], now=NOW)).ok


async def test_delete_one_device(session_factory: SessionFactory) -> None:
    registry, ids = await _world(session_factory)
    rw_fake = registry.panels[ids["rw"]]
    result = await devices.reset(session_factory, registry, ids["sub"], device_id="hw-1", panel_id=ids["rw"], now=NOW)
    assert result.ok and result.reset == 1
    assert [d.id for d in rw_fake.devices["tg_1"]] == ["hw-2"]  # type: ignore[attr-defined]
    with pytest.raises(DeviceNotSupported):
        await devices.reset(
            session_factory, registry, ids["sub"], device_id="1.2.3.4", panel_id=ids["xui"], actor_id=ADMIN, now=NOW
        )
    with pytest.raises(ValueError):
        await devices.reset(session_factory, registry, ids["sub"], device_id="x", now=NOW)


def test_cooldown_from_settings() -> None:
    assert devices.cooldown_from(make_settings()) == devices.DEFAULT_COOLDOWN
    assert devices.cooldown_from(make_settings(device_reset_cooldown_min=0)) == timedelta(0)
    assert devices.cooldown_from(make_settings(device_reset_cooldown_min=90)) == timedelta(minutes=90)
