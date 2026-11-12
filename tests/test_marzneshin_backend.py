"""Marzneshin adapter against an in-process HTTP server."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.db.models import PanelKind
from app.services.panels.base import AccountSpec, PanelAuthError, PanelError, PanelUnsupportedError
from app.services.panels.marzneshin import MarzneshinBackend


@dataclass
class FakeMarzneshin:
    users: dict[str, dict[str, Any]] = field(default_factory=dict)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    def _user(self, username: str, **changes: Any) -> dict[str, Any]:
        row = {
            "id": len(self.users) + 1,
            "username": username,
            "key": str(uuid4()).replace("-", ""),
            "enabled": True,
            "expire_date": "2027-01-01T00:00:00+00:00",
            "data_limit": 1024,
            "used_traffic": 12,
            "subscription_url": f"/sub/{username}/subkey",  # relative, as without a public prefix
        }
        row.update(changes)
        self.users[username] = row
        return row

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.path.startswith("/sub/") and request.path.endswith("/subkey/links"):
            row = self.users.get(request.path.split("/")[2])
            if row is None:
                return web.Response(status=404)
            return web.Response(text=f"vless://{UUID(row['key'])}@203.0.113.5:443?security=reality#n\nignored\n")
        if request.path == "/api/admins/token":
            body = await request.post()
            return web.json_response(
                {"access_token": "good"} if body.get("password") == "pass" else {},
                status=200 if body.get("password") == "pass" else 401,
            )
        if request.headers.get("Authorization") != "Bearer good":
            return web.json_response({}, status=401)
        if request.path == "/api/admins/current":
            return web.json_response({"username": "admin"})
        if request.path == "/api/users" and request.method == "GET":
            return web.json_response({"items": list(self.users.values()), "pages": 1})
        if request.path == "/api/users" and request.method == "POST":
            body = await request.json()
            return web.json_response(self._user(**body))
        parts = request.path.split("/")
        if len(parts) >= 4 and parts[1:3] == ["api", "users"]:
            username = parts[3]
            row = self.users.get(username)
            if row is None:
                return web.json_response({}, status=404)
            if len(parts) == 5 and parts[4] == "reset":
                row["used_traffic"] = 0
                return web.json_response(row)
            if len(parts) == 5 and parts[4] in {"enable", "disable"}:
                row["enabled"] = parts[4] == "enable"
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
async def panel() -> tuple[FakeMarzneshin, str]:
    fake, server = FakeMarzneshin(), TestServer(FakeMarzneshin().app())
    server = TestServer(fake.app())
    await server.start_server()
    try:
        yield fake, str(server.make_url("/")).rstrip("/")
    finally:
        await server.close()


def spec(**changes: Any) -> AccountSpec:
    base = AccountSpec(
        "tg_1_s1", str(uuid4()), True, datetime.now(UTC) + timedelta(days=30), 0, 1024, 1, "sub", "managed"
    )
    return replace(base, **changes)


async def test_lifecycle(panel: tuple[FakeMarzneshin, str]) -> None:
    fake, url = panel
    backend = MarzneshinBackend(url, username="admin", password="pass", service_ids=[7])
    try:
        desired = spec()
        assert backend.kind == PanelKind.MARZNESHIN and await backend.ping() == "connected"
        account = await backend.upsert_account(None, desired)
        assert account.uuid == desired.uuid and fake.users[account.email]["service_ids"] == [7]
        changed = await backend.upsert_account(None, replace(desired, enabled=False, traffic_limit_bytes=0))
        assert not changed.enabled and changed.traffic_limit_bytes == 0
        await backend.reset_traffic(None, account.email)
        assert await backend.get_used_bytes(None, account.email) == 0
        assert await backend.get_links(account) == [f"vless://{account.uuid}@203.0.113.5:443?security=reality#n"]
        with pytest.raises(PanelError, match="identity changed"):
            await backend.delete_account(None, account.email, str(uuid4()))
        await backend.delete_account(None, account.email, account.uuid)
        assert await backend.get_account(None, account.email) is None
        with pytest.raises(PanelUnsupportedError):
            await backend.reset_devices(account)
    finally:
        await backend.close()


async def test_bad_credentials(panel: tuple[FakeMarzneshin, str]) -> None:
    _, url = panel
    backend = MarzneshinBackend(url, username="admin", password="wrong")
    try:
        with pytest.raises(PanelAuthError):
            await backend.ping()
    finally:
        await backend.close()
