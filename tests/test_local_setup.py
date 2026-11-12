from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.__main__ import build_local_setup, build_parser, cmd_bootstrap_local
from app.db.models import Base, Endpoint, Inbound, Location, Panel, PanelKind
from app.db.session import SessionFactory, create_engine
from app.services import local_setup
from app.services.local_setup import LocalSetup, SetupError, TelemtSetup, XuiSetup, bootstrap_local
from app.services.panels import inventory
from app.services.panels.base import InboundInfo, PanelError
from app.utils.crypto import SecretBox
from tests.conftest import make_settings
from tests.fakes import FakeCapablePanel, default_inbound

PASSWORD = "xui-secret-password-123"
TOKEN = "Bearer telemt-secret-token-456"


@dataclass
class LocalPanel(FakeCapablePanel):
    """A 3x-ui that also creates mixed inbounds."""

    inbounds: list[InboundInfo] = field(default_factory=list)

    async def create_mixed_inbound(self, port: int, remark: str = "") -> InboundInfo:
        self.calls.append(("create_mixed_inbound", port, remark))
        if any(i.port == port for i in self.inbounds):
            raise PanelError(f"port {port} is already used")
        info = InboundInfo(
            remote_id=max((i.remote_id for i in self.inbounds), default=0) + 1,
            remark=remark,
            protocol="mixed",
            port=port,
            enabled=True,
            params={"socks_auth": "password", "local": True},
        )
        self.inbounds.append(info)
        return info


@dataclass
class Registry:
    panels: dict[PanelKind, Any] = field(default_factory=dict)
    session_factory: SessionFactory | None = None

    async def get(self, panel_id: int) -> Any:
        assert self.session_factory is not None
        async with self.session_factory() as session:
            panel = await session.get(Panel, panel_id)
        assert panel is not None
        return self.panels[panel.kind]


def xui_setup(**overrides: Any) -> XuiSetup:
    values: dict[str, Any] = {
        "url": "http://host.docker.internal:2053/abc",
        "username": "admin1",
        "password": PASSWORD,
        "reality_port": 8445,
        "reality_sni": "vpn.example.com",
        "reality_dest": "127.0.0.1:8444",
        "endpoint_address": "203.0.113.5",
        "socks_port": 2080,
    }
    values.update(overrides)
    return XuiSetup(**values)


def telemt_setup() -> TelemtSetup:
    return TelemtSetup(url="http://telemt:9091", api_token=TOKEN, public_host="203.0.113.5")


async def _no_sleep(_: float) -> None:
    return None


def _env(session_factory: SessionFactory, xui: LocalPanel | None = None) -> tuple[Registry, SecretBox, LocalPanel]:
    panel = xui or LocalPanel()
    registry = Registry(
        panels={PanelKind.XUI: panel, PanelKind.TELEMT: FakeCapablePanel(kind=PanelKind.TELEMT)},
        session_factory=session_factory,
    )
    return registry, SecretBox("test-secret-key-0123456789abcdef"), panel


async def _count(session_factory: SessionFactory, model: Any) -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _counts(session_factory: SessionFactory) -> dict[str, int]:
    return {
        "locations": await _count(session_factory, Location),
        "panels": await _count(session_factory, Panel),
        "inbounds": await _count(session_factory, Inbound),
        "endpoints": await _count(session_factory, Endpoint),
    }


async def _panel_of(session_factory: SessionFactory, kind: PanelKind) -> Panel:
    async with session_factory() as session:
        panel = await session.scalar(select(Panel).where(Panel.kind == kind))
    assert panel is not None
    return panel


async def test_first_run_creates_everything(session_factory: SessionFactory) -> None:
    registry, box, panel = _env(session_factory)
    setup = LocalSetup(xui=xui_setup(), telemt=telemt_setup())
    result = await bootstrap_local(session_factory, registry, box, setup, sleep=_no_sleep)
    assert result.xui["reality"]["inbound"] == "created"
    assert result.xui["reality"]["endpoint"] == "created"
    assert result.xui["socks"]["inbound"] == "created"
    assert result.telemt["reachable"] is True
    assert await _counts(session_factory) == {"locations": 1, "panels": 2, "inbounds": 2, "endpoints": 1}
    assert ("create_reality_inbound", 8445, "vpn.example.com") in panel.calls
    xui = await _panel_of(session_factory, PanelKind.XUI)
    telemt = await _panel_of(session_factory, PanelKind.TELEMT)
    async with session_factory() as session:
        endpoint = await session.scalar(select(Endpoint))
        rows = {row.remote_id: row for row in await session.scalars(select(Inbound))}
    assert endpoint is not None
    assert xui.password and xui.password != PASSWORD and box.decrypt(xui.password) == PASSWORD
    assert telemt.api_token and box.decrypt(telemt.api_token) == TOKEN
    assert json.loads(xui.options or "{}") == {"tg_socks_host": "203.0.113.5", "tg_socks_port": 2080}
    assert json.loads(telemt.options or "{}")["public_port"] == 443
    assert (endpoint.address, endpoint.port) == ("203.0.113.5", 443)
    reality = rows[1]
    assert reality.is_active and reality.location_id is not None
    socks = rows[2]
    assert not socks.is_active and inventory.tg_socks_enabled(socks)


async def test_second_run_changes_nothing(session_factory: SessionFactory) -> None:
    registry, box, panel = _env(session_factory)
    setup = LocalSetup(xui=xui_setup(), telemt=telemt_setup())
    await bootstrap_local(session_factory, registry, box, setup, sleep=_no_sleep)
    before = await _counts(session_factory)
    password_before = (await _panel_of(session_factory, PanelKind.XUI)).password
    calls = len(panel.calls)
    again = await bootstrap_local(session_factory, registry, box, setup, sleep=_no_sleep)
    assert await _counts(session_factory) == before
    assert again.xui["panel"] == "unchanged" and again.telemt["panel"] == "unchanged"
    assert again.xui["reality"]["inbound"] == "existing" and again.xui["reality"]["endpoint"] == "unchanged"
    assert again.xui["socks"]["inbound"] == "existing"
    assert not any(call[0].startswith("create_") for call in panel.calls[calls:])
    assert (await _panel_of(session_factory, PanelKind.XUI)).password == password_before  # not re-encrypted


async def test_changed_credentials_are_updated(session_factory: SessionFactory) -> None:
    registry, box, _ = _env(session_factory)
    await bootstrap_local(session_factory, registry, box, LocalSetup(xui=xui_setup(socks_port=None)), sleep=_no_sleep)
    result = await bootstrap_local(
        session_factory,
        registry,
        box,
        LocalSetup(xui=xui_setup(socks_port=None, password="new-password-789")),
        sleep=_no_sleep,
    )
    assert result.xui["panel"] == "updated"
    xui = await _panel_of(session_factory, PanelKind.XUI)
    assert xui.password and box.decrypt(xui.password) == "new-password-789"
    assert (await _counts(session_factory))["panels"] == 1


async def test_existing_reality_inbound_is_reused(session_factory: SessionFactory) -> None:
    registry, box, panel = _env(session_factory, LocalPanel(inbounds=[default_inbound(7, 8445)]))
    result = await bootstrap_local(
        session_factory, registry, box, LocalSetup(xui=xui_setup(socks_port=None)), sleep=_no_sleep
    )
    assert result.xui["reality"]["inbound"] == "existing"
    assert result.xui["reality"]["remote_id"] == 7
    assert not any(call[0] == "create_reality_inbound" for call in panel.calls)
    assert result.warnings and "www.example.com" in result.warnings[0]  # the name differs from the expected one
    assert (await _counts(session_factory))["endpoints"] == 1


async def test_port_used_by_another_inbound_is_an_error(session_factory: SessionFactory) -> None:
    other = InboundInfo(remote_id=3, remark="x", protocol="trojan", port=8445, enabled=True, params={})
    registry, box, _ = _env(session_factory, LocalPanel(inbounds=[other]))
    with pytest.raises(SetupError, match="already used"):
        await bootstrap_local(session_factory, registry, box, LocalSetup(xui=xui_setup()), sleep=_no_sleep)


async def test_unreachable_panel_is_reported_without_secrets(session_factory: SessionFactory) -> None:
    registry, box, panel = _env(session_factory)
    panel.down = True
    with pytest.raises(SetupError) as raised:
        await bootstrap_local(session_factory, registry, box, LocalSetup(xui=xui_setup()), sleep=_no_sleep)
    assert PASSWORD not in str(raised.value)


async def test_unreachable_telemt_is_only_a_warning(session_factory: SessionFactory) -> None:
    registry, box, _ = _env(session_factory)
    registry.panels[PanelKind.TELEMT].down = True
    result = await bootstrap_local(session_factory, registry, box, LocalSetup(telemt=telemt_setup()), sleep=_no_sleep)
    assert result.telemt["reachable"] is False
    assert result.warnings and TOKEN not in result.warnings[0]
    assert (await _counts(session_factory))["panels"] == 1


def test_cli_arguments_build_the_setup(tmp_path: Path) -> None:
    password = tmp_path / "pw"
    password.write_text(PASSWORD + "\n", encoding="utf-8")
    token = tmp_path / "tk"
    token.write_text(TOKEN, encoding="utf-8")
    args = build_parser().parse_args(
        [
            "bootstrap-local",
            "--xui-url=http://x:2053/p",
            "--xui-user=admin",
            f"--xui-pass-file={password}",
            "--reality-port=8445",
            "--reality-sni=vpn.example.com",
            "--endpoint-address=203.0.113.5",
            "--socks-port=2080",
            "--telemt-url=http://telemt:9091",
            f"--telemt-token-file={token}",
            "--telemt-host=203.0.113.5",
        ]
    )
    setup = build_local_setup(args)
    assert setup.xui.password == PASSWORD and setup.xui.socks_port == 2080
    assert setup.telemt.api_token == TOKEN
    assert PASSWORD not in repr(args)  # only the file name is on the command line


def test_cli_requires_the_matching_options() -> None:
    args = build_parser().parse_args(["bootstrap-local", "--xui-url=http://x:2053/p"])
    with pytest.raises(SystemExit, match="--xui-user"):
        build_local_setup(args)
    with pytest.raises(SystemExit, match="nothing to do"):
        build_local_setup(build_parser().parse_args(["bootstrap-local"]))


async def test_command_prints_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'cmd.db'}"
    engine = create_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    panel = LocalPanel()

    class FakeRegistry:
        def __init__(self, session_factory: SessionFactory, *_: Any) -> None:
            self.inner = Registry(
                panels={PanelKind.XUI: panel, PanelKind.TELEMT: FakeCapablePanel(kind=PanelKind.TELEMT)},
                session_factory=session_factory,
            )

        async def get(self, panel_id: int) -> Any:
            return await self.inner.get(panel_id)

        async def close(self) -> None:
            return None

    monkeypatch.setattr("app.services.panels.registry.PanelRegistry", FakeRegistry)
    monkeypatch.setattr(local_setup, "PING_DELAY_SEC", 0.0)
    code = await cmd_bootstrap_local(
        make_settings(database_url=url), LocalSetup(xui=xui_setup(), telemt=telemt_setup())
    )
    out = capsys.readouterr()
    assert code == 0
    assert PASSWORD not in out.out + out.err and TOKEN not in out.out + out.err
    assert json.loads(out.out)["xui"]["reality"]["port"] == 8445


def test_mixed_inbound_body_is_password_protected() -> None:
    from app.services.panels.xui import build_mixed_inbound

    body = build_mixed_inbound(2080, remark="Telegram SOCKS5")
    assert body["protocol"] == "mixed" and body["port"] == 2080 and body["enable"] is True
    settings = json.loads(body["settings"])
    assert settings["auth"] == "password" and settings["accounts"] == []
    with pytest.raises(ValueError):
        build_mixed_inbound(0)


@dataclass
class NoSsPanel(LocalPanel):
    """A backend of an older version: it cannot create Shadowsocks inbounds."""

    create_shadowsocks_inbound = None  # type: ignore[assignment]


async def test_shadowsocks_is_skipped_when_the_backend_cannot_create_it(session_factory: SessionFactory) -> None:
    registry, box, panel = _env(session_factory, NoSsPanel())
    result = await bootstrap_local(
        session_factory, registry, box, LocalSetup(xui=xui_setup(socks_port=None, ss_port=8388)), sleep=_no_sleep
    )
    assert result.xui["shadowsocks"] == {"skipped": "not supported by this version"}
    assert any("Shadowsocks skipped" in warning for warning in result.warnings)
    assert result.xui["reality"]["inbound"] == "created"
    assert not any(call[0] == "create_shadowsocks_inbound" for call in panel.calls)


async def test_shadowsocks_is_created_once_and_activated(session_factory: SessionFactory) -> None:
    registry, box, panel = _env(session_factory)
    setup = LocalSetup(xui=xui_setup(socks_port=None, ss_port=8388))
    first = await bootstrap_local(session_factory, registry, box, setup, sleep=_no_sleep)
    assert first.xui["shadowsocks"]["inbound"] == "created"
    assert first.xui["shadowsocks"]["endpoint"] == "created" and first.xui["shadowsocks"]["active"] is True
    assert [call[:2] for call in panel.calls].count(("create_shadowsocks_inbound", 8388)) == 1
    counts = await _counts(session_factory)
    again = await bootstrap_local(session_factory, registry, box, setup, sleep=_no_sleep)
    assert again.xui["shadowsocks"]["inbound"] == "existing" and again.xui["shadowsocks"]["endpoint"] == "unchanged"
    assert [call[:2] for call in panel.calls].count(("create_shadowsocks_inbound", 8388)) == 1
    assert await _counts(session_factory) == counts
