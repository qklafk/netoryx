"""PasarGuard adapter against an in-process HTTP server."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.db.models import PanelKind
from app.services.panels.base import AccountSpec, PanelAuthError, PanelError, PanelUnsupportedError
from app.services.panels.pasarguard import PasarGuardBackend


@dataclass
class FakePasarGuard:
    users: dict[str, dict[str, Any]] = field(default_factory=dict)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    def user(self, username: str, **changes: Any) -> dict[str, Any]:
        row = {
            "id": len(self.users) + 1,
            "username": username,
            "status": "active",
            "expire": "2026-12-01T00:00:00+00:00",
            "data_limit": 3 * 1024**3,
            "used_traffic": 512,
            "hwid_limit": 2,
            # relative, like a panel without a public subscription prefix
            "subscription_url": f"/sub/tok-{username}",
            "proxy_settings": {"vless": {"id": str(uuid4())}},
        }
        row.update(changes)
        self.users[username] = row
        return row

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.path.startswith("/sub/tok-") and request.path.endswith("/links"):
            # the public subscription in the "links" format needs no API key
            username = request.path.removeprefix("/sub/tok-").removesuffix("/links")
            row = self.users.get(username)
            if row is None:
                return web.Response(status=404)
            uid = row["proxy_settings"]["vless"]["id"]
            return web.Response(text=f"vless://{uid}@203.0.113.5:443?security=reality#n\nnot a link\n")
        if request.headers.get("Authorization") != "ApiKey api-key":
            return web.json_response({}, status=401)
        if request.path == "/api/system/resources":
            return web.json_response({"version": "1.8.0"})
        if request.path == "/api/users" and request.method == "GET":
            return web.json_response({"users": list(self.users.values()), "total": len(self.users)})
        if request.path == "/api/user" and request.method == "POST":
            body = await request.json()
            if body["username"] in self.users:
                return web.json_response({}, status=409)
            return web.json_response(self.user(**body), status=201)
        if request.path.startswith("/api/user/"):
            parts = request.path.rstrip("/").split("/")
            username = parts[3]
            row = self.users.get(username)
            if row is None:
                return web.json_response({}, status=404)
            if len(parts) == 5 and parts[4] == "reset" and request.method == "POST":
                row["used_traffic"] = 0
                return web.json_response(row)
            if request.method == "GET":
                return web.json_response(row)
            if request.method == "PUT":
                row.update(await request.json())
                return web.json_response(row)
            if request.method == "DELETE":
                del self.users[username]
                return web.Response(status=204)
        return web.json_response({}, status=404)


@pytest.fixture
async def panel() -> tuple[FakePasarGuard, str]:
    fake = FakePasarGuard()
    server = TestServer(fake.app())
    await server.start_server()
    try:
        yield fake, str(server.make_url("/")).rstrip("/")
    finally:
        await server.close()


def spec(**changes: Any) -> AccountSpec:
    value = AccountSpec(
        email="tg_1_s1",
        uuid=str(uuid4()),
        enabled=True,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        device_limit=2,
        traffic_limit_bytes=3 * 1024**3,
        tg_id=1,
        sub_id="0123456789abcdef",
        comment="managed",
    )
    return replace(value, **changes)


async def test_lifecycle_preserves_identity_and_usage(panel: tuple[FakePasarGuard, str]) -> None:
    fake, url = panel
    backend = PasarGuardBackend(url, "api-key")
    try:
        assert backend.kind == PanelKind.PASARGUARD
        assert await backend.ping() == "1.8.0"
        desired = spec()
        created = await backend.upsert_account(None, desired)
        assert created.email == "tg_1_s1" and created.traffic_limit_bytes == 3 * 1024**3
        assert created.uuid == desired.uuid
        assert len(await backend.list_accounts(None)) == 1
        changed = spec(uuid=created.uuid, enabled=False, traffic_limit_bytes=0, device_limit=4)
        updated = await backend.upsert_account(None, changed)
        assert not updated.enabled and updated.traffic_limit_bytes == 0 and updated.device_limit == 4
        fake.users[created.email]["used_traffic"] = 1234
        assert await backend.get_used_bytes(None, created.email) == 1234
        assert await backend.get_links(created) == [f"vless://{created.uuid}@203.0.113.5:443?security=reality#n"]
        await backend.reset_traffic(None, created.email)
        assert await backend.get_used_bytes(None, created.email) == 0
        with pytest.raises(PanelError, match="identity changed"):
            await backend.delete_account(None, created.email, str(uuid4()))
        await backend.delete_account(None, created.email, created.uuid)
        assert await backend.get_account(None, created.email) is None
        with pytest.raises(PanelUnsupportedError):
            await backend.reset_devices(created)
    finally:
        await backend.close()


async def test_invalid_key_and_url(panel: tuple[FakePasarGuard, str]) -> None:
    _, url = panel
    backend = PasarGuardBackend(url, "wrong")
    try:
        with pytest.raises(PanelAuthError):
            await backend.ping()
    finally:
        await backend.close()
    with pytest.raises(ValueError):
        PasarGuardBackend("ftp://panel.example", "key")
