"""Web security regressions: owner-only web-app menu buttons, public API throttling and banned keys."""

from __future__ import annotations

from app.db.models import AdminRole, User
from app.services import public_api
from app.web.security import DEFAULT_RATE_RULES
from tests.test_mod_webapi_support import Api, api, as_role, ctx
from tests.test_web_support import AsgiClient, make_ctx, web_app

__all__ = ["api", "ctx"]


async def test_admin_cannot_create_or_change_webapp_menu_buttons(api: Api) -> None:
    admin = await as_role(api, AdminRole.ADMIN, 3101)
    body = {"kind": "webapp", "title": "App", "payload": "https://evil.example/app"}
    assert (await admin.post("/menu-buttons", json=body)).status_code == 403
    owned = (await api.post("/menu-buttons", json=body)).json()
    assert (
        await admin.patch(f"/menu-buttons/{owned['id']}", json={"payload": "https://other.example"})
    ).status_code == 403
    plain = (
        await admin.post("/menu-buttons", json={"kind": "url", "title": "U", "payload": "https://a.example"})
    ).json()
    assert (await admin.patch(f"/menu-buttons/{plain['id']}", json={"kind": "webapp"})).status_code == 403
    assert (await admin.patch(f"/menu-buttons/{plain['id']}", json={"title": "V"})).status_code == 200


async def test_public_api_is_rate_limited_per_network() -> None:
    assert any(rule.prefix == "/api/public/" for rule in DEFAULT_RATE_RULES)


async def test_public_api_key_of_a_banned_user_is_refused(session_factory, settings) -> None:
    async with session_factory.begin() as session:
        session.add(User(tg_id=42, referral_code="R", sub_token="S", username="alice", is_banned=True))
        token, _ = await public_api.create_key(session, 42, "test")
    client_ = AsgiClient(web_app(make_ctx(settings, session_factory)))
    response = await client_.get("/api/public/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status == 401
