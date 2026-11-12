"""Web admin API of the external blacklist."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.core.modules import ModuleRegistry
from app.db.models import AdminRole, User
from app.modules import admin_api
from app.modules import blacklist as blacklist_module
from app.modules.admin_api.deps import drain_background
from app.modules.blacklist import service as blacklist_service
from app.services import blacklist
from app.web.app import create_web_app
from tests.test_mod_webapi_support import Api, add_user, as_role, build_ctx


@pytest.fixture
async def bl_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    blacklist.CACHE.clear()
    monkeypatch.setattr(blacklist_service, "notify", lambda *args, **kwargs: True)
    ctx, engine = await build_ctx(
        tmp_path, blacklist_enabled=True, blacklist_action="block", blacklist_manual_ids=[11, 12]
    )
    ctx.modules = ModuleRegistry([admin_api.module, blacklist_module.module])
    app = create_web_app(ctx)  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app, client=("203.0.113.5", 50000))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield await Api(ctx, client).login()
    finally:
        await drain_background()
        await engine.dispose()
        blacklist.CACHE.clear()


async def test_status_and_refresh_now(bl_api: Api) -> None:
    await add_user(bl_api.ctx, 11)
    status = await bl_api.get("/blacklist/status")
    assert status.status_code == 200
    body = status.json()
    assert body["enabled"] is True and body["manual_ids"] == 2 and body["sources"] == []

    refreshed = await bl_api.post("/blacklist/refresh")
    assert refreshed.status_code == 200 and refreshed.json()["banned"] == 1
    async with bl_api.ctx.session_factory() as s:
        user = await s.get(User, 11)
        assert user is not None and user.is_banned
    assert (await bl_api.get("/blacklist/status")).json()["banned_by_list"] == 1
    assert "provisioning.reconcile" in bl_api.ctx.scheduler.ran  # type: ignore[union-attr]


async def test_refresh_needs_settings_permission(bl_api: Api) -> None:
    support = await as_role(bl_api, AdminRole.SUPPORT)
    assert (await support.get("/blacklist/status")).status_code == 200
    assert (await support.post("/blacklist/refresh")).status_code == 403
