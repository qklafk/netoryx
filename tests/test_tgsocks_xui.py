"""Telegram SOCKS5 accounts on 3x-ui ``mixed`` inbounds: links, account-list edits on both API generations,
inventory import and selection."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select

from app.db.models import Inbound, Panel, PanelKind
from app.db.session import SessionFactory
from app.services.panels import xui as xui_module
from app.services.panels.base import PanelError
from app.services.panels.inventory import (
    is_proxy_inbound,
    is_socks_inbound,
    refresh_inbounds,
    set_tg_socks_flag,
    tg_socks_enabled,
)
from app.services.panels.socks import (
    SocksAccount,
    SocksLink,
    SocksProxyBackend,
    is_managed_username,
    new_password,
    username_for,
)
from app.services.panels.xui import XuiBackend, parse_inbound
from tests.test_xui_backend import BASE, PASSWORD, TOKEN, USER, FakeXui, ok

SOCKS_ID = 5


class FakeXuiSocks(FakeXui):
    """FakeXui with a ``mixed`` inbound whose accounts are edited through ``inbounds/update`` (both generations)."""

    def __init__(self, generation: Any = "modern", *, auth: str = "password") -> None:
        super().__init__(generation, token=TOKEN)
        self.updated: list[int] = []
        self.on_update: Callable[[], None] | None = None
        self.inbounds[SOCKS_ID] = {
            "id": SOCKS_ID,
            "up": 0,
            "down": 0,
            "total": 0,
            "remark": "socks",
            "enable": True,
            "expiryTime": 0,
            "listen": "",
            "port": 1080,
            "protocol": "mixed",
            "settings": {
                "auth": auth,
                "accounts": [{"user": "admin-made", "pass": "keep-me"}],
                "udp": False,
                "ip": "127.0.0.1",
            },
            "streamSettings": {},
            "sniffing": {"enabled": False},
            "tag": "inbound-1080",
        }

    def accounts(self) -> dict[str, str]:
        return {a["user"]: a["pass"] for a in self.inbounds[SOCKS_ID]["settings"]["accounts"]}

    def _inbound_view(self, inbound: dict[str, Any], *, with_stats: bool) -> dict[str, Any]:
        if inbound["protocol"] != "mixed":
            return super()._inbound_view(inbound, with_stats=with_stats)
        view = json.loads(json.dumps(inbound))
        if not self.modern:  # legacy panels return these as JSON strings
            for name in ("settings", "streamSettings", "sniffing"):
                view[name] = json.dumps(inbound[name])
        view["clientStats"] = []
        return view

    def _route(self, name: str, only: Any = None) -> Callable[[web.Request], Awaitable[web.Response]]:
        return super()._route(name, None if name == "l_inbound_update" else only)

    async def l_inbound_update(self, request: web.Request) -> web.Response:
        body = await request.json()
        assert all(isinstance(body[k], str) for k in ("settings", "streamSettings", "sniffing"))
        inbound_id = int(request.match_info["id"])
        self.updated.append(inbound_id)
        inbound = self.inbounds[inbound_id]
        inbound["settings"] = json.loads(body["settings"])
        for key in ("remark", "port", "protocol", "listen"):
            assert body[key] == inbound[key]
        if self.on_update is not None:
            hook, self.on_update = self.on_update, None
            hook()
        return ok(msg="Inbound has been successfully updated.")


Started = Callable[..., Awaitable[tuple[FakeXuiSocks, XuiBackend]]]


@pytest.fixture(autouse=True)
def _no_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xui_module, "_RETRY_BASE_DELAY", 0)
    monkeypatch.setattr(xui_module, "SOCKS_RETRY_DELAY", 0)


@pytest.fixture
async def start() -> AsyncIterator[Started]:
    servers: list[TestServer] = []
    backends: list[XuiBackend] = []

    async def _start(generation: Any = "modern", **fake_kw: Any) -> tuple[FakeXuiSocks, XuiBackend]:
        fake = FakeXuiSocks(generation, **fake_kw)
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


generations = pytest.mark.parametrize("generation", ["modern", "legacy"])


# ---------------------------------------------------------------------------------------------
# Links and names
# ---------------------------------------------------------------------------------------------
def test_link_format_and_url_encoding() -> None:
    link = SocksLink("proxy.example.com", 1080, "tg42", "p a&ss/+%é")
    for url, prefix in ((link.tg_url, "tg://socks?"), (link.tme_url, "https://t.me/socks?")):
        assert url.startswith(prefix)
        assert "+" not in url.split("?", 1)[1].replace("%2B", "")  # a space is %20, a plus is %2B
        query = parse_qs(urlsplit(url).query)
        assert query == {"server": ["proxy.example.com"], "port": ["1080"], "user": ["tg42"], "pass": ["p a&ss/+%é"]}
    assert "pass=p%20a%26ss%2F%2B%25%C3%A9" in link.tg_url


def test_names_and_passwords() -> None:
    assert username_for(42) == "tg42"
    assert is_managed_username("tg42") and is_managed_username("tg1234567890123")
    for other in ("admin", "tg", "tg4x", "TG42", "tg42-1-tg", ""):
        assert not is_managed_username(other)
    first, second = new_password(), new_password()
    assert first != second and len(first) >= 16 and first.isalnum()


# ---------------------------------------------------------------------------------------------
# Account list edits (both API generations)
# ---------------------------------------------------------------------------------------------
@generations
async def test_add_keeps_other_accounts_and_inbound_fields(start: Started, generation: str) -> None:
    fake, backend = await start(generation)
    assert isinstance(backend, SocksProxyBackend)
    accounts = await backend.apply_socks_users(SOCKS_ID, {"tg1": "pw1"})
    assert {a.user: a.password for a in accounts} == {"admin-made": "keep-me", "tg1": "pw1"}
    assert fake.accounts() == {"admin-made": "keep-me", "tg1": "pw1"}
    inbound = fake.inbounds[SOCKS_ID]
    assert inbound["port"] == 1080 and inbound["protocol"] == "mixed" and inbound["settings"]["udp"] is False
    assert inbound["settings"]["ip"] == "127.0.0.1" and inbound["settings"]["auth"] == "password"
    assert fake.updated == [SOCKS_ID]  # no other inbound is ever written
    assert await backend.list_socks_users(SOCKS_ID) == [
        SocksAccount("admin-made", "keep-me"),
        SocksAccount("tg1", "pw1"),
    ]


@generations
async def test_idempotent_rotate_and_remove(start: Started, generation: str) -> None:
    fake, backend = await start(generation)
    await backend.apply_socks_users(SOCKS_ID, {"tg1": "pw1", "tg2": "pw2"})
    fake.updated.clear()

    await backend.apply_socks_users(SOCKS_ID, {"tg1": "pw1"})  # nothing changes
    await backend.delete_socks_user(SOCKS_ID, "tg-nobody")
    assert fake.updated == []

    await backend.apply_socks_users(SOCKS_ID, {"tg1": "new"})  # rotation replaces only that password
    assert fake.accounts() == {"admin-made": "keep-me", "tg1": "new", "tg2": "pw2"}

    await backend.delete_socks_user(SOCKS_ID, "tg1")
    assert fake.accounts() == {"admin-made": "keep-me", "tg2": "pw2"}
    await backend.apply_socks_users(SOCKS_ID, {"tg3": "pw3"}, ["tg2"])
    assert fake.accounts() == {"admin-made": "keep-me", "tg3": "pw3"}


@generations
async def test_concurrent_updates_do_not_lose_accounts(start: Started, generation: str) -> None:
    fake, backend = await start(generation)
    names = {f"tg{n}": f"pw{n}" for n in range(1, 13)}
    await asyncio.gather(*(backend.apply_socks_users(SOCKS_ID, {name: pw}) for name, pw in names.items()))
    assert fake.accounts() == {"admin-made": "keep-me", **names}
    await asyncio.gather(*(backend.delete_socks_user(SOCKS_ID, name) for name in list(names)[:6]))
    assert fake.accounts() == {"admin-made": "keep-me", **dict(list(names.items())[6:])}


async def test_a_competing_writer_that_overwrites_us_is_repaired(start: Started) -> None:
    fake, backend = await start("modern")

    def other_writer() -> None:  # another process wrote from a stale read right after our update
        fake.inbounds[SOCKS_ID]["settings"]["accounts"] = [{"user": "admin-made", "pass": "keep-me"}]

    fake.on_update = other_writer
    accounts = await backend.apply_socks_users(SOCKS_ID, {"tg1": "pw1"})
    assert {a.user for a in accounts} == {"admin-made", "tg1"}
    assert fake.accounts() == {"admin-made": "keep-me", "tg1": "pw1"}
    assert len(fake.updated) == 2


async def test_gives_up_when_the_change_never_sticks(start: Started) -> None:
    fake, backend = await start("modern")
    original = fake.l_inbound_update

    async def lossy(request: web.Request) -> web.Response:
        response = await original(request)
        fake.inbounds[SOCKS_ID]["settings"]["accounts"] = []
        return response

    fake.l_inbound_update = lossy  # type: ignore[method-assign]
    with pytest.raises(PanelError, match="not stored"):
        await backend.apply_socks_users(SOCKS_ID, {"tg1": "pw1"})


@generations
async def test_auth_is_forced_to_password(start: Started, generation: str) -> None:
    fake, backend = await start(generation, auth="noauth")
    await backend.apply_socks_users(SOCKS_ID, {"tg1": "pw1"})
    assert fake.inbounds[SOCKS_ID]["settings"]["auth"] == "password"
    # Even a no-op request repairs an inbound that was switched back to "noauth".
    fake.inbounds[SOCKS_ID]["settings"]["auth"] = "noauth"
    await backend.apply_socks_users(SOCKS_ID, {"tg1": "pw1"})
    assert fake.inbounds[SOCKS_ID]["settings"]["auth"] == "password"


async def test_refuses_to_touch_other_inbounds(start: Started) -> None:
    fake, backend = await start("modern")
    with pytest.raises(PanelError, match="not a mixed"):
        await backend.apply_socks_users(1, {"tg1": "pw1"})  # the VLESS inbound of the fake panel
    assert fake.updated == []


async def test_duplicate_entries_of_a_managed_account_collapse(start: Started) -> None:
    fake, backend = await start("modern")
    fake.inbounds[SOCKS_ID]["settings"]["accounts"] += [{"user": "tg1", "pass": "a"}, {"user": "tg1", "pass": "b"}]
    await backend.apply_socks_users(SOCKS_ID, {"tg1": "z"})
    assert [a["user"] for a in fake.inbounds[SOCKS_ID]["settings"]["accounts"]].count("tg1") == 1
    assert fake.accounts()["tg1"] == "z"


# ---------------------------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------------------------
def test_parse_mixed_inbound() -> None:
    raw = {
        "id": 3,
        "remark": "s",
        "protocol": "mixed",
        "port": 1080,
        "enable": True,
        "settings": '{"auth": "password"}',
    }
    info = parse_inbound(raw)
    assert info.protocol == "mixed" and info.params == {"socks_auth": "password", "local": True}
    legacy = parse_inbound({**raw, "protocol": "socks", "settings": {}, "nodeId": 4})
    assert legacy.params == {"socks_auth": "noauth", "local": False}


class _Registry:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    async def get(self, panel_id: int) -> Any:
        return self.backend


async def test_refresh_imports_mixed_inbounds_inactive_and_unselected(
    start: Started, session_factory: SessionFactory
) -> None:
    fake, backend = await start("modern")
    async with session_factory.begin() as session:
        panel = Panel(name="x", kind=PanelKind.XUI, url="https://x.example.com:2053", is_active=True)
        session.add(panel)
        await session.flush()
        panel_id = panel.id
    # Even when new inbounds are activated (env bootstrap), a mixed inbound is never selected automatically.
    result = await refresh_inbounds(session_factory, _Registry(backend), panel_id, activate_new=True)
    async with session_factory() as session:
        rows = {r.remote_id: r for r in await session.scalars(select(Inbound).where(Inbound.panel_id == panel_id))}
    mixed = rows[SOCKS_ID]
    assert mixed.id in result.socks and result.mtproto == []
    assert is_socks_inbound(mixed) and is_proxy_inbound(mixed) and not is_proxy_inbound(rows[1])
    assert mixed.is_active is False and not tg_socks_enabled(mixed)
    assert json.loads(mixed.params or "{}")["socks_auth"] == "password"

    # Selection survives refreshes and is cleared when the inbound disappears from the panel.
    async with session_factory.begin() as session:
        row = await session.get(Inbound, mixed.id)
        assert row is not None and set_tg_socks_flag(row, True)
        vless = await session.get(Inbound, rows[1].id)
        assert vless is not None and not set_tg_socks_flag(vless, True)
    await refresh_inbounds(session_factory, _Registry(backend), panel_id)
    async with session_factory() as session:
        row = await session.get(Inbound, mixed.id)
        assert row is not None and tg_socks_enabled(row) and row.is_active is False
    fake.inbounds.pop(SOCKS_ID)
    await refresh_inbounds(session_factory, _Registry(backend), panel_id)
    async with session_factory() as session:
        row = await session.get(Inbound, mixed.id)
        assert row is not None and not tg_socks_enabled(row)
