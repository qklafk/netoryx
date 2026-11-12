"""The Marzban adapter preserves existing credentials and follows the panel's authenticated CRUD API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.db.models import PanelKind
from app.services.panels.base import AccountSpec, PanelAuthError, PanelError, PanelUnsupportedError
from app.services.panels.marzban import MarzbanBackend, parse_account


@dataclass
class FakeMarzban:
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[tuple[str, str, Any]] = field(default_factory=list)
    token: str = "token"
    logins: int = 0
    chunked: bool = False

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    def add(self, username: str = "existing_user", **values: Any) -> dict[str, Any]:
        user = {
            "username": username,
            "proxies": {"vless": {"id": str(uuid4()), "flow": "xtls-rprx-vision"}},
            "inbounds": {"vless": ["old-inbound"]},
            "status": "active",
            "expire": 1900000000,
            "data_limit": 3 * 1024**3,
            "used_traffic": 123456,
            "note": "Original operator note",
            "subscription_url": "https://original.example/sub/keep-this-token",
            "links": [],
        }
        user.update(values)
        self.users[username] = user
        return user

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.path == "/api/admin/token":
            login_data = await request.post()
            self.logins += 1
            if login_data.get("username") != "admin" or login_data.get("password") != "password":
                return web.json_response({}, status=401)
            return web.json_response({"access_token": self.token})
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return web.json_response({}, status=401)
        body = await request.json() if request.can_read_body else None
        self.requests.append((request.method, request.path, body))
        if request.path == "/api/system":
            return web.json_response(
                {"version": "0.8.4", "cpu_usage": 12.5, "cpu_cores": 2, "mem_total": 1000, "mem_used": 400}
            )
        if request.path == "/api/core":
            return web.json_response({"version": "25.1", "started": True})
        if request.path == "/api/core/restart":
            return web.json_response({})
        if request.path == "/api/users":
            offset, limit = int(request.query.get("offset", 0)), int(request.query.get("limit", 200))
            data = {"users": list(self.users.values())[offset : offset + limit], "total": len(self.users)}
            if self.chunked:
                response = web.StreamResponse(headers={"Content-Type": "application/json"})
                await response.prepare(request)
                encoded = json.dumps(data).encode()
                await response.write(encoded[:10])
                await response.write(encoded[10:])
                await response.write_eof()
                return response
            return web.json_response(data)
        if request.path == "/api/user" and request.method == "POST":
            assert isinstance(body, dict)
            if body["username"] in self.users:
                return web.json_response({}, status=409)
            return web.json_response(self.add(**body))
        if request.path.startswith("/api/user/"):
            username = request.path.split("/")[3]
            user = self.users.get(username)
            if user is None:
                return web.json_response({}, status=404)
            if request.path.endswith("/reset"):
                user["used_traffic"] = 0
                return web.json_response(user)
            if request.method == "DELETE":
                del self.users[username]
                return web.Response(status=204)
            if request.method == "PUT":
                assert isinstance(body, dict)
                user.update(body)
            return web.json_response(user)
        return web.json_response({}, status=404)


@pytest.fixture
async def panel() -> AsyncIterator[tuple[FakeMarzban, str]]:
    fake = FakeMarzban()
    server = TestServer(fake.app())
    await server.start_server()
    try:
        yield fake, str(server.make_url("/")).rstrip("/")
    finally:
        await server.close()


def spec(email: str = "tg_123_s5", **changes: Any) -> AccountSpec:
    base = AccountSpec(
        email=email,
        uuid=str(uuid4()),
        enabled=True,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        device_limit=0,
        traffic_limit_bytes=3 * 1024**3,
        tg_id=123,
        sub_id="abcdef0123456789",
    )
    return replace(base, **changes)


async def test_password_login_refresh_and_close(panel: tuple[FakeMarzban, str]) -> None:
    fake, url = panel
    backend = MarzbanBackend(url, username="admin", password="password")
    try:
        assert backend.kind == PanelKind.MARZBAN
        assert await backend.ping() == "0.8.4"
        fake.token = "new-token"
        assert await backend.ping() == "0.8.4"
        assert fake.logins == 2
    finally:
        await backend.close()
    assert backend._session is not None and backend._session.closed


async def test_static_bad_token_is_rejected(panel: tuple[FakeMarzban, str]) -> None:
    _, url = panel
    backend = MarzbanBackend(url, api_token="bad")
    try:
        with pytest.raises(PanelAuthError):
            await backend.ping()
    finally:
        await backend.close()


async def test_create_update_keeps_subscription_and_other_protocols(panel: tuple[FakeMarzban, str]) -> None:
    fake, url = panel
    raw = fake.add(
        proxies={"vless": {"id": str(uuid4()), "flow": "xtls-rprx-vision"}, "trojan": {"password": "retain"}}
    )
    backend = MarzbanBackend(url, api_token="token", inbound_tags=["new-inbound"])
    try:
        desired = spec("existing_user", uuid=raw["proxies"]["vless"]["id"])
        await backend.upsert_account(None, desired)
        assert raw["proxies"]["trojan"]["password"] == "retain"
        assert raw["proxies"]["vless"]["flow"] == "xtls-rprx-vision"
        assert raw["subscription_url"].endswith("keep-this-token")
        assert raw["inbounds"] == {"vless": ["old-inbound"]}
        assert raw["used_traffic"] == 123456 and raw["note"] == "Original operator note"
        new = spec()
        created = await backend.upsert_account(None, new)
        assert created.uuid == new.uuid
        assert fake.users[new.email]["inbounds"] == {"vless": ["new-inbound"]}
        await backend.upsert_account(None, new)
        assert len(fake.users) == 2
    finally:
        await backend.close()


async def test_disabled_create_reset_and_identity_safe_delete(panel: tuple[FakeMarzban, str]) -> None:
    fake, url = panel
    backend = MarzbanBackend(url, api_token="token")
    try:
        desired = spec(enabled=False)
        account = await backend.upsert_account(None, desired)
        assert not account.enabled
        fake.users[desired.email]["used_traffic"] = 500
        assert await backend.get_used_bytes(None, desired.email) == 500
        await backend.reset_traffic(None, desired.email)
        assert await backend.get_used_bytes(None, desired.email) == 0
        with pytest.raises(PanelError, match="identity changed"):
            await backend.delete_account(None, desired.email, str(uuid4()))
        await backend.delete_account(None, desired.email, desired.uuid)
        await backend.delete_account(None, desired.email, desired.uuid)
        assert await backend.get_account(None, desired.email) is None
        with pytest.raises(PanelUnsupportedError):
            await backend.reset_devices(account)
    finally:
        await backend.close()


async def test_paginated_chunked_listing_and_monitor(panel: tuple[FakeMarzban, str]) -> None:
    fake, url = panel
    for index in range(205):
        fake.add(f"existing_{index}")
    fake.add("trojan_only", proxies={"trojan": {"password": "secret"}})
    fake.chunked = True
    backend = MarzbanBackend(url, api_token="token")
    try:
        assert len(await backend.list_users()) == 206
        assert len(await backend.list_accounts(None)) == 205
        status = await backend.server_status()
        assert status.cpu_percent == 12.5 and status.core_running
        await backend.restart_core()
    finally:
        await backend.close()


async def test_links_only_valid_vless(panel: tuple[FakeMarzban, str]) -> None:
    fake, url = panel
    raw = fake.add(links=["vless://id@de.example:443#DE", "javascript:alert(1)", "trojan://pw@host:443"])
    backend = MarzbanBackend(url, api_token="token")
    try:
        assert await backend.get_links(parse_account(raw)) == ["vless://id@de.example:443#DE"]
    finally:
        await backend.close()


def test_config_and_zero_expiry() -> None:
    with pytest.raises(ValueError):
        MarzbanBackend("https://panel.example")
    with pytest.raises(ValueError):
        MarzbanBackend("https://admin:password@panel.example", api_token="token")
    assert parse_account({"username": "user", "expire": 0}).expires_at is None
