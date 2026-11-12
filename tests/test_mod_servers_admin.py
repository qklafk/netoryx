"""Servers in the bot: add a panel with a connection test, card actions, delete with cleanup, inbounds, VLESS-Reality
creation, locations, endpoints with presets and link preview, health."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from aiogram.methods import DeleteMessage
from sqlalchemy import func, select

from app.db.models import (
    AdminAuditLog,
    AdminRole,
    Endpoint,
    EndpointKind,
    EndpointProfile,
    Inbound,
    Location,
    Panel,
    PanelKind,
    Provision,
    ProvisionState,
    Subscription,
    SubscriptionStatus,
)
from app.db.session import SessionFactory
from app.modules.admin_servers import service
from app.modules.admin_servers.callbacks import SrvCb
from app.services.panels.base import InboundInfo, PanelError
from app.utils.time import utcnow
from tests.fakes import FakePanel, default_inbound
from tests.test_mod_admin_tools_support import (
    Harness,
    add_user,
    make_harness,
    module,
    release_module_routers,  # noqa: F401 - autouse fixture
)


@dataclass
class RealityPanel(FakePanel):
    created: list[tuple[int, str]] | None = None

    async def create_reality_inbound(
        self, port: int, sni: str, dest: str | None = None, remark: str = "", flow: str = "xtls-rprx-vision"
    ) -> InboundInfo:
        if any(i.port == port for i in self.inbounds):
            raise PanelError("port is taken")
        info = default_inbound(remote_id=len(self.inbounds) + 10, port=port)
        info.remark = remark
        info.params["sni"] = sni
        self.inbounds.append(info)
        self.created = [*(self.created or []), (port, sni)]
        return info


def cb(action: str, id_: int = 0, value: str = "") -> str:
    return SrvCb(a=action, id=id_, v=value).pack()


async def _harness(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch, panel: FakePanel | None = None
) -> Harness:
    h = await make_harness(session_factory, [module("admin_servers")])
    fake = panel or RealityPanel()
    h.ctx.panels.default = fake
    monkeypatch.setattr(service, "build_backend", lambda *args, **kwargs: fake)
    return h


async def _panel(session_factory: SessionFactory, **values: Any) -> int:
    async with session_factory.begin() as session:
        panel = Panel(
            name=values.pop("name", "DE-1"),
            kind=values.pop("kind", PanelKind.XUI),
            url="https://1.2.3.4:2053/x",
            **values,
        )
        session.add(panel)
        await session.flush()
        return panel.id


async def test_add_xui_panel_with_connection_test(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await _harness(session_factory, monkeypatch)
    await h.click(cb("home"))
    assert "Пока ни одного сервера" in h.last()
    await h.click(h.button("Добавить сервер"))
    await h.click(h.button("3x-ui"))
    await h.say("Германия-1")
    await h.say("not a url")
    assert "не похоже на адрес" in h.last()
    await h.say("https://1.2.3.4:2053/secret/")
    await h.say("admin s3cret")
    assert h.api.of(DeleteMessage), "the message with credentials is deleted"
    assert "Подключение работает" in h.last() and "3.11.0" in h.last()
    await h.click(h.button("TLS"))  # toggles verification and tests again
    await h.click(h.button("Сохранить"))
    async with session_factory() as session:
        panel = await session.scalar(select(Panel))
        inbounds = (await session.scalars(select(Inbound))).all()
        audit = await session.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "panel.create"))
    assert panel is not None and panel.url == "https://1.2.3.4:2053/secret"
    assert panel.username == "admin" and panel.password and panel.password.startswith("enc:")
    assert h.ctx.secret_box.decrypt(panel.password) == "s3cret"
    assert panel.tls_verify is False and panel.version == "3.11.0"
    assert len(inbounds) == 1 and inbounds[0].is_active is False
    assert audit is not None
    assert "Найдено inbound'ов: 1" in h.last()


async def test_add_remnawave_failed_test_can_be_saved_anyway(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await _harness(session_factory, monkeypatch, FakePanel(kind=PanelKind.REMNAWAVE, down=True))
    await h.click(cb("kind", value="remnawave"))
    await h.say("RW")
    await h.say("https://panel.example.com")
    await h.say("token with spaces")
    assert "Нужен API-токен" in h.last()
    await h.say("eyJtoken")
    assert "Не получилось подключиться" in h.last()
    await h.click(h.button("Всё равно сохранить"))
    async with session_factory() as session:
        panel = await session.scalar(select(Panel))
    assert panel is not None and panel.kind == PanelKind.REMNAWAVE
    assert panel.api_token and h.ctx.secret_box.decrypt(panel.api_token) == "eyJtoken"
    assert json.loads(panel.options or "{}") == {"squad_uuids": [], "direct": False}


async def test_card_ping_sync_toggle_and_rename(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await _harness(session_factory, monkeypatch)
    panel_id = await _panel(session_factory)
    await h.click(cb("card", panel_id))
    assert "DE-1" in h.last()
    await h.click(h.button("Проверить связь"))
    assert "Связь есть" in h.last()
    await h.click(h.button("Синхронизировать"))
    assert "Аккаунтов к обновлению: 0" in h.last()
    await h.click(h.button("Отключить"))
    async with session_factory() as session:
        assert (await session.get(Panel, panel_id)).is_active is False  # type: ignore[union-attr]
    await h.click(cb("ename", panel_id))
    await h.say("NL-2")
    async with session_factory() as session:
        assert (await session.get(Panel, panel_id)).name == "NL-2"  # type: ignore[union-attr]


async def _subscriber(session_factory: SessionFactory, h: Harness, panel_id: int, inbound_id: int) -> None:
    await add_user(session_factory, h.ctx.settings, 501)
    async with session_factory.begin() as session:
        sub = Subscription(
            user_id=501,
            client_uuid="11111111-1111-4111-8111-111111111111",
            status=SubscriptionStatus.ACTIVE,
            expires_at=utcnow() + timedelta(days=10),
        )
        session.add(sub)
        await session.flush()
        session.add(
            Provision(
                subscription_id=sub.id,
                panel_id=panel_id,
                inbound_id=inbound_id,
                inbound_key=inbound_id,
                remote_email=f"tg501-{inbound_id}",
                desired_state=ProvisionState.ENABLED,
                actual_state=ProvisionState.ENABLED,
            )
        )


async def test_delete_with_cleanup_removes_our_accounts(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = RealityPanel()
    h = await _harness(session_factory, monkeypatch, fake)
    panel_id = await _panel(session_factory)
    async with session_factory.begin() as session:
        inbound = Inbound(panel_id=panel_id, remote_id=1, name="in", params=json.dumps({"port": 443}))
        session.add(inbound)
        await session.flush()
        inbound_id = inbound.id
    await _subscriber(session_factory, h, panel_id, inbound_id)
    await h.click(cb("del", panel_id))
    assert "аккаунтов, созданных ботом: 1" in h.last()
    await h.click(h.button("убрать клиентов"))
    await h.click(h.button("Да, удалить"))
    assert ("delete", 1, f"tg501-{inbound_id}") in fake.calls
    async with session_factory() as session:
        assert await session.get(Panel, panel_id) is None
        assert await session.scalar(select(func.count(Provision.id))) == 0
    assert "Убрано аккаунтов с панели: 1" in h.last()
    assert panel_id in h.ctx.panels.invalidated


async def test_delete_keeping_accounts(session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = RealityPanel()
    h = await _harness(session_factory, monkeypatch, fake)
    panel_id = await _panel(session_factory)
    await h.click(cb("delc", panel_id, "0"))
    await h.click(h.button("Да, удалить"))
    assert not [c for c in fake.calls if c[0] == "delete"]
    async with session_factory() as session:
        assert await session.get(Panel, panel_id) is None


async def test_inbounds_toggle_and_location(session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    h = await _harness(session_factory, monkeypatch)
    panel_id = await _panel(session_factory)
    async with session_factory.begin() as session:
        session.add(Location(name="Германия", flag="🇩🇪"))
    await h.click(cb("refr", panel_id))
    assert "новых 1" in h.last()
    await h.click(cb("inbs", panel_id))
    await h.click(h.button("in-443"))
    assert "выключен" in h.last()
    await h.click(h.button("Включить для пользователей"))
    assert "нет ни одной точки подключения" in h.last()
    await h.click(h.button("Локация"))
    await h.click(h.button("Германия"))
    async with session_factory() as session:
        inbound = await session.scalar(select(Inbound))
    assert inbound is not None and inbound.is_active and inbound.location_id is not None


async def test_create_reality_inbound(session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = RealityPanel()
    h = await _harness(session_factory, monkeypatch, fake)

    async def fake_checks(hosts: Any) -> list[service.SniCheck]:
        return [service.SniCheck(hosts[0], True, 20), service.SniCheck(hosts[1], False, 30, "h2")]

    async def fake_check(host: str, **kwargs: Any) -> service.SniCheck:
        return service.SniCheck(host, host.endswith(".com"), 15, None if host.endswith(".com") else "tls13")

    monkeypatch.setattr(service, "check_snis", fake_checks)
    monkeypatch.setattr(service, "check_sni", fake_check)
    panel_id = await _panel(session_factory)
    await h.click(cb("rnew", panel_id))
    await h.click(h.button("443"))
    assert "уже занят" in h.last()
    await h.click(cb("rnew", panel_id))
    await h.say("8443")
    assert "TLS 1.3" in h.last()
    assert any(text.startswith("❌") and "нет HTTP/2" in text for text, _ in h.buttons())
    await h.click(h.button("Указать свой"))
    await h.say("example.org")
    assert "не подходит" in h.last()
    await h.say("www.example.com")
    await h.click(h.button("без локации"))
    assert "Порт: 8443" in h.last()
    await h.click(h.button("Создать"))
    assert fake.created == [(8443, "www.example.com")]
    async with session_factory() as session:
        inbound = await session.scalar(select(Inbound).where(Inbound.remote_id == 11))
        endpoint = await session.scalar(select(Endpoint))
    assert inbound is not None and inbound.is_active
    assert endpoint is not None and endpoint.address == "1.2.3.4"
    assert "Inbound создан" in h.last()


async def test_locations_crud(session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    h = await _harness(session_factory, monkeypatch)
    await h.click(cb("locs"))
    await h.click(h.button("Добавить локацию"))
    await h.say("🇩🇪 Германия")
    await h.click(cb("locnew"))
    await h.say("🇳🇱 Нидерланды")
    async with session_factory() as session:
        de, nl = (await session.scalars(select(Location).order_by(Location.sort_order))).all()
    assert (de.flag, de.name) == ("🇩🇪", "Германия")
    await h.click(cb("locmv", nl.id, "up"))
    async with session_factory() as session:
        first = await session.scalar(select(Location).order_by(Location.sort_order))
    assert first is not None and first.id == nl.id
    await h.click(cb("locren", de.id))
    await h.say("🇩🇪 Франкфурт")
    await h.click(cb("loct", de.id))
    async with session_factory() as session:
        renamed = await session.get(Location, de.id)
    assert renamed is not None and renamed.name == "Франкфурт" and renamed.is_active is False
    await h.click(cb("locdely", de.id))
    async with session_factory() as session:
        assert await session.get(Location, de.id) is None


async def test_endpoint_presets_preview_and_overrides(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await _harness(session_factory, monkeypatch)
    panel_id = await _panel(session_factory)
    params = {**default_inbound().params, "port": 443, "protocol": "vless"}
    async with session_factory.begin() as session:
        inbound = Inbound(panel_id=panel_id, remote_id=1, name="in", params=json.dumps(params), flow="xtls-rprx-vision")
        session.add(inbound)
        await session.flush()
        inbound_id = inbound.id
    await h.click(cb("eps", inbound_id))
    assert "Пока ни одной" in h.last()
    await h.click(h.button("Добавить точку"))
    await h.click(h.button("CDN"))
    assert "XHTTP" in (h.popups()[-1] or "")
    await h.click(h.button("Relay L4"))
    await h.say("5.6.7.8:8443")
    async with session_factory() as session:
        endpoint = await session.scalar(select(Endpoint))
    assert endpoint is not None
    assert (endpoint.kind, endpoint.profile, endpoint.address, endpoint.port) == (
        EndpointKind.RELAY,
        EndpointProfile.WHITELIST,
        "5.6.7.8",
        8443,
    )
    assert "vless://00000000-0000-4000-8000-000000000000@5.6.7.8:8443" in h.last()
    assert "pbk=PUBKEY" in h.last()

    await h.click(cb("epovr", endpoint.id))
    await h.say("[1, 2]")
    assert "Не получилось применить" in h.last()
    await h.say('{"sni": "ok.ru", "fp": "firefox"}')
    assert "sni=ok.ru" in h.last()
    await h.click(cb("epprof", endpoint.id))
    async with session_factory() as session:
        endpoint = await session.get(Endpoint, endpoint.id)
    assert endpoint is not None and endpoint.profile == EndpointProfile.STANDARD
    await h.click(cb("epdely", endpoint.id))
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Endpoint.id))) == 0


async def test_ru_sni_preset_needs_reality(session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    h = await _harness(session_factory, monkeypatch)
    panel_id = await _panel(session_factory)
    params = {"type": "ws", "security": "tls", "port": 443}
    async with session_factory.begin() as session:
        inbound = Inbound(panel_id=panel_id, remote_id=1, name="in", params=json.dumps(params))
        session.add(inbound)
        await session.flush()
        inbound_id = inbound.id
    await h.click(cb("eppre", inbound_id, "ru_sni"))
    assert "Reality" in (h.popups()[-1] or "")


async def test_health_screen(session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    h = await _harness(session_factory, monkeypatch)
    await _panel(session_factory)
    await h.click(cb("health"))
    assert "Состояние серверов" in h.last()
    await h.click(h.button("Проверить сейчас"))
    assert "Проверка завершена" in h.last()
    async with session_factory() as session:
        panel = await session.scalar(select(Panel))
    assert panel is not None and panel.last_ok_at is not None


async def test_requires_panels_permission(session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    h = await _harness(session_factory, monkeypatch)
    await h.click(cb("home"), role=AdminRole.SUPPORT)
    assert h.screens() == []


def test_parsers() -> None:
    assert service.normalize_url(" https://a.example.com:2053/path/ ") == "https://a.example.com:2053/path"
    assert service.normalize_url("ftp://x") is None
    assert service.parse_host_port("1.2.3.4") == ("1.2.3.4", None)
    assert service.parse_host_port("relay.example.ru:8443") == ("relay.example.ru", 8443)
    assert service.parse_host_port("[2001:db8::1]:443") == ("2001:db8::1", 443)
    assert service.parse_host_port("host:99999") is None
    assert service.parse_host_port("a b") is None
    from app.modules.admin_servers.locations import parse_location

    assert parse_location("🇩🇪 Германия") == ("🇩🇪", "Германия")
    assert parse_location("Amsterdam") == ("", "Amsterdam")
    assert parse_location("") is None


async def test_check_sni_unreachable() -> None:
    result = await service.check_sni("127.0.0.1", port=1, limit_sec=2)
    assert not result.ok and result.problem == "unreachable"


async def test_admin_role_cannot_change_panel_connection(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """URL, credentials, TLS checking and new panels are owner-only, like in the web admin."""
    h = await _harness(session_factory, monkeypatch)
    panel_id = await _panel(session_factory, username="admin", password=h.ctx.secret_box.encrypt("pw"))
    admin = AdminRole.ADMIN
    await h.click(cb("home"), role=admin)
    assert all("Добавить сервер" not in text for text, _ in h.buttons())
    await h.click(cb("edit", panel_id), role=admin)
    actions = {SrvCb.unpack(data).a for _, data in h.buttons() if data}
    assert "ename" in actions and not actions & {"eurl", "ecreds", "etls"}

    await h.click(cb("eurl", panel_id), role=admin)
    await h.say("https://attacker.example", role=admin)
    await h.click(cb("ecreds", panel_id), role=admin)
    await h.say("evil evil", role=admin)
    await h.click(cb("etls", panel_id), role=admin)
    assert "только владелец" in (h.popups()[-1] or "")
    # a prompt opened by the owner cannot be answered with a lower role either
    await h.click(cb("eurl", panel_id))
    await h.say("https://attacker.example", role=admin)
    async with session_factory() as session:
        panel = await session.get(Panel, panel_id)
    assert panel is not None and panel.url == "https://1.2.3.4:2053/x"
    assert panel.username == "admin" and panel.tls_verify is True
    assert "только владелец" in h.last()

    await h.click(cb("add"), role=admin)
    await h.click(cb("kind", value="xui"), role=admin)
    await h.click(cb("kind", value="xui"))
    await h.say("Rogue")
    await h.say("https://rogue.example")
    await h.say("admin s3cret")
    await h.click(cb("save"), role=admin)
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Panel.id))) == 1


async def test_reality_endpoint_uses_public_host(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel URL may be internal; clients get the public address (XUI_PUBLIC_HOST or an existing endpoint)."""
    fake = RealityPanel()
    h = await make_harness(
        session_factory,
        [module("admin_servers")],
        xui_host="http://127.0.0.1:2053",
        xui_public_host="vpn.example.com",
        xui_username="admin",
        xui_password="pw",
    )
    h.ctx.panels.default = fake
    monkeypatch.setattr(service, "build_backend", lambda *args, **kwargs: fake)

    async def fake_checks(hosts: Any) -> list[service.SniCheck]:
        return [service.SniCheck(hosts[0], True, 20)]

    monkeypatch.setattr(service, "check_snis", fake_checks)
    async with session_factory.begin() as session:
        panel = Panel(name="local", kind=PanelKind.XUI, url="http://127.0.0.1:2053")
        session.add(panel)
        await session.flush()
        panel_id = panel.id
    await h.click(cb("rnew", panel_id))
    await h.say("8443")
    await h.click(h.buttons()[0][1] or "")
    await h.click(h.button("без локации"))
    assert "vpn.example.com" in h.last()
    await h.click(h.button("Создать"))
    async with session_factory() as session:
        endpoint = await session.scalar(select(Endpoint))
    assert endpoint is not None and endpoint.address == "vpn.example.com"

    # another panel: the address of its existing direct endpoint wins over the URL host; it can be changed
    fake.inbounds.clear()
    async with session_factory.begin() as session:
        other = Panel(name="remote", kind=PanelKind.XUI, url="https://panel.internal:2053")
        session.add(other)
        await session.flush()
        inbound = Inbound(panel_id=other.id, remote_id=1, name="old", params=json.dumps({"port": 443}))
        session.add(inbound)
        await session.flush()
        session.add(
            Endpoint(
                inbound_id=inbound.id,
                kind=EndpointKind.DIRECT,
                profile=EndpointProfile.STANDARD,
                address="de.example.net",
            )
        )
        other_id = other.id
    fake.inbounds.append(default_inbound(remote_id=1, port=443))
    await h.click(cb("rnew", other_id))
    await h.say("9443")
    await h.click(h.buttons()[0][1] or "")
    await h.click(h.button("без локации"))
    assert "de.example.net" in h.last()
    await h.click(h.button("адрес"))
    await h.say("not a host!")
    await h.say("nl.example.net")
    assert "nl.example.net" in h.last()
    await h.click(h.button("Создать"))
    async with session_factory() as session:
        addresses = set((await session.scalars(select(Endpoint.address))).all())
    assert "nl.example.net" in addresses and "panel.internal" not in addresses
