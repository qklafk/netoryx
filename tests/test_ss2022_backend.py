"""Shadowsocks-2022 clients on both 3x-ui API generations (in-process panel emulation)."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.services.panels import shadowsocks
from app.services.panels import xui as xui_module
from app.services.panels.base import AccountSpec
from app.services.panels.xui import XuiBackend
from tests.test_xui_backend import BASE, PASSWORD, TOKEN, USER, FakeXui, Generation, ok, posts

METHOD = "2022-blake3-aes-128-gcm"
SERVER_KEY = "AAECAwQFBgcICQoLDA0ODw=="
UUID = "6f1c1c2e-8d1f-4b6c-9a51-3e0e2c4d5f60"
EXPIRY = datetime(2027, 1, 15, 12, 0, tzinfo=UTC)
SS_INBOUND = 5


class FakeXuiSs(FakeXui):
    """:class:`FakeXui` with a Shadowsocks inbound: legacy panels address its clients by email."""

    def __init__(self, generation: Generation) -> None:
        super().__init__(generation, token=TOKEN)
        self.add_inbound(
            SS_INBOUND,
            8388,
            {"network": "tcp", "security": "none", "tcpSettings": {"header": {"type": "none"}}},
            protocol="shadowsocks",
            settings={"method": METHOD, "password": SERVER_KEY, "network": "tcp,udp", "clients": []},
        )

    def app(self) -> web.Application:
        app = super().app()
        app.router.add_post(f"{BASE}/panel/api/inbounds/add", self._route("inbound_add"))
        return app

    def _key(self, inbound_id: int, client: dict[str, Any]) -> str:
        return client["email"] if self.inbounds[inbound_id]["protocol"] == "shadowsocks" else client["id"]

    async def inbound_add(self, request: web.Request) -> web.Response:
        body = await request.json()
        inbound_id = max(self.inbounds) + 1
        inbound = {
            **body,
            "id": inbound_id,
            "settings": json.loads(body["settings"]),
            "streamSettings": json.loads(body["streamSettings"]),
            "sniffing": json.loads(body["sniffing"]),
        }
        self.inbounds[inbound_id] = inbound
        return ok(self._inbound_view(inbound, with_stats=True))

    async def l_update(self, request: web.Request) -> web.Response:
        inbound_id, (client,) = await self._legacy_body(request)
        stored = self.inbounds[inbound_id]["settings"]["clients"]
        for index, existing in enumerate(stored):
            if self._key(inbound_id, existing) == request.match_info["uuid"]:
                stored[index] = client
                return ok(msg="Inbound client has been updated.")
        return web.json_response({"success": False, "msg": "Something went wrong (empty client ID\n)", "obj": None})

    async def l_del(self, request: web.Request) -> web.Response:
        inbound_id = int(request.match_info["id"])
        stored = self.inbounds[inbound_id]["settings"]["clients"]
        key = request.match_info["uuid"]
        if not any(self._key(inbound_id, c) == key for c in stored):
            return web.json_response(
                {"success": False, "msg": f"Client Not Found In Inbound For ID: {key}", "obj": None}
            )
        if len(stored) == 1:
            return web.json_response({"success": False, "msg": "no client remained in Inbound", "obj": None})
        stored[:] = [c for c in stored if self._key(inbound_id, c) != key]
        return ok(msg="Inbound client has been deleted.")


Started = Callable[[Generation], Awaitable[tuple[FakeXuiSs, XuiBackend]]]


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xui_module, "_RETRY_BASE_DELAY", 0)


@pytest.fixture
async def start() -> AsyncIterator[Started]:
    servers: list[TestServer] = []
    backends: list[XuiBackend] = []

    async def _start(generation: Generation) -> tuple[FakeXuiSs, XuiBackend]:
        fake = FakeXuiSs(generation)
        server = TestServer(fake.app())
        await server.start_server()
        servers.append(server)
        backend = XuiBackend(str(server.make_url(BASE)), timeout=5, username=USER, password=PASSWORD)
        backends.append(backend)
        return fake, backend

    yield _start
    for backend in backends:
        await backend.close()
    for server in servers:
        await server.close()


def ss_spec(**overrides: Any) -> AccountSpec:
    values: dict[str, Any] = {
        "email": "tg42-9",
        "uuid": UUID,
        "enabled": True,
        "expires_at": EXPIRY,
        "device_limit": 0,
        "traffic_limit_bytes": 10 * 1024**3,
        "tg_id": 42,
        "sub_id": "abcdefgh12345678",
        "comment": "vpn-bot",
        "flow": "",
        "password": shadowsocks.derive_client_key(UUID, 9, METHOD),
    }
    values.update(overrides)
    return AccountSpec(**values)


def has_vless_id(client: dict[str, Any]) -> bool:
    """A VLESS identity: ``uuid`` on v3.1+ (its numeric ``id`` is a table row), ``id`` before."""
    return bool(client.get("uuid")) or isinstance(client.get("id"), str)


BOTH = pytest.mark.parametrize("generation", ["modern", "legacy"])


@BOTH
async def test_client_is_created_with_a_password_and_no_vless_fields(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    spec = ss_spec()
    account = await backend.upsert_account(SS_INBOUND, spec)
    stored = fake.raw_client(spec.email)
    assert stored is not None
    assert stored["password"] == spec.password
    assert not stored.get("flow") and not has_vless_id(stored)
    assert (stored["enable"], stored["limitIp"], stored["tgId"]) == (True, 0, 42)
    assert account.uuid == spec.password  # an SS account is identified by its key
    read = await backend.get_account(SS_INBOUND, spec.email)
    assert read is not None and read.uuid == spec.password
    assert [a.uuid for a in await backend.list_accounts(SS_INBOUND)] == [spec.password]
    assert [a.uuid for a in (await backend.list_accounts_by_target([SS_INBOUND]))[SS_INBOUND]] == [spec.password]


@BOTH
async def test_unchanged_client_is_not_rewritten(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    await backend.upsert_account(SS_INBOUND, ss_spec())
    fake.calls.clear()
    await backend.upsert_account(SS_INBOUND, ss_spec())
    assert posts(fake, "update") == [] and posts(fake, "add") == []


@BOTH
async def test_expiry_ban_and_rotation_update_the_client(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    spec = ss_spec()
    await backend.upsert_account(SS_INBOUND, spec)
    update = "updateClient" if generation == "legacy" else "clients/update"

    await backend.upsert_account(SS_INBOUND, ss_spec(enabled=False))  # expired / banned
    stored = fake.raw_client(spec.email)
    assert stored is not None and stored["enable"] is False and stored["password"] == spec.password
    assert len(posts(fake, update)) == 1

    rotated = shadowsocks.derive_client_key("aaaaaaaa-8d1f-4b6c-9a51-3e0e2c4d5f60", 9, METHOD)
    await backend.upsert_account(SS_INBOUND, ss_spec(password=rotated))
    stored = fake.raw_client(spec.email)
    assert stored is not None and stored["password"] == rotated and stored["enable"] is True
    assert len(posts(fake, update)) == 2
    assert not has_vless_id(stored) and not stored.get("flow")


@BOTH
async def test_legacy_panels_address_the_client_by_email(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    await backend.upsert_account(SS_INBOUND, ss_spec())
    await backend.upsert_account(SS_INBOUND, ss_spec(enabled=False))
    await backend.upsert_account(SS_INBOUND, ss_spec(email="tg43-9", password=shadowsocks.generate_key(METHOD)))
    await backend.delete_account(SS_INBOUND, "tg42-9", UUID)
    paths = [path for method, path, _ in fake.calls if method == "POST"]
    if generation == "legacy":
        assert "/panel/api/inbounds/updateClient/tg42-9" in paths
        assert f"/panel/api/inbounds/{SS_INBOUND}/delClient/tg42-9" in paths
    else:
        assert "/panel/api/clients/update/tg42-9" in paths and "/panel/api/clients/del/tg42-9" in paths
    assert fake.raw_client("tg42-9") is None and fake.raw_client("tg43-9") is not None


@BOTH
async def test_deleting_the_last_client_works_and_a_missing_one_is_fine(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    await backend.upsert_account(SS_INBOUND, ss_spec())
    await backend.delete_account(SS_INBOUND, "tg42-9", UUID)
    assert fake.raw_client("tg42-9") is None
    await backend.delete_account(SS_INBOUND, "tg42-9", UUID)  # already gone: success
    if generation == "legacy":
        # The placeholder that unblocked the deletion is a valid Shadowsocks client (a key, no id), and disabled.
        (placeholder,) = fake.inbounds[SS_INBOUND]["settings"]["clients"]
        assert placeholder["enable"] is False and not has_vless_id(placeholder) and not placeholder.get("flow")
        assert len(base64.b64decode(placeholder["password"], validate=True)) == 16
        assert await backend.list_accounts(SS_INBOUND) == []  # placeholders are not accounts


@BOTH
async def test_vless_clients_on_other_inbounds_are_unchanged(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    vless = AccountSpec(
        email="tg42-1",
        uuid=UUID,
        enabled=True,
        expires_at=EXPIRY,
        device_limit=0,
        traffic_limit_bytes=0,
        tg_id=42,
        sub_id="abcdefgh12345679",
        flow="xtls-rprx-vision",
    )
    account = await backend.upsert_account(1, vless)
    assert account.uuid == UUID
    stored = fake.raw_client("tg42-1")
    assert stored is not None and stored["flow"] == "xtls-rprx-vision" and "password" not in stored


@BOTH
async def test_create_shadowsocks_inbound(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    info = await backend.create_shadowsocks_inbound(8443, "2022-blake3-aes-256-gcm", remark="ss-main")
    assert info.protocol == "shadowsocks" and info.port == 8443 and info.remote_id == SS_INBOUND + 1
    assert info.params[shadowsocks.PARAM_METHOD] == "2022-blake3-aes-256-gcm"
    assert shadowsocks.PARAM_UNSUPPORTED not in info.params
    assert info.params[shadowsocks.PARAM_PASSWORD] == fake.inbounds[info.remote_id]["settings"]["password"]
    listed = {i.remote_id: i for i in await backend.list_inbounds()}
    assert listed[SS_INBOUND].params[shadowsocks.PARAM_METHOD] == METHOD
    with pytest.raises(ValueError):
        await backend.create_shadowsocks_inbound(8444, "2022-blake3-chacha20-poly1305")
