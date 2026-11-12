"""Telegram proxy on 3x-ui ``mtproto`` inbounds: client API, inventory import, registry wiring."""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select

from app.config import Settings
from app.db.models import Endpoint, Inbound, Panel, PanelKind
from app.db.session import SessionFactory
from app.services.panels import xui as xui_module
from app.services.panels.base import InboundInfo, PanelError, PanelUnsupportedError
from app.services.panels.inventory import (
    bootstrap_from_env,
    is_mtproto_inbound,
    refresh_inbounds,
    set_tg_proxy_flag,
    tg_proxy_enabled,
)
from app.services.panels.mtproto import MtprotoProxyBackend, ProxyUserSpec, fake_tls_secret, parse_secret
from app.services.panels.registry import build_backend
from app.services.panels.telemt import TelemtBackend
from app.services.panels.xui import (
    MTPROTO_DEFAULT_DOMAIN,
    XuiBackend,
    build_mtproto_client,
    mtproto_secret,
    mtproto_sub_id,
    parse_inbound,
)
from app.services.provisioning import XUI_EMAIL_RE
from app.utils.crypto import SecretBox
from tests.conftest import make_settings
from tests.fakes import FakePanel, default_inbound
from tests.test_xui_backend import BASE, PASSWORD, TOKEN, USER, FakeXui, fail, ok

DOMAIN = "petrovich.ru"
KEY_A = "a" * 32
KEY_B = "b" * 32
TAG = "c" * 32
MT_INBOUND = 7


class FakeXuiMtproto(FakeXui):
    """FakeXui with an mtproto inbound: the panel generates a missing secret, keeps it on updates without one and
    rebuilds the domain part from the inbound's ``fakeTlsDomain``."""

    def __init__(self, *, generation: Any = "modern", version: str = "3.11.0") -> None:
        super().__init__(generation, token=TOKEN)
        self.version = version
        self.inbounds[MT_INBOUND] = {
            "id": MT_INBOUND,
            "remark": "tg",
            "enable": True,
            "port": 8443,
            "protocol": "mtproto",
            "settings": {"fakeTlsDomain": DOMAIN, "clients": []},
            "streamSettings": {},
            "up": 0,
            "down": 0,
        }

    def _secret(self, given: str, current: str = "") -> str:
        parsed = parse_secret(given) if given else None
        if parsed is None:
            parsed = parse_secret(current) if current else None
        key = parsed.key if parsed else secrets.token_hex(16)
        return fake_tls_secret(key, self.inbounds[MT_INBOUND]["settings"]["fakeTlsDomain"])

    async def status(self, request: web.Request) -> web.Response:
        obj: dict[str, Any] = {"xray": {"state": "running"}}
        if self.modern:
            obj["panelVersion"] = self.version
        return ok(obj)

    async def m_add(self, request: web.Request) -> web.Response:
        body = await request.json()
        if MT_INBOUND in body["inboundIds"]:
            assert "id" not in body["client"] and "flow" not in body["client"]
            if body["client"]["email"] in self.clients:
                return fail(f"Something went wrong (email already in use: {body['client']['email']}\n)")
            body["client"]["secret"] = self._secret(body["client"].get("secret", ""))
            stored = self._modern_read_shape(body["client"])
            self.clients[body["client"]["email"]] = {"client": stored, "inboundIds": body["inboundIds"]}
            return ok(msg="Inbound client(s) have been added.")
        return await super().m_add(request)

    async def m_update(self, request: web.Request) -> web.Response:
        entry = self.clients.get(request.match_info["email"])
        if entry is None or MT_INBOUND not in entry["inboundIds"]:
            return await super().m_update(request)
        body = await request.json()
        body["secret"] = self._secret(body.get("secret", ""), entry["client"].get("secret", ""))
        entry["client"] = self._modern_read_shape(body)
        return ok(msg="Inbound client has been updated.")


Started = Callable[..., Awaitable[tuple[FakeXuiMtproto, XuiBackend]]]


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xui_module, "_RETRY_BASE_DELAY", 0)


@pytest.fixture
async def start() -> AsyncIterator[Started]:
    servers: list[TestServer] = []
    backends: list[XuiBackend] = []

    async def _start(**fake_kw: Any) -> tuple[FakeXuiMtproto, XuiBackend]:
        fake = FakeXuiMtproto(**fake_kw)
        server = TestServer(fake.app())
        await server.start_server()
        servers.append(server)
        backend = XuiBackend(str(server.make_url(BASE)), username=USER, password=PASSWORD, timeout=5)
        backends.append(backend)
        return fake, backend

    yield _start
    for backend in backends:
        await backend.close()
    for server in servers:
        await server.close()


def spec(**overrides: Any) -> ProxyUserSpec:
    values: dict[str, Any] = {"name": "tg42-5-tg", "enabled": True, "key": KEY_A, "tls_domain": DOMAIN, "tg_id": 42}
    values.update(overrides)
    return ProxyUserSpec(**values)


def writes(fake: FakeXui) -> list[tuple[str, Any]]:
    return [(path, body) for method, path, body in fake.calls if method == "POST" and "/clients/" in path]


# ---------------------------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------------------------
def test_parse_mtproto_inbound() -> None:
    raw = {"id": 3, "remark": "tg", "protocol": "mtproto", "port": 443, "enable": True, "settings": "{}"}
    info = parse_inbound(raw)
    assert info.protocol == "mtproto" and info.port == 443
    assert info.params == {"fake_tls_domain": MTPROTO_DEFAULT_DOMAIN, "local": True}
    node = parse_inbound({**raw, "settings": {"fakeTlsDomain": " wb.ru "}, "nodeId": 2})
    assert node.params == {"fake_tls_domain": "wb.ru", "local": False}


def test_build_mtproto_client_has_no_vless_identity() -> None:
    expires = datetime(2027, 1, 1, tzinfo=UTC)
    client = build_mtproto_client(spec(expires_at=expires, max_unique_ips=2, ad_tag=TAG.upper(), comment="c"))
    assert client == {
        "email": "tg42-5-tg",
        "enable": True,
        "expiryTime": int(expires.timestamp() * 1000),
        "totalGB": 0,
        "limitIp": 2,
        "tgId": 42,
        "subId": mtproto_sub_id("tg42-5-tg"),
        "comment": "c",
        "secret": fake_tls_secret(KEY_A, DOMAIN),
        "adTag": TAG,
        "reset": 0,
    }


def test_mtproto_secret_domain_fallbacks() -> None:
    current = fake_tls_secret(KEY_B, "wb.ru")
    assert mtproto_secret(spec(key=None), current) == current
    assert mtproto_secret(spec(key=None)) == ""
    assert mtproto_secret(spec(tls_domain=""), current) == fake_tls_secret(KEY_A, "wb.ru")
    assert mtproto_secret(spec(tls_domain="")) == fake_tls_secret(KEY_A, MTPROTO_DEFAULT_DOMAIN)


def test_proxy_emails_never_look_like_vpn_clients() -> None:
    assert XUI_EMAIL_RE.fullmatch("tg42-5") and not XUI_EMAIL_RE.fullmatch("tg42-5-tg")


# ---------------------------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------------------------
async def test_xui_backend_is_a_proxy_backend(start: Started) -> None:
    _, backend = await start()
    assert isinstance(backend, MtprotoProxyBackend)


async def test_upsert_creates_and_reads_back_secret(start: Started) -> None:
    fake, backend = await start()
    account = await backend.upsert_proxy_user(MT_INBOUND, spec(ad_tag=TAG))
    ((path, body),) = writes(fake)
    assert path == "/panel/api/clients/add" and body["inboundIds"] == [MT_INBOUND]
    assert account.key == KEY_A and account.tls_secret == fake_tls_secret(KEY_A, DOMAIN)
    assert account.ad_tag == TAG and account.enabled and account.name == "tg42-5-tg"
    fake.calls.clear()
    again = await backend.upsert_proxy_user(MT_INBOUND, spec(ad_tag=TAG))
    assert writes(fake) == [] and again.key == KEY_A


async def test_upsert_without_key_lets_the_panel_generate_it(start: Started) -> None:
    fake, backend = await start()
    account = await backend.upsert_proxy_user(MT_INBOUND, spec(key=None))
    assert account.key is not None and account.tls_secret == fake.raw_client("tg42-5-tg")["secret"]  # type: ignore[index]
    fake.calls.clear()
    kept = await backend.upsert_proxy_user(MT_INBOUND, spec(key=None, enabled=False))
    assert kept.key == account.key and not kept.enabled
    assert [p for p, _ in writes(fake)] == ["/panel/api/clients/update/tg42-5-tg"]


async def test_new_key_and_domain_change(start: Started) -> None:
    fake, backend = await start()
    await backend.upsert_proxy_user(MT_INBOUND, spec())
    rotated = await backend.upsert_proxy_user(MT_INBOUND, spec(key=KEY_B))
    assert rotated.key == KEY_B
    fake.inbounds[MT_INBOUND]["settings"]["fakeTlsDomain"] = "wb.ru"
    moved = await backend.upsert_proxy_user(MT_INBOUND, spec(key=KEY_B, tls_domain="wb.ru"))
    assert moved.tls_secret == fake_tls_secret(KEY_B, "wb.ru")


async def test_get_list_delete(start: Started) -> None:
    fake, backend = await start()
    await backend.upsert_proxy_user(MT_INBOUND, spec())
    await backend.upsert_proxy_user(MT_INBOUND, spec(name="tg43-5-tg", key=KEY_B))
    fake.traffic["tg42-5-tg"] = (10, 20)
    users = await backend.list_proxy_users(MT_INBOUND)
    assert sorted(u.name for u in users) == ["tg42-5-tg", "tg43-5-tg"]
    assert next(u for u in users if u.name == "tg42-5-tg").used_bytes == 30
    assert await backend.list_proxy_users(1) == []
    got = await backend.get_proxy_user(MT_INBOUND, "tg43-5-tg")
    assert got is not None and got.key == KEY_B
    assert await backend.get_proxy_user(1, "tg43-5-tg") is None
    await backend.delete_proxy_user(MT_INBOUND, "tg43-5-tg")
    await backend.delete_proxy_user(MT_INBOUND, "tg43-5-tg")
    assert await backend.get_proxy_user(MT_INBOUND, "tg43-5-tg") is None


async def test_old_panels_are_unsupported(start: Started) -> None:
    _, backend = await start(version="3.4.2")
    with pytest.raises(PanelUnsupportedError):
        await backend.upsert_proxy_user(MT_INBOUND, spec())
    _, legacy = await start(generation="legacy")
    with pytest.raises(PanelUnsupportedError):
        await legacy.list_proxy_users(MT_INBOUND)


async def test_target_is_required(start: Started) -> None:
    _, backend = await start()
    with pytest.raises(PanelError):
        await backend.upsert_proxy_user(None, spec())


# ---------------------------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------------------------
def mtproto_inbound(remote_id: int = 9, *, enabled: bool = True, local: bool = True) -> InboundInfo:
    return InboundInfo(remote_id, "tg", "mtproto", 8443, enabled, {"fake_tls_domain": DOMAIN, "local": local})


class StubRegistry:
    def __init__(self, panels: dict[int, FakePanel]) -> None:
        self.panels = panels

    async def get(self, panel_id: int) -> FakePanel:
        return self.panels[panel_id]


async def add_panel(session_factory: SessionFactory) -> int:
    async with session_factory.begin() as session:
        panel = Panel(name="DE", kind=PanelKind.XUI, url="https://panel.example.com:2053/p")
        session.add(panel)
        await session.flush()
        return panel.id


async def rows(session_factory: SessionFactory) -> dict[int, Inbound]:
    async with session_factory() as session:
        return {r.remote_id: r for r in await session.scalars(select(Inbound))}


async def test_refresh_imports_mtproto_outside_vpn_rotation(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    fake = FakePanel(inbounds=[default_inbound(1), mtproto_inbound(9)])
    result = await refresh_inbounds(
        session_factory, StubRegistry({panel_id: fake}), panel_id, activate_new=True, endpoint_address="1.2.3.4"
    )
    found = await rows(session_factory)
    mt = found[9]
    assert result.mtproto == [mt.id] and found[1].is_active and not mt.is_active and mt.flow == ""
    assert is_mtproto_inbound(mt) and not is_mtproto_inbound(found[1])
    assert tg_proxy_enabled(mt)
    assert json.loads(mt.params or "") == {
        "fake_tls_domain": DOMAIN,
        "local": True,
        "panel_enabled": True,
        "port": 8443,
        "protocol": "mtproto",
        "tg_proxy": True,
    }
    async with session_factory() as session:
        endpoint_inbounds = list(await session.scalars(select(Endpoint.inbound_id)))
    assert endpoint_inbounds == [found[1].id]  # no VPN endpoint for the proxy inbound


async def test_flag_survives_refresh_and_is_cleared_when_inbound_vanishes(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    fake = FakePanel(inbounds=[default_inbound(1), mtproto_inbound(9)])
    registry = StubRegistry({panel_id: fake})
    await refresh_inbounds(session_factory, registry, panel_id)
    assert not tg_proxy_enabled((await rows(session_factory))[9])  # new inbounds are opt-in
    async with session_factory.begin() as session:
        row = await session.scalar(select(Inbound).where(Inbound.remote_id == 9))
        assert row is not None and set_tg_proxy_flag(row, True)
    fake.inbounds = [default_inbound(1), mtproto_inbound(9, enabled=False)]
    await refresh_inbounds(session_factory, registry, panel_id)
    mt = (await rows(session_factory))[9]
    assert json.loads(mt.params or "")["tg_proxy"] is True and not tg_proxy_enabled(mt)  # disabled on the panel
    fake.inbounds = [default_inbound(1), mtproto_inbound(9)]
    await refresh_inbounds(session_factory, registry, panel_id)
    assert tg_proxy_enabled((await rows(session_factory))[9])
    fake.inbounds = [default_inbound(1)]
    result = await refresh_inbounds(session_factory, registry, panel_id)
    mt = (await rows(session_factory))[9]
    assert mt.id in result.deactivated and json.loads(mt.params or "")["tg_proxy"] is False


async def test_inbound_switched_to_mtproto_leaves_vpn_rotation(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    fake = FakePanel(inbounds=[default_inbound(1)])
    registry = StubRegistry({panel_id: fake})
    await refresh_inbounds(session_factory, registry, panel_id, activate_new=True)
    fake.inbounds = [mtproto_inbound(1)]
    result = await refresh_inbounds(session_factory, registry, panel_id)
    row = (await rows(session_factory))[1]
    assert not row.is_active and row.flow == "" and row.id in result.deactivated
    assert not tg_proxy_enabled(row)


async def test_remote_node_inbound_is_not_usable(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    fake = FakePanel(inbounds=[mtproto_inbound(9, local=False)])
    await refresh_inbounds(session_factory, StubRegistry({panel_id: fake}), panel_id, activate_new=True)
    assert not tg_proxy_enabled((await rows(session_factory))[9])


async def test_set_flag_ignores_vpn_inbounds() -> None:
    row = Inbound(params=json.dumps({"protocol": "vless"}))
    assert not set_tg_proxy_flag(row, True) and not tg_proxy_enabled(row)
    assert not set_tg_proxy_flag(Inbound(params="garbage"), True)


async def test_bootstrap_counts_vpn_inbounds_only(session_factory: SessionFactory) -> None:
    settings = make_settings(xui_host="https://panel.example.com:2053/p", xui_username="u", xui_password="p")
    box = SecretBox("k" * 32)
    fake = FakePanel(inbounds=[mtproto_inbound(9)])

    class AnyPanel:
        async def get(self, panel_id: int) -> FakePanel:
            return fake

    result = await bootstrap_from_env(session_factory, settings, box, AnyPanel())
    assert result is not None and result.inbounds_imported == 0
    assert result.errors and "no VLESS inbounds" in result.errors[0]
    assert tg_proxy_enabled((await rows(session_factory))[9])


# ---------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------
def test_build_telemt_backend(settings: Settings) -> None:
    box = SecretBox("k" * 32)
    panel = Panel(
        id=5,
        name="tg",
        kind=PanelKind.TELEMT,
        url="http://telemt:9091",
        api_token=box.encrypt("Bearer abc"),
        tls_verify=False,
        options=json.dumps({"public_host": "1.2.3.4", "public_port": 443, "headers": {"X-A": "1"}}),
    )
    backend = build_backend(panel, box, settings)
    assert isinstance(backend, TelemtBackend)
    assert backend.base == "http://telemt:9091" and backend._auth == "Bearer abc"
    assert (backend.public_host, backend.public_port) == ("1.2.3.4", 443)
    plain = build_backend(
        Panel(id=6, name="t", kind=PanelKind.TELEMT, url="http://t:1", options='{"public_port": true}'), box, settings
    )
    assert isinstance(plain, TelemtBackend) and plain._auth is None and plain.public_port is None


def test_build_telemt_backend_bad_url_is_auth_error(settings: Settings) -> None:
    from app.services.panels.base import PanelAuthError

    with pytest.raises(PanelAuthError):
        build_backend(Panel(id=7, name="t", kind=PanelKind.TELEMT, url="telemt:9091"), SecretBox("k" * 32), settings)
