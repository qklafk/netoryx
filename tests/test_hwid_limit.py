"""Bot-side HWID device limit: registration, enforcement in /sub, panel limits, device screens, cleanup."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta
from urllib.parse import unquote, urlsplit

import pytest
from sqlalchemy import select, text

from app.config import Settings
from app.db.models import (
    Endpoint,
    EndpointKind,
    EndpointProfile,
    Panel,
    PanelKind,
    Provision,
    ProvisionState,
    Subscription,
    SubscriptionDevice,
)
from app.db.session import SessionFactory
from app.services import devices, hwid
from app.services import provisioning as prov
from app.services import subscription_page as sp
from app.utils.crypto import SecretBox
from tests.conftest import make_settings
from tests.fakes import FakePanel
from tests.test_subs_support import NOW, FakeRegistry, add_rw_panel, add_sub, add_user, add_xui_panel, days

BOX = SecretBox("test-secret-key")
RAW = "HWID-ABCDEF-0001"
TOKEN = "token1"  # the token add_user gives user 1


@pytest.fixture(autouse=True)
def _clear() -> None:
    sp.clear_caches()


def enabled_provision(sub_id: int, panel_id: int, email: str, inbound_id: int | None = None) -> Provision:
    return Provision(
        subscription_id=sub_id,
        panel_id=panel_id,
        inbound_id=inbound_id,
        inbound_key=inbound_id or 0,
        remote_email=email,
        remote_id=None if inbound_id else "5",
        desired_state=ProvisionState.ENABLED,
        actual_state=ProvisionState.ENABLED,
    )


async def xui_world(session_factory: SessionFactory, *, device_limit: int = 2) -> tuple[FakeRegistry, int, int]:
    """A user with a subscription on a 3x-ui inbound that has a direct endpoint: (registry, sub id, panel id)."""
    async with session_factory.begin() as s:
        panel, (inbound,) = await add_xui_panel(s)
        await add_user(s, 1)
        sub = await add_sub(s, 1, expires_at=NOW + days(10), device_limit=device_limit)
        s.add(
            Endpoint(inbound_id=inbound.id, kind=EndpointKind.DIRECT, profile=EndpointProfile.STANDARD, address="a.b")
        )
        s.add(enabled_provision(sub.id, panel.id, "tg1-1", inbound.id))
    return FakeRegistry({panel.id: FakePanel()}), sub.id, panel.id


async def marzban_world(session_factory: SessionFactory, *, device_limit: int) -> tuple[FakeRegistry, int, int]:
    async with session_factory.begin() as s:
        panel = Panel(name="mz", kind=PanelKind.MARZBAN, url="https://mz.example.com")
        s.add(panel)
        await s.flush()
        await add_user(s, 1)
        sub = await add_sub(s, 1, expires_at=NOW + days(10), device_limit=device_limit)
        s.add(enabled_provision(sub.id, panel.id, "tg1"))
    return FakeRegistry({panel.id: FakePanel(kind=PanelKind.MARZBAN)}), sub.id, panel.id


def headers(hwid_value: str | None = RAW, **extra: str) -> dict[str, str]:
    result = {"user-agent": "Happ/2.4.1", "x-device-os": "Android", "x-ver-os": "14", "x-device-model": "Pixel 8"}
    if hwid_value is not None:
        result["x-hwid"] = hwid_value
    result.update(extra)
    return result


async def fetch(
    session_factory: SessionFactory,
    registry: FakeRegistry,
    client_headers: dict[str, str],
    *,
    settings: Settings | None = None,
    track: bool = True,
    now: datetime = NOW,
    **kwargs: str,
) -> sp.RenderedSubscription:
    rendered = await sp.render(
        session_factory,
        registry,
        settings or make_settings(),
        TOKEN,
        client_headers=client_headers,
        now=now,
        secret_box=BOX,
        track_devices=track,
        **kwargs,  # type: ignore[arg-type]
    )
    assert rendered is not None
    return rendered


def is_notice(rendered: sp.RenderedSubscription) -> bool:
    return len(rendered.links) == 1 and "0.0.0.0" in rendered.links[0]


async def device_rows(session_factory: SessionFactory) -> list[SubscriptionDevice]:
    async with session_factory() as s:
        return list((await s.scalars(select(SubscriptionDevice).order_by(SubscriptionDevice.id))).all())


async def test_devices_register_up_to_the_limit_then_placeholder(session_factory: SessionFactory) -> None:
    registry, _, _ = await xui_world(session_factory, device_limit=2)
    for n in (1, 2):
        rendered = await fetch(session_factory, registry, headers(f"{RAW}-{n}"))
        assert not is_notice(rendered)
        assert "x-hwid-max-devices-reached" not in rendered.headers
    third = await fetch(session_factory, registry, headers(f"{RAW}-3"))
    assert is_notice(third)
    assert third.headers["x-hwid-max-devices-reached"] == "true"
    assert "лимит устройств (2)" in unquote(urlsplit(third.links[0]).fragment)
    announce = base64.b64decode(third.headers["announce"].removeprefix("base64:")).decode()
    assert "(2)" in announce
    assert len(await device_rows(session_factory)) == 2
    async with session_factory.begin() as s:
        await s.execute(text("UPDATE users SET language='en'"))
    third = await fetch(session_factory, registry, headers(f"{RAW}-3"))
    assert "Device limit reached (2)" in unquote(urlsplit(third.links[0]).fragment)


async def test_known_device_passes_and_last_seen_is_throttled(session_factory: SessionFactory) -> None:
    registry, _, _ = await xui_world(session_factory, device_limit=1)
    await fetch(session_factory, registry, headers())
    assert is_notice(await fetch(session_factory, registry, headers("another")))
    (row,) = await device_rows(session_factory)
    assert row.last_seen_at == NOW
    soon = await fetch(session_factory, registry, headers(), now=NOW + timedelta(minutes=30))
    assert not is_notice(soon)
    assert (await device_rows(session_factory))[0].last_seen_at == NOW
    later = await fetch(session_factory, registry, headers(), now=NOW + timedelta(hours=2))
    assert not is_notice(later)
    row = (await device_rows(session_factory))[0]
    assert row.last_seen_at == NOW + timedelta(hours=2)
    assert row.first_seen_at == NOW


async def test_concurrent_new_devices_cannot_exceed_the_limit(session_factory: SessionFactory) -> None:
    registry, _, _ = await xui_world(session_factory, device_limit=2)
    results = await asyncio.gather(*(fetch(session_factory, registry, headers(f"dev-{n}")) for n in range(6)))
    assert sum(not is_notice(r) for r in results) == 2
    assert len(await device_rows(session_factory)) == 2


async def test_same_device_requested_concurrently_registers_once(session_factory: SessionFactory) -> None:
    registry, _, _ = await xui_world(session_factory, device_limit=3)
    results = await asyncio.gather(*(fetch(session_factory, registry, headers()) for _ in range(4)))
    assert all(not is_notice(r) for r in results)
    assert len(await device_rows(session_factory)) == 1


async def test_hwid_is_stored_only_as_hmac_and_without_any_address(session_factory: SessionFactory) -> None:
    registry, sub_id, _ = await xui_world(session_factory)
    await fetch(session_factory, registry, headers())
    (row,) = await device_rows(session_factory)
    assert row.hwid_hash == BOX.sign("hwid:" + RAW) and row.hwid_hash != RAW
    assert (row.platform, row.os_version, row.model, row.app) == ("Android", "14", "Pixel 8", "Happ")
    assert row.subscription_id == sub_id
    assert not {c.name for c in SubscriptionDevice.__table__.columns} & {"ip", "address", "user_agent"}
    async with session_factory() as s:
        dump = (await s.execute(text("SELECT * FROM subscription_devices"))).all()
    assert RAW not in repr(dump)


async def test_no_hwid_is_allowed_by_default_and_blocked_when_required(session_factory: SessionFactory) -> None:
    registry, _, _ = await xui_world(session_factory)
    allowed = await fetch(session_factory, registry, headers(None))
    assert not is_notice(allowed) and await device_rows(session_factory) == []
    strict = await fetch(session_factory, registry, headers(None), settings=make_settings(hwid_required=True))
    assert is_notice(strict)
    assert strict.headers["x-hwid-not-supported"] == "true"
    assert "Happ" in unquote(urlsplit(strict.links[0]).fragment)


async def test_unlimited_untracked_and_ip_mode_are_not_counted(session_factory: SessionFactory) -> None:
    registry, sub_id, _ = await xui_world(session_factory, device_limit=1)
    await fetch(session_factory, registry, headers("one"))
    assert not is_notice(await fetch(session_factory, registry, headers("two"), track=False))
    ip_mode = make_settings(xui_device_limit="ip", hwid_required=True)
    assert not is_notice(await fetch(session_factory, registry, headers("two"), settings=ip_mode))
    assert not is_notice(await fetch(session_factory, registry, headers(None), settings=ip_mode))
    async with session_factory.begin() as s:
        sub = await s.get(Subscription, sub_id)
        assert sub is not None
        sub.device_limit = 0
    assert not is_notice(await fetch(session_factory, registry, headers("three")))
    assert len(await device_rows(session_factory)) == 1


async def test_narrowed_urls_cannot_skip_the_limit(session_factory: SessionFactory) -> None:
    registry, _, _ = await xui_world(session_factory, device_limit=1)
    await fetch(session_factory, registry, headers("one"))
    rendered = await fetch(session_factory, registry, headers("two"), server_id="0" * 16)
    assert rendered.headers["x-hwid-max-devices-reached"] == "true"


async def test_remnawave_keeps_its_native_flow(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        rw = await add_rw_panel(s)
        await add_user(s, 1)
        sub = await add_sub(s, 1, expires_at=NOW + days(10), device_limit=1)
        s.add(enabled_provision(sub.id, rw.id, "tg_1"))
    registry = FakeRegistry({rw.id: FakePanel(kind=PanelKind.REMNAWAVE)})
    for n in range(3):
        rendered = await fetch(session_factory, registry, headers(f"dev-{n}"))
        assert "x-hwid-max-devices-reached" not in rendered.headers
    assert await device_rows(session_factory) == []


async def test_marzban_subscription_is_counted_by_the_bot(session_factory: SessionFactory) -> None:
    registry, _, _ = await marzban_world(session_factory, device_limit=1)
    assert not is_notice(await fetch(session_factory, registry, headers("one")))
    assert (await fetch(session_factory, registry, headers("two"))).headers["x-hwid-max-devices-reached"] == "true"


def test_panel_device_limit_by_mode() -> None:
    hwid_mode, ip_mode = make_settings(), make_settings(xui_device_limit="ip")
    assert hwid_mode.xui_device_limit == "hwid"
    assert prov.panel_device_limit(PanelKind.XUI, 3, hwid_mode) == 0
    assert prov.panel_device_limit(PanelKind.XUI, 3, ip_mode) == 3
    assert prov.panel_device_limit(PanelKind.REMNAWAVE, 3, hwid_mode) == 3
    assert prov.panel_device_limit(PanelKind.PASARGUARD, 3, hwid_mode) == 3
    assert prov.panel_device_limit(PanelKind.MARZBAN, 3, ip_mode) == 0


async def test_changing_the_mode_repushes_existing_clients(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        panel, (inbound,) = await add_xui_panel(s)
        await add_user(s, 1)
        await add_sub(s, 1, expires_at=NOW + days(10), device_limit=3)
    fake = FakePanel()
    registry = FakeRegistry({panel.id: fake})
    key = (inbound.remote_id, f"tg1-{inbound.id}")
    await prov.reconcile(session_factory, registry, make_settings(), now=NOW)
    assert fake.accounts[key].device_limit == 0
    await prov.reconcile(session_factory, registry, make_settings(xui_device_limit="ip"), now=NOW)
    assert fake.accounts[key].device_limit == 3
    await prov.reconcile(session_factory, registry, make_settings(), now=NOW)
    assert fake.accounts[key].device_limit == 0


async def test_devices_service_lists_and_removes_bot_tracked_devices(session_factory: SessionFactory) -> None:
    registry, sub_id, panel_id = await xui_world(session_factory, device_limit=3)
    await fetch(session_factory, registry, headers("one"))
    await fetch(session_factory, registry, headers("two", **{"x-device-model": "iPhone 15", "x-device-os": "iOS"}))
    (entry,) = await devices.list_devices(session_factory, registry, sub_id)
    assert (entry.panel_id, entry.supported, entry.can_delete_one) == (panel_id, True, True)
    assert [d.model for d in entry.devices] == ["Pixel 8", "iPhone 15"]
    assert entry.devices[0].platform == "Android" and entry.devices[0].user_agent == "Happ"
    assert entry.devices[0].last_seen_at == NOW and entry.devices[0].ip is None

    result = await devices.reset(
        session_factory, registry, sub_id, device_id=entry.devices[0].id, panel_id=panel_id, now=NOW
    )
    assert result.ok and result.reset == 1
    (entry,) = await devices.list_devices(session_factory, registry, sub_id)
    assert [d.model for d in entry.devices] == ["iPhone 15"]

    # The freed slot is usable again, and "reset all" forgets the rest.
    assert not is_notice(await fetch(session_factory, registry, headers("three")))
    assert (await devices.reset(session_factory, registry, sub_id, now=NOW + timedelta(hours=1))).ok
    assert await device_rows(session_factory) == []


async def test_bot_tracked_device_of_another_subscription_is_not_removed(session_factory: SessionFactory) -> None:
    registry, sub_id, panel_id = await xui_world(session_factory)
    async with session_factory.begin() as s:
        await add_user(s, 2)
        other = await add_sub(s, 2, expires_at=NOW + days(10))
        s.add(SubscriptionDevice(subscription_id=other.id, hwid_hash="c" * 64, first_seen_at=NOW, last_seen_at=NOW))
    (foreign,) = await device_rows(session_factory)
    await devices.reset(session_factory, registry, sub_id, device_id=str(foreign.id), panel_id=panel_id, now=NOW)
    assert len(await device_rows(session_factory)) == 1


async def test_marzban_only_subscription_devices_can_be_reset(session_factory: SessionFactory) -> None:
    registry, sub_id, _ = await marzban_world(session_factory, device_limit=2)
    async with session_factory.begin() as s:
        s.add(
            SubscriptionDevice(
                subscription_id=sub_id, hwid_hash="d" * 64, model="Pixel", first_seen_at=NOW, last_seen_at=NOW
            )
        )
    (entry,) = await devices.list_devices(session_factory, registry, sub_id)
    assert entry.supported and len(entry.devices) == 1
    assert (await devices.reset(session_factory, registry, sub_id, now=NOW)).ok
    assert await device_rows(session_factory) == []


async def test_stale_devices_are_purged(session_factory: SessionFactory) -> None:
    _, sub_id, _ = await xui_world(session_factory, device_limit=3)
    async with session_factory.begin() as s:
        s.add(
            SubscriptionDevice(
                subscription_id=sub_id, hwid_hash="a" * 64, first_seen_at=NOW, last_seen_at=NOW - days(40)
            )
        )
        s.add(
            SubscriptionDevice(
                subscription_id=sub_id, hwid_hash="b" * 64, first_seen_at=NOW, last_seen_at=NOW - days(5)
            )
        )
    assert await hwid.purge_stale(session_factory, 0, now=NOW) == 0
    assert await hwid.purge_stale(session_factory, 30, now=NOW) == 1
    assert [row.hwid_hash for row in await device_rows(session_factory)] == ["b" * 64]


def test_setting_defaults() -> None:
    defaults = make_settings()
    assert (defaults.hwid_required, defaults.hwid_device_ttl_days, defaults.xui_device_limit) == (False, 30, "hwid")
