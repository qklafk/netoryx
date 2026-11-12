"""Panel inventory: env bootstrap, inbound refresh, panel/endpoint health."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import func, select

from app.db.models import Endpoint, EndpointKind, EndpointProfile, Inbound, Location, Panel, PanelKind
from app.db.session import SessionFactory
from app.services.panels import inventory
from app.services.panels.base import InboundInfo, PanelBackend
from app.services.panels.inventory import (
    HealthChange,
    bootstrap_from_env,
    check_health,
    inbound_port,
    refresh_inbounds,
    tcp_probe,
)
from app.utils.crypto import SecretBox
from tests.conftest import make_settings
from tests.fakes import FakePanel, default_inbound

KEY = "test-secret-key-0123456789abcdef"


class StubRegistry:
    """PanelRegistry stand-in serving FakePanels by panel id."""

    def __init__(self, panels: dict[int, FakePanel] | None = None, default: FakePanel | None = None) -> None:
        self.panels = panels or {}
        self.default = default

    async def get(self, panel_id: int) -> PanelBackend:
        panel = self.panels.get(panel_id) or self.default
        if panel is None:
            raise LookupError(f"panel {panel_id} not found")
        return panel


def ws_tls_inbound(remote_id: int, *, enabled: bool = True) -> InboundInfo:
    return InboundInfo(
        remote_id=remote_id,
        remark=f"ws-{remote_id}",
        protocol="vless",
        port=8443,
        enabled=enabled,
        params={"type": "ws", "security": "tls", "path": "/ws", "host": "cdn.ru", "flow_allowed": False},
    )


def other_inbound(remote_id: int, protocol: str = "vless", network: str = "tcp") -> InboundInfo:
    return InboundInfo(remote_id, "x", protocol, 1000 + remote_id, True, {"type": network, "security": "none"})


async def add_panel(session_factory: SessionFactory, **overrides: Any) -> int:
    values: dict[str, Any] = {"name": "DE", "kind": PanelKind.XUI, "url": "https://panel.example.com:2053/p"}
    values.update(overrides)
    async with session_factory.begin() as session:
        panel = Panel(**values)
        session.add(panel)
        await session.flush()
        return panel.id


async def inbounds(session_factory: SessionFactory) -> list[Inbound]:
    async with session_factory() as session:
        return list(await session.scalars(select(Inbound).order_by(Inbound.remote_id)))


async def endpoints(session_factory: SessionFactory) -> list[Endpoint]:
    async with session_factory() as session:
        return list(await session.scalars(select(Endpoint).order_by(Endpoint.id)))


# ---------------------------------------------------------------------------------------------
# refresh_inbounds
# ---------------------------------------------------------------------------------------------
async def test_refresh_creates_inactive_rows_for_supported_vless_only(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    fake = FakePanel(
        inbounds=[
            default_inbound(1, 443),
            other_inbound(2, protocol="trojan"),
            other_inbound(3, network="kcp"),
            ws_tls_inbound(4, enabled=False),
        ]
    )
    result = await refresh_inbounds(session_factory, StubRegistry({panel_id: fake}), panel_id)
    rows = await inbounds(session_factory)
    assert [r.remote_id for r in rows] == [1, 4]
    assert result.created == [r.id for r in rows]
    assert result.skipped_remote_ids == [2, 3]
    assert result.disabled_on_panel == [rows[1].id]
    reality, ws = rows
    assert not reality.is_active and not ws.is_active
    assert reality.name == "in-443"
    assert reality.flow == "xtls-rprx-vision"
    assert ws.flow == ""
    params = json.loads(reality.params or "")
    assert params == {**default_inbound(1, 443).params, "port": 443, "protocol": "vless"}
    assert inbound_port(reality) == 443
    assert reality.params_updated_at is not None
    assert await endpoints(session_factory) == []


async def test_refresh_bootstrap_options(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    async with session_factory.begin() as session:
        location = Location(name="Германия", flag="🇩🇪")
        session.add(location)
        await session.flush()
        location_id = location.id
    fake = FakePanel(inbounds=[default_inbound(1, 443), default_inbound(2, 444)])
    await refresh_inbounds(
        session_factory,
        StubRegistry({panel_id: fake}),
        panel_id,
        activate_new=True,
        only_remote_ids=[2],
        location_id=location_id,
        endpoint_address="203.0.113.10",
    )
    (row,) = await inbounds(session_factory)
    assert (row.remote_id, row.is_active, row.location_id) == (2, True, location_id)
    (endpoint,) = await endpoints(session_factory)
    assert (endpoint.inbound_id, endpoint.address, endpoint.port) == (row.id, "203.0.113.10", None)
    assert (endpoint.kind, endpoint.profile, endpoint.location_id) == (
        EndpointKind.DIRECT,
        EndpointProfile.STANDARD,
        location_id,
    )


async def test_refresh_updates_params_keeps_admin_choices(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    fake = FakePanel(inbounds=[default_inbound(1, 443)])
    registry = StubRegistry({panel_id: fake})
    await refresh_inbounds(session_factory, registry, panel_id, activate_new=True)
    async with session_factory.begin() as session:
        row = await session.scalar(select(Inbound))
        assert row is not None
        row.name, row.sort_order = "Admin name", 9
    unchanged = await refresh_inbounds(session_factory, registry, panel_id)
    assert unchanged.updated == [] and unchanged.created == []

    changed_params = {**default_inbound(1, 443).params, "sni": "new.example.com"}
    fake.inbounds = [replace(default_inbound(1, 443), params=changed_params, remark="renamed on panel")]
    result = await refresh_inbounds(session_factory, registry, panel_id)
    (row,) = await inbounds(session_factory)
    assert result.updated == [row.id]
    assert json.loads(row.params or "")["sni"] == "new.example.com"
    assert (row.name, row.sort_order, row.is_active, row.flow) == ("Admin name", 9, True, "xtls-rprx-vision")


async def test_refresh_clears_flow_when_no_longer_allowed(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    fake = FakePanel(inbounds=[default_inbound(1, 443)])
    registry = StubRegistry({panel_id: fake})
    await refresh_inbounds(session_factory, registry, panel_id)
    fake.inbounds = [replace(ws_tls_inbound(1), port=443)]
    await refresh_inbounds(session_factory, registry, panel_id)
    (row,) = await inbounds(session_factory)
    assert row.flow == ""


async def test_refresh_deactivates_missing_but_not_on_empty_answer(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    fake = FakePanel(inbounds=[default_inbound(1, 443), default_inbound(2, 444)])
    registry = StubRegistry({panel_id: fake})
    await refresh_inbounds(session_factory, registry, panel_id, activate_new=True)

    fake.inbounds = []
    result = await refresh_inbounds(session_factory, registry, panel_id)
    assert result.deactivated == []
    assert all(r.is_active for r in await inbounds(session_factory))

    fake.inbounds = [default_inbound(1, 443)]
    result = await refresh_inbounds(session_factory, registry, panel_id)
    first, second = await inbounds(session_factory)
    assert result.deactivated == [second.id]
    assert first.is_active and not second.is_active


async def test_refresh_deactivates_inbound_switched_to_unsupported_transport(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    fake = FakePanel(inbounds=[default_inbound(1, 443), default_inbound(2, 444)])
    registry = StubRegistry({panel_id: fake})
    await refresh_inbounds(session_factory, registry, panel_id, activate_new=True)

    fake.inbounds = [default_inbound(1, 443), replace(other_inbound(2, network="kcp"), port=444)]
    result = await refresh_inbounds(session_factory, registry, panel_id)
    first, second = await inbounds(session_factory)
    assert result.skipped_remote_ids == [2]
    assert result.deactivated == [second.id]
    assert first.is_active and not second.is_active

    again = await refresh_inbounds(session_factory, registry, panel_id)
    assert again.deactivated == []


async def test_refresh_only_unsupported_inbounds_left_deactivates_them(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    fake = FakePanel(inbounds=[default_inbound(1, 443)])
    registry = StubRegistry({panel_id: fake})
    await refresh_inbounds(session_factory, registry, panel_id, activate_new=True)

    fake.inbounds = [replace(other_inbound(1, protocol="trojan"), port=443)]
    result = await refresh_inbounds(session_factory, registry, panel_id)
    (row,) = await inbounds(session_factory)
    assert result.deactivated == [row.id]
    assert not row.is_active


async def test_refresh_ignores_other_panels_rows(session_factory: SessionFactory) -> None:
    panel_a = await add_panel(session_factory)
    panel_b = await add_panel(session_factory, name="NL")
    registry = StubRegistry({panel_a: FakePanel(), panel_b: FakePanel(inbounds=[default_inbound(1, 443)])})
    await refresh_inbounds(session_factory, registry, panel_a, activate_new=True)
    await refresh_inbounds(session_factory, registry, panel_b, activate_new=True)
    rows = await inbounds(session_factory)
    assert sorted(r.panel_id for r in rows) == [panel_a, panel_b]
    assert all(r.is_active for r in rows)


async def test_refresh_propagates_panel_errors_without_changes(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    with pytest.raises(inventory.PanelError):
        await refresh_inbounds(session_factory, StubRegistry({panel_id: FakePanel(down=True)}), panel_id)
    assert await inbounds(session_factory) == []


# ---------------------------------------------------------------------------------------------
# bootstrap_from_env
# ---------------------------------------------------------------------------------------------
XUI_ENV = {
    "xui_host": "https://panel.example.com:2053/secret/",
    "xui_username": "admin",
    "xui_password": "pw",
    "xui_location_name": "Германия",
    "xui_location_flag": "🇩🇪",
}


async def test_bootstrap_noop_without_config(session_factory: SessionFactory) -> None:
    result = await bootstrap_from_env(session_factory, make_settings(), SecretBox(KEY), StubRegistry())
    assert result is None
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Location)) == 0


async def test_bootstrap_noop_when_panels_exist(session_factory: SessionFactory) -> None:
    await add_panel(session_factory)
    registry = StubRegistry(default=FakePanel())
    result = await bootstrap_from_env(session_factory, make_settings(**XUI_ENV), SecretBox(KEY), registry)
    assert result is None


async def test_bootstrap_xui_imports_inbounds_with_endpoints(session_factory: SessionFactory) -> None:
    box = SecretBox(KEY)
    fake = FakePanel(inbounds=[default_inbound(1, 443), ws_tls_inbound(2), other_inbound(3, protocol="trojan")])
    settings = make_settings(**XUI_ENV, xui_api_token="tok")
    result = await bootstrap_from_env(session_factory, settings, box, StubRegistry(default=fake))
    assert result is not None
    assert (result.inbounds_imported, result.errors) == (2, [])
    async with session_factory() as session:
        (panel,) = (await session.scalars(select(Panel))).all()
        (location,) = (await session.scalars(select(Location))).all()
    assert result.panel_ids == [panel.id]
    assert (panel.kind, panel.url, panel.username, panel.tls_verify) == (
        PanelKind.XUI,
        "https://panel.example.com:2053/secret",
        "admin",
        True,
    )
    assert panel.password is not None and panel.password.startswith("enc:")
    assert box.decrypt(panel.password) == "pw"
    assert panel.api_token is not None and box.decrypt(panel.api_token) == "tok"
    assert (location.name, location.flag) == ("Германия", "🇩🇪")
    rows = await inbounds(session_factory)
    assert all(r.is_active and r.location_id == location.id for r in rows)
    assert [e.address for e in await endpoints(session_factory)] == ["panel.example.com", "panel.example.com"]


async def test_bootstrap_respects_inbound_ids_and_public_host(session_factory: SessionFactory) -> None:
    fake = FakePanel(inbounds=[default_inbound(1, 443), default_inbound(2, 444)])
    settings = make_settings(**XUI_ENV, xui_inbound_ids="2", xui_public_host="203.0.113.7")
    result = await bootstrap_from_env(session_factory, settings, SecretBox(KEY), StubRegistry(default=fake))
    assert result is not None and result.inbounds_imported == 1
    (row,) = await inbounds(session_factory)
    assert row.remote_id == 2
    assert [e.address for e in await endpoints(session_factory)] == ["203.0.113.7"]


async def test_bootstrap_keeps_panel_when_import_fails(session_factory: SessionFactory) -> None:
    registry = StubRegistry(default=FakePanel(down=True))
    result = await bootstrap_from_env(session_factory, make_settings(**XUI_ENV), SecretBox(KEY), registry)
    assert result is not None
    assert result.inbounds_imported == 0
    assert len(result.errors) == 1 and "panel down" in result.errors[0]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Panel)) == 1


async def test_bootstrap_reports_no_vless_inbounds(session_factory: SessionFactory) -> None:
    registry = StubRegistry(default=FakePanel(inbounds=[other_inbound(1, protocol="trojan")]))
    result = await bootstrap_from_env(session_factory, make_settings(**XUI_ENV), SecretBox(KEY), registry)
    assert result is not None and "no VLESS inbounds" in result.errors[0]


async def test_bootstrap_remnawave(session_factory: SessionFactory) -> None:
    box = SecretBox(KEY)
    settings = make_settings(
        remnawave_url="http://remnawave:3000",
        remnawave_api_token="rw-token",
        remnawave_squad_uuids="s1,s2",
        remnawave_direct=False,
    )
    result = await bootstrap_from_env(session_factory, settings, box, StubRegistry())
    assert result is not None and result.errors == []
    async with session_factory() as session:
        (panel,) = (await session.scalars(select(Panel))).all()
        (location,) = (await session.scalars(select(Location))).all()
    assert panel.kind == PanelKind.REMNAWAVE
    assert box.decrypt(panel.api_token or "") == "rw-token"
    assert json.loads(panel.options or "") == {"squad_uuids": ["s1", "s2"], "direct": False, "location_id": location.id}
    assert await inbounds(session_factory) == []


async def test_bootstrap_both_panels(session_factory: SessionFactory) -> None:
    settings = make_settings(**XUI_ENV, remnawave_url="http://rw:3000", remnawave_api_token="t")
    result = await bootstrap_from_env(session_factory, settings, SecretBox(KEY), StubRegistry(default=FakePanel()))
    assert result is not None and len(result.panel_ids) == 2


# ---------------------------------------------------------------------------------------------
# check_health
# ---------------------------------------------------------------------------------------------
@pytest.fixture
async def tcp_server() -> AsyncIterator[int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


async def add_endpoint(session_factory: SessionFactory, panel_id: int, **overrides: Any) -> int:
    async with session_factory.begin() as session:
        remote_id = 1 + (await session.scalar(select(func.count()).select_from(Inbound)) or 0)
        inbound = Inbound(panel_id=panel_id, remote_id=remote_id, name="in", params='{"port": 443}', is_active=True)
        session.add(inbound)
        await session.flush()
        values: dict[str, Any] = {
            "inbound_id": inbound.id,
            "kind": EndpointKind.DIRECT,
            "profile": EndpointProfile.STANDARD,
            "address": "127.0.0.1",
        }
        values.update(overrides)
        endpoint = Endpoint(**values)
        session.add(endpoint)
        await session.flush()
        return endpoint.id


async def load(session_factory: SessionFactory, model: Any, row_id: int) -> Any:
    async with session_factory() as session:
        return await session.get(model, row_id)


async def test_panel_health_transitions(session_factory: SessionFactory) -> None:
    settings = make_settings(health_failures_before_alert=2)
    panel_id = await add_panel(session_factory)
    fake = FakePanel(version="3.11.0")
    registry = StubRegistry({panel_id: fake})

    assert await check_health(session_factory, registry, settings) == []
    panel = await load(session_factory, Panel, panel_id)
    assert (panel.version, panel.consecutive_failures, panel.last_error) == ("3.11.0", 0, None)
    assert panel.last_ok_at is not None

    fake.down = True
    assert await check_health(session_factory, registry, settings) == []
    changes = await check_health(session_factory, registry, settings)
    assert changes == [HealthChange("panel", panel_id, "DE", ok=False, error="panel down (ping)")]
    assert await check_health(session_factory, registry, settings) == []  # alert once
    panel = await load(session_factory, Panel, panel_id)
    assert (panel.consecutive_failures, panel.last_error) == (3, "panel down (ping)")

    fake.down = False
    assert await check_health(session_factory, registry, settings) == [HealthChange("panel", panel_id, "DE", ok=True)]
    panel = await load(session_factory, Panel, panel_id)
    assert (panel.consecutive_failures, panel.last_error) == (0, None)


async def test_panel_health_handles_missing_backend_and_skips_inactive(session_factory: SessionFactory) -> None:
    settings = make_settings(health_failures_before_alert=1)
    broken = await add_panel(session_factory)
    inactive = await add_panel(session_factory, is_active=False)
    changes = await check_health(session_factory, StubRegistry(), settings)
    assert [(c.id, c.ok) for c in changes] == [(broken, False)]
    assert "not found" in (changes[0].error or "")
    assert (await load(session_factory, Panel, inactive)).consecutive_failures == 0


async def test_panel_ping_timeout(session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(health_failures_before_alert=1, panel_request_timeout_sec=1)
    panel_id = await add_panel(session_factory)
    fake = FakePanel()

    async def slow_ping() -> str:
        await asyncio.sleep(10)
        return "never"

    monkeypatch.setattr(fake, "ping", slow_ping)
    original = inventory._ping

    async def fast_ping(registry: Any, pid: int, timeout_sec: float) -> tuple[str | None, str | None]:
        return await original(registry, pid, 0.05)

    monkeypatch.setattr(inventory, "_ping", fast_ping)
    (change,) = await check_health(session_factory, StubRegistry({panel_id: fake}), settings)
    assert change.ok is False and "no answer" in (change.error or "")


async def test_endpoint_probe_hides_and_recovers(
    session_factory: SessionFactory, tcp_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(health_failures_before_alert=2)
    panel_id = await add_panel(session_factory)
    endpoint_id = await add_endpoint(session_factory, panel_id, port=tcp_server, remark="🇩🇪 Германия")
    await add_endpoint(session_factory, panel_id, port=tcp_server, address="localhost")  # stays reachable
    registry = StubRegistry({panel_id: FakePanel()})

    assert await check_health(session_factory, registry, settings) == []
    endpoint = await load(session_factory, Endpoint, endpoint_id)
    assert endpoint.last_ok_at is not None and endpoint.consecutive_failures == 0

    async def refused(host: str, port: int, timeout_sec: float = 3) -> str | None:
        return f"{host}:{port}: refused" if host == "127.0.0.1" else None

    monkeypatch.setattr(inventory, "tcp_probe", refused)
    assert await check_health(session_factory, registry, settings) == []
    (change,) = await check_health(session_factory, registry, settings)
    assert change == HealthChange(
        "endpoint", endpoint_id, "🇩🇪 Германия", ok=False, error=f"127.0.0.1:{tcp_server}: refused", panel_id=panel_id
    )
    assert await check_health(session_factory, registry, settings) == []
    assert (await load(session_factory, Endpoint, endpoint_id)).auto_hidden is True

    monkeypatch.undo()
    (change,) = await check_health(session_factory, registry, settings)
    assert (change.kind, change.ok) == ("endpoint", True)
    endpoint = await load(session_factory, Endpoint, endpoint_id)
    assert (endpoint.auto_hidden, endpoint.consecutive_failures, endpoint.last_error) == (False, 0, None)


async def test_all_probes_failing_hides_nothing(
    session_factory: SessionFactory, tcp_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a probe-side outage (bot host offline, DNS or egress filtered) must not empty every
    subscription. When no probe of a round succeeds, endpoints stay visible and one alert is raised."""
    settings = make_settings(health_failures_before_alert=2)
    panel_id = await add_panel(session_factory)
    ids = [await add_endpoint(session_factory, panel_id, port=tcp_server) for _ in range(3)]
    registry = StubRegistry({panel_id: FakePanel()})

    async def unreachable(host: str, port: int, timeout_sec: float = 3) -> str:
        return f"{host}:{port}: Network is unreachable"

    monkeypatch.setattr(inventory, "tcp_probe", unreachable)
    assert await check_health(session_factory, registry, settings) == []
    (change,) = await check_health(session_factory, registry, settings)
    assert (change.kind, change.ok) == ("probes", False)
    assert "3" in change.name and "Network is unreachable" in (change.error or "")
    for _ in range(3):
        assert await check_health(session_factory, registry, settings) == []  # alert once
    for endpoint_id in ids:
        endpoint = await load(session_factory, Endpoint, endpoint_id)
        assert endpoint.auto_hidden is False
        assert endpoint.consecutive_failures == 5 and "unreachable" in (endpoint.last_error or "")

    monkeypatch.undo()
    assert await check_health(session_factory, registry, settings) == [
        HealthChange("probes", 0, "3 endpoints", ok=True)
    ]
    assert await check_health(session_factory, registry, settings) == []
    for endpoint_id in ids:
        assert (await load(session_factory, Endpoint, endpoint_id)).consecutive_failures == 0


async def test_endpoint_failing_after_probe_outage_is_hidden(
    session_factory: SessionFactory, tcp_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once probes work again, an endpoint that is really down is hidden at once (its count is past the
    threshold), and the probe recovery is reported."""
    settings = make_settings(health_failures_before_alert=2)
    panel_id = await add_panel(session_factory)
    down = await add_endpoint(session_factory, panel_id, port=tcp_server)
    await add_endpoint(session_factory, panel_id, port=tcp_server, address="localhost")
    registry = StubRegistry({panel_id: FakePanel()})
    outage = True

    async def probe(host: str, port: int, timeout_sec: float = 3) -> str | None:
        return f"{host}:{port}: refused" if outage or host == "127.0.0.1" else None

    monkeypatch.setattr(inventory, "tcp_probe", probe)
    await check_health(session_factory, registry, settings)
    await check_health(session_factory, registry, settings)
    outage = False
    changes = await check_health(session_factory, registry, settings)
    assert sorted((c.kind, c.id, c.ok) for c in changes) == [("endpoint", down, False), ("probes", 0, True)]
    assert (await load(session_factory, Endpoint, down)).auto_hidden is True


async def test_endpoint_uses_inbound_port_and_skips_inactive(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(health_failures_before_alert=5)
    panel_id = await add_panel(session_factory)
    active = await add_endpoint(session_factory, panel_id, address="198.51.100.1")
    await add_endpoint(session_factory, panel_id, address="198.51.100.2", is_active=False)
    probed: list[tuple[str, int]] = []

    async def record(host: str, port: int, timeout_sec: float = 3) -> None:
        probed.append((host, port))

    monkeypatch.setattr(inventory, "tcp_probe", record)
    await check_health(session_factory, StubRegistry({panel_id: FakePanel()}), settings)
    assert probed == [("198.51.100.1", 443)]
    assert (await load(session_factory, Endpoint, active)).last_ok_at is not None


async def test_endpoint_with_unknown_port_fails_without_probe(session_factory: SessionFactory, tcp_server: int) -> None:
    settings = make_settings(health_failures_before_alert=1)
    panel_id = await add_panel(session_factory)
    endpoint_id = await add_endpoint(session_factory, panel_id)
    async with session_factory.begin() as session:
        (await session.scalar(select(Inbound))).params = None  # type: ignore[union-attr]
    await add_endpoint(session_factory, panel_id, port=tcp_server)  # probes work
    (change,) = await check_health(session_factory, StubRegistry({panel_id: FakePanel()}), settings)
    assert change.id == endpoint_id and "unknown port" in (change.error or "")


async def test_tcp_probe_real_sockets(tcp_server: int) -> None:
    assert await tcp_probe("127.0.0.1", tcp_server) is None
    error = await tcp_probe("127.0.0.1", 1, timeout_sec=0.2)
    assert error is not None and error.startswith("127.0.0.1:1: ")


@pytest.mark.parametrize(("raw", "expected"), [('{"port": 8443}', 8443), ('{"port": "x"}', None), ("bad", None)])
def test_inbound_port(raw: str, expected: int | None) -> None:
    assert inbound_port(Inbound(params=raw)) == expected


async def test_one_broken_panel_does_not_stop_health_checks(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected exception from one backend (e.g. an undecryptable token) is recorded, not raised."""
    settings = make_settings(health_failures_before_alert=1)
    broken = await add_panel(session_factory, name="RW")
    down = await add_panel(session_factory)
    endpoint_id = await add_endpoint(session_factory, down, address="198.51.100.1")
    fake = FakePanel()
    fake.down = True

    class BrokenRegistry(StubRegistry):
        async def get(self, panel_id: int) -> PanelBackend:
            if panel_id == broken:
                raise ValueError("cannot decrypt value: wrong SECRET_KEY or corrupted data")
            return await super().get(panel_id)

    await add_endpoint(session_factory, down, address="198.51.100.2")  # reachable: probes work

    async def refused(host: str, port: int, timeout_sec: float = 3) -> str | None:
        return f"{host}:{port}: refused" if host == "198.51.100.1" else None

    monkeypatch.setattr(inventory, "tcp_probe", refused)
    changes = await check_health(session_factory, BrokenRegistry({down: fake}), settings)
    assert sorted((c.kind, c.id, c.ok) for c in changes) == [
        ("endpoint", endpoint_id, False),
        ("panel", broken, False),
        ("panel", down, False),
    ]
    assert "SECRET_KEY" in ((await load(session_factory, Panel, broken)).last_error or "")
