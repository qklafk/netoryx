"""Web admin API of invitations and members: permissions, CSRF, audit with the client IP."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import Admin, AdminAuditLog, AdminRole, Invite, Subscription
from app.db.session import SessionFactory
from app.modules.invites import module as invites_module
from app.modules.members import module as members_module
from app.services import provisioning
from app.utils.time import utcnow
from app.web import auth
from app.web.app import create_web_app
from tests.fakes import FakeCapablePanel
from tests.test_mod_personal_support import OWNER, ModCtx, make_ctx
from tests.test_subs_support import add_sub, add_user, add_xui_panel
from tests.test_web_support import AsgiClient

MEMBER = 8001
VIEWER = 8002
IP = "203.0.113.5"


class Api:
    def __init__(self, ctx: ModCtx, client: AsgiClient) -> None:
        self.ctx = ctx
        self.client = client
        self.csrf = ""

    async def login(self, tg_id: int = OWNER) -> Api:
        async with self.ctx.session_factory.begin() as session:
            token, _ = await auth.create_session(session, self.ctx.settings, tg_id, ip=IP, user_agent="t")
        self.client.cookies = {f"{auth.SECURE_PREFIX}{auth.SESSION_COOKIE}": token}
        self.csrf = auth.csrf_token_for(token)
        return self

    async def get(self, url: str) -> Any:
        return await self.client.get(url)

    async def post(self, url: str, json: Any = None, *, csrf: bool = True) -> Any:
        headers = {auth.CSRF_HEADER: self.csrf} if csrf else {}
        return await self.client.post(url, json=json if json is not None else {}, headers=headers)

    async def delete(self, url: str) -> Any:
        return await self.client.request("DELETE", url, headers={auth.CSRF_HEADER: self.csrf})


@pytest.fixture
async def api(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Api]:
    ctx, _ = make_ctx(session_factory, tmp_path, [invites_module, members_module], app_mode="personal")
    async with session_factory.begin() as session:
        panel, _inbounds = await add_xui_panel(session)
        await add_user(session, OWNER)
        await add_user(session, VIEWER)
        session.add(Admin(tg_id=VIEWER, role=AdminRole.VIEWER))
        await add_user(session, MEMBER, first_name="Оля")
        sub = await add_sub(session, MEMBER, expires_at=(utcnow() + timedelta(days=5)).replace(microsecond=0))
        await provisioning.ensure_provisions(session, sub)
    ctx.panels.panels[panel.id] = FakeCapablePanel()
    app = create_web_app(ctx, [invites_module, members_module])  # type: ignore[arg-type]
    yield Api(ctx, AsgiClient(app, client_ip=IP, host="bot.example.com"))


async def test_invites_api(api: Api, session_factory: SessionFactory) -> None:
    assert (await api.get("/api/admin/invites")).status == 401
    await api.login()
    created = await api.post("/api/admin/invites", {"days": 30, "device_limit": 2, "max_uses": 0, "note": "Мама"})
    assert created.status == 201
    body = created.json()
    assert body["state"] == "active" and body["link"].startswith("https://t.me/test_bot?start=inv_")
    assert (await api.post("/api/admin/invites", {"code": "FAMILY"})).status == 201
    assert (await api.post("/api/admin/invites", {"code": "family"})).status == 409
    assert (await api.post("/api/admin/invites", {"days": 0})).status == 422
    assert (await api.post("/api/admin/invites", {"days": 30}, csrf=False)).status == 403

    page = (await api.get("/api/admin/invites?limit=1")).json()
    assert page["total"] == 2 and len(page["items"]) == 1
    invite_id = body["id"]
    assert (await api.post(f"/api/admin/invites/{invite_id}/revoke")).json() == {"ok": True}
    assert (await api.post("/api/admin/invites/999/revoke")).status == 404
    assert (await api.get(f"/api/admin/invites/{invite_id}/users")).json() == []
    active = (await api.get("/api/admin/invites?active_only=true")).json()
    assert [i["code"] for i in active["items"]] == ["FAMILY"]
    async with session_factory() as session:
        audits = list(await session.execute(select(AdminAuditLog.action, AdminAuditLog.source, AdminAuditLog.ip)))
        invite = await session.get(Invite, invite_id)
    assert ("invite.revoke", "web", IP) in [tuple(a) for a in audits] and invite is not None and not invite.is_active


async def test_viewers_cannot_manage(api: Api) -> None:
    await api.login(VIEWER)
    assert (await api.get("/api/admin/invites")).status == 403
    assert (await api.get("/api/admin/members")).status == 403


async def test_members_api(api: Api, session_factory: SessionFactory) -> None:
    await api.login()
    page = (await api.get("/api/admin/members")).json()
    assert page["total"] == 1 and page["items"][0]["first_name"] == "Оля"
    member = (await api.post(f"/api/admin/members/{MEMBER}/extend", {"days": 30})).json()
    assert member["state"] == "active" and not member["unlimited"]
    member = (await api.post(f"/api/admin/members/{MEMBER}/extend", {"days": None})).json()
    assert member["unlimited"]
    assert (await api.post(f"/api/admin/members/{MEMBER}/pause")).json()["state"] == "paused"
    assert (await api.post(f"/api/admin/members/{MEMBER}/resume")).json()["state"] == "active"
    limited = (await api.post(f"/api/admin/members/{MEMBER}/limits", {"device_limit": 5})).json()
    assert limited["device_limit"] == 5 and limited["traffic_limit_gb"] == 0
    assert (await api.post(f"/api/admin/members/{MEMBER}/reset-devices")).json() == {"reset": 1, "failed": 0}
    assert (await api.post(f"/api/admin/members/{MEMBER}/rotate-link")).status == 200
    assert (await api.get("/api/admin/members/424242")).status == 404
    assert (await api.post("/api/admin/members/424242/extend", {"days": 1})).status == 404

    removed = await api.delete(f"/api/admin/members/{MEMBER}")
    assert removed.json() == {"complete": True}
    assert (await api.get(f"/api/admin/members/{MEMBER}")).status == 404
    async with session_factory() as session:
        assert await session.scalar(select(Subscription).where(Subscription.user_id == MEMBER)) is None
        rows = list(await session.execute(select(AdminAuditLog.action, AdminAuditLog.source)))
    actions = {action for action, source in rows if source == "web"}
    assert {"member.unlimited", "member.limits", "member.rotate_link", "member.remove"} <= actions


async def test_members_api_is_hidden_in_shop_mode(api: Api) -> None:
    await api.login()
    api.ctx.current = api.ctx.current.model_copy(update={"app_mode": "shop"})
    assert (await api.get("/api/admin/members")).status == 404
    assert (await api.get("/api/admin/invites")).status == 200
