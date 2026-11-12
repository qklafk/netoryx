"""Hiddify Manager v2 adapter against an in-process HTTP server."""

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
from app.services.panels.hiddify import HiddifyBackend


@dataclass
class FakeHiddify:
    users: dict[str, dict[str, Any]] = field(default_factory=dict)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    def user(self, name: str = "tg_1_s1", **changes: Any) -> dict[str, Any]:
        row = {
            "uuid": str(uuid4()),
            "name": name,
            "enable": True,
            "usage_limit_GB": 3,
            "current_usage_GB": 0.5,
            "package_days": 30,
            "start_date": "2026-11-01",
            "mode": "no_reset",
        }
        row.update(changes)
        self.users[row["uuid"]] = row
        return row

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Hiddify-API-Key") != "api-key":
            return web.json_response({}, status=401)
        if request.path == "/admin/api/v2/panel/info/":
            return web.json_response({"version": "14.0.5"})
        if request.path == "/admin/api/v2/admin/user/":
            if request.method == "GET":
                return web.json_response(list(self.users.values()))
            body = await request.json()
            if any(row["uuid"] == body["uuid"] for row in self.users.values()):
                return web.json_response({}, status=400)
            return web.json_response(self.user(**body))
        if request.path.startswith("/admin/api/v2/admin/user/"):
            key = request.path.rstrip("/").split("/")[-1]
            row = self.users.get(key)
            if row is None:
                return web.json_response({}, status=404)
            if request.method == "PATCH":
                row.update(await request.json())
                return web.json_response(row)
            if request.method == "DELETE":
                del self.users[key]
                return web.json_response({"status": 200})
        return web.json_response({}, status=404)


@pytest.fixture
async def panel() -> tuple[FakeHiddify, str]:
    fake = FakeHiddify()
    server = TestServer(fake.app())
    await server.start_server()
    try:
        yield fake, str(server.make_url("admin")).rstrip("/")
    finally:
        await server.close()


def spec(**changes: Any) -> AccountSpec:
    value = AccountSpec(
        email="tg_1_s1",
        uuid=str(uuid4()),
        enabled=True,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        device_limit=0,
        traffic_limit_bytes=3 * 1024**3,
        tg_id=1,
        sub_id="0123456789abcdef",
        comment="managed",
    )
    return replace(value, **changes)


async def test_lifecycle_preserves_identity_and_usage(panel: tuple[FakeHiddify, str]) -> None:
    fake, url = panel
    backend = HiddifyBackend(url, "api-key")
    try:
        assert backend.kind == PanelKind.HIDDIFY
        assert await backend.ping() == "14.0.5"
        created = await backend.upsert_account(None, spec())
        assert created.email == "tg_1_s1" and created.traffic_limit_bytes == 3 * 1024**3
        assert len(await backend.list_accounts(None)) == 1
        changed = spec(uuid=created.uuid, enabled=False, traffic_limit_bytes=0)
        updated = await backend.upsert_account(None, changed)
        assert not updated.enabled and updated.traffic_limit_bytes == 0
        fake.users[created.uuid]["current_usage_GB"] = 1.25
        assert await backend.get_used_bytes(None, created.email) == round(1.25 * 1024**3)
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


async def test_invalid_key_and_url(panel: tuple[FakeHiddify, str]) -> None:
    _, url = panel
    backend = HiddifyBackend(url, "wrong")
    try:
        with pytest.raises(PanelAuthError):
            await backend.ping()
    finally:
        await backend.close()
    with pytest.raises(ValueError):
        HiddifyBackend("ftp://panel.example", "key")
