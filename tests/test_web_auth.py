from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.config import Settings
from app.core.modules import FeatureModule
from app.core.permissions import ROLE_PERMISSIONS, Permission
from app.db.models import Admin, AdminAuditLog, AdminRole, AdminSession, KeyValue
from app.db.session import SessionFactory
from app.services import kv
from app.web import auth
from tests.conftest import make_settings
from tests.test_web_support import AsgiClient, FakeCtx, make_ctx, web_app

OWNER = 1000
STAFF = 2000
HOST_COOKIE = "__Host-vpnbot_session"
HOST_CSRF = "__Host-vpnbot_csrf"
ORIGIN = "https://bot.example.com"


def _admin_module() -> FeatureModule:
    router = APIRouter()

    @router.get("/view")
    async def view(principal: auth.CurrentAdmin) -> dict[str, int]:
        return {"ok": 1}

    @router.get("/settings")
    async def settings_page(
        principal: Annotated[auth.AdminPrincipal, Depends(auth.admin_user(Permission.SETTINGS))],
    ) -> dict[str, Any]:
        return {"tg_id": principal.tg_id, "role": principal.role}

    @router.post("/change")
    async def change(
        principal: Annotated[auth.AdminPrincipal, Depends(auth.admin_user(Permission.USERS_VIEW))],
    ) -> dict[str, int]:
        return {"changed": principal.tg_id}

    return FeatureModule(name="probe", title_ru="p", title_en="p", admin_api_routers=[router])


@pytest.fixture
def ctx(session_factory: SessionFactory) -> FakeCtx:
    return make_ctx(make_settings(), session_factory, _admin_module())


@pytest.fixture
def client(ctx: FakeCtx) -> AsgiClient:
    return AsgiClient(web_app(ctx))


async def _login(client: AsgiClient, ctx: FakeCtx, admin_id: int = OWNER, **kw: Any) -> Any:
    token = await auth.create_login_token(ctx.session_factory, admin_id)
    return await client.post("/api/admin/auth/magic", json={"token": token}, **kw)


async def _add_admin(session_factory: SessionFactory, tg_id: int, role: AdminRole) -> None:
    async with session_factory.begin() as s:
        s.add(Admin(tg_id=tg_id, role=role))


# ---------------------------------------------------------------------------------------------
# Login tokens
# ---------------------------------------------------------------------------------------------
async def test_login_token_is_single_use_and_hashed(session_factory: SessionFactory) -> None:
    token = await auth.create_login_token(session_factory, OWNER)
    async with session_factory() as s:
        keys = (await s.scalars(select(KeyValue.key))).all()
    assert len(keys) == 1 and token not in keys[0] and len(keys[0]) <= kv.MAX_KEY_LENGTH
    async with session_factory.begin() as s:
        assert await auth.redeem_login_token(s, token) == OWNER
    async with session_factory.begin() as s:
        assert await auth.redeem_login_token(s, token) is None
        assert await auth.redeem_login_token(s, "short") is None
        assert await auth.redeem_login_token(s, "x" * 40) is None


async def test_login_token_expires(session_factory: SessionFactory) -> None:
    past = datetime.now(UTC) - timedelta(minutes=6)
    token = await auth.create_login_token(session_factory, OWNER, now=past)
    async with session_factory.begin() as s:
        assert await auth.redeem_login_token(s, token) is None


async def test_purge_expired_login_tokens(session_factory: SessionFactory) -> None:
    await auth.create_login_token(session_factory, OWNER, now=datetime.now(UTC) - timedelta(hours=1))
    fresh = await auth.create_login_token(session_factory, OWNER)
    async with session_factory.begin() as s:
        await kv.set(s, "weblogin:broken", "not json")
        await kv.set(s, "maintenance", "0")
    assert await auth.purge_expired_login_tokens(session_factory) == 2
    async with session_factory.begin() as s:
        assert await kv.get(s, "maintenance") == "0"
        assert await auth.redeem_login_token(s, fresh) == OWNER


async def test_create_login_link(session_factory: SessionFactory, settings: Settings) -> None:
    link = await auth.create_login_link(session_factory, settings, OWNER)
    assert link.startswith("https://bot.example.com/admin/login#token=")
    token = link.split("#token=", 1)[1]
    async with session_factory.begin() as s:
        assert await auth.redeem_login_token(s, token) == OWNER
    relative = await auth.create_login_link(session_factory, make_settings(public_base_url=None), OWNER)
    assert relative.startswith("/admin/login#token=")


def test_login_url(settings: Settings) -> None:
    assert auth.login_url(settings, "tok") == "https://bot.example.com/admin/login#token=tok"
    assert auth.login_url(make_settings(public_base_url=None), "tok") is None


# ---------------------------------------------------------------------------------------------
# Magic login -> session
# ---------------------------------------------------------------------------------------------
async def test_magic_login_sets_secure_cookies(client: AsgiClient, ctx: FakeCtx) -> None:
    resp = await _login(client, ctx)
    assert resp.status == 200, resp.text
    body = resp.json()
    assert body["tg_id"] == OWNER and body["role"] == "owner"
    assert set(body["permissions"]) == {str(p) for p in Permission}
    cookies = resp.set_cookies()
    session_cookie = cookies[HOST_COOKIE].lower()
    for attr in ("httponly", "secure", "samesite=strict", "path=/"):
        assert attr in session_cookie
    csrf_cookie = cookies[HOST_CSRF].lower()
    assert "httponly" not in csrf_cookie and "secure" in csrf_cookie
    assert client.cookies[HOST_CSRF] == body["csrf_token"]
    async with ctx.session_factory() as s:
        row = await s.scalar(select(AdminSession))
        audit = await s.scalar(select(AdminAuditLog))
    assert row is not None and row.token_hash != client.cookies[HOST_COOKIE] and row.ip == "203.0.113.5"
    assert audit is not None and audit.action == "web.login" and audit.source == "web"


async def test_magic_login_rejects_reused_and_bad_tokens(client: AsgiClient, ctx: FakeCtx) -> None:
    token = await auth.create_login_token(ctx.session_factory, OWNER)
    assert (await client.post("/api/admin/auth/magic", json={"token": token})).status == 200
    reused = await client.post("/api/admin/auth/magic", json={"token": token})
    assert reused.status == 401 and reused.json()["error"] == "invalid_token"
    assert (await client.post("/api/admin/auth/magic", json={"token": "nope"})).status == 401
    assert (await client.post("/api/admin/auth/magic", json={})).status == 422


async def test_magic_login_requires_admin_role(client: AsgiClient, ctx: FakeCtx) -> None:
    resp = await _login(client, ctx, admin_id=555)
    assert resp.status == 403
    assert not client.cookies


async def test_magic_login_brute_force_protection(client: AsgiClient, ctx: FakeCtx) -> None:
    for _ in range(auth.LOGIN_ATTEMPTS_PER_IP):
        assert (await client.post("/api/admin/auth/magic", json={"token": "x" * 43})).status == 401
    blocked = await client.post("/api/admin/auth/magic", json={"token": "x" * 43})
    assert blocked.status == 429 and "retry-after" in blocked.headers
    assert (await _login(client, ctx)).status == 429  # even a valid token from that IP
    assert (await _login(client, ctx, client_ip="198.51.100.20")).status == 200


async def test_bad_tokens_from_many_addresses_do_not_block_valid_logins(client: AsgiClient, ctx: FakeCtx) -> None:
    # Login tokens are unguessable, so no shared counter may let anonymous clients lock the owner out.
    for i in range(320):
        bad = await client.post(
            "/api/admin/auth/magic", json={"token": "x" * 43}, client_ip=f"198.18.{i // 256}.{i % 256}"
        )
        assert bad.status == 401
    assert (await client.post("/api/admin/auth/magic", json={"token": "x" * 43}, client_ip="192.0.2.1")).status == 401
    assert (await _login(client, ctx, client_ip="192.0.2.2")).status == 200


async def test_login_attempts_are_counted_per_ipv6_network(client: AsgiClient, ctx: FakeCtx) -> None:
    for i in range(auth.LOGIN_ATTEMPTS_PER_IP):
        resp = await client.post(
            "/api/admin/auth/magic", json={"token": "x" * 43}, client_ip=f"2001:db8:1:2::{i + 1:x}"
        )
        assert resp.status == 401
    blocked = await client.post("/api/admin/auth/magic", json={"token": "x" * 43}, client_ip="2001:db8:1:2::ffff")
    assert blocked.status == 429
    assert (await _login(client, ctx, client_ip="2001:db8:1:3::1")).status == 200


async def test_magic_login_rejects_cross_origin(client: AsgiClient, ctx: FakeCtx) -> None:
    resp = await _login(client, ctx, headers={"origin": "https://evil.example"})
    assert resp.status == 403 and resp.json()["error"] == "csrf_failed"
    assert (await _login(client, ctx, headers={"origin": ORIGIN})).status == 200


async def test_web_admin_disabled(client: AsgiClient, ctx: FakeCtx) -> None:
    assert (await _login(client, ctx)).status == 200
    ctx.settings = make_settings(web_admin_enabled=False)
    assert (await client.get("/api/admin/auth/me")).status == 404
    assert (await _login(client, ctx)).status == 404


async def test_dev_mode_localhost_uses_plain_cookies(session_factory: SessionFactory) -> None:
    ctx = make_ctx(make_settings(dev_mode=True), session_factory)
    client = AsgiClient(web_app(ctx), host="localhost:8080")
    resp = await _login(client, ctx)
    assert resp.status == 200
    cookies = resp.set_cookies()
    assert "vpnbot_session" in cookies and "secure" not in cookies["vpnbot_session"].lower()
    assert (await client.get("/api/admin/auth/me")).status == 200
    # Without dev mode localhost still gets Secure, prefixed cookies.
    ctx2 = make_ctx(make_settings(), session_factory)
    client2 = AsgiClient(web_app(ctx2), host="localhost:8080")
    assert HOST_COOKIE in (await _login(client2, ctx2)).set_cookies()


# ---------------------------------------------------------------------------------------------
# Session use
# ---------------------------------------------------------------------------------------------
async def test_me_and_logout_with_csrf(client: AsgiClient, ctx: FakeCtx) -> None:
    assert (await client.get("/api/admin/auth/me")).status == 401
    csrf = (await _login(client, ctx)).json()["csrf_token"]
    me = await client.get("/api/admin/auth/me")
    assert me.status == 200 and me.json()["csrf_token"] == csrf

    no_csrf = await client.post("/api/admin/auth/logout")
    assert no_csrf.status == 403 and no_csrf.json()["error"] == "csrf_failed"
    wrong = await client.post("/api/admin/auth/logout", headers={"x-csrf-token": "x" * 43})
    assert wrong.status == 403

    out = await client.post("/api/admin/auth/logout", headers={"x-csrf-token": csrf})
    assert out.status == 200
    assert HOST_COOKIE not in client.cookies
    async with ctx.session_factory() as s:
        row = await s.scalar(select(AdminSession))
    assert row is not None and row.revoked


async def test_revoked_session_cookie_is_useless(client: AsgiClient, ctx: FakeCtx) -> None:
    await _login(client, ctx)
    saved = dict(client.cookies)
    async with ctx.session_factory.begin() as s:
        row = await s.scalar(select(AdminSession))
        assert row is not None
        assert await auth.revoke_session(s, row.id)
    client.cookies = saved
    resp = await client.get("/api/admin/auth/me")
    assert resp.status == 401 and resp.json()["error"] == "session_expired"


async def test_cookie_without_host_prefix_is_ignored_in_production(client: AsgiClient, ctx: FakeCtx) -> None:
    await _login(client, ctx)
    client.cookies = {"vpnbot_session": client.cookies[HOST_COOKIE]}
    assert (await client.get("/api/admin/auth/me")).status == 401


async def test_permissions_on_module_routes(client: AsgiClient, ctx: FakeCtx) -> None:
    viewer_lacks = Permission.SETTINGS
    assert viewer_lacks not in ROLE_PERMISSIONS[AdminRole.VIEWER]
    await _add_admin(ctx.session_factory, STAFF, AdminRole.VIEWER)

    assert (await client.get("/api/admin/probe/view")).status == 401
    csrf = (await _login(client, ctx, admin_id=STAFF)).json()["csrf_token"]
    assert (await client.get("/api/admin/probe/view")).status == 200
    denied = await client.get("/api/admin/probe/settings")
    assert denied.status == 403 and denied.json()["error"] == "forbidden"
    assert (await client.post("/api/admin/probe/change")).status == 403  # CSRF first
    changed = await client.post("/api/admin/probe/change", headers={"x-csrf-token": csrf})
    assert changed.json() == {"changed": STAFF}

    owner = AsgiClient(client.app)
    await _login(owner, ctx)
    assert (await owner.get("/api/admin/probe/settings")).json() == {"tg_id": OWNER, "role": "owner"}


async def test_removed_admin_loses_session(client: AsgiClient, ctx: FakeCtx) -> None:
    await _add_admin(ctx.session_factory, STAFF, AdminRole.SUPPORT)
    await _login(client, ctx, admin_id=STAFF)
    async with ctx.session_factory.begin() as s:
        admin = await s.get(Admin, STAFF)
        await s.delete(admin)
    assert (await client.get("/api/admin/auth/me")).status == 401
    async with ctx.session_factory() as s:
        row = await s.scalar(select(AdminSession))
    assert row is not None and row.revoked


async def test_role_change_applies_immediately(client: AsgiClient, ctx: FakeCtx) -> None:
    await _add_admin(ctx.session_factory, STAFF, AdminRole.VIEWER)
    await _login(client, ctx, admin_id=STAFF)
    assert (await client.get("/api/admin/probe/settings")).status == 403
    async with ctx.session_factory.begin() as s:
        admin = await s.get(Admin, STAFF)
        assert admin is not None
        admin.role = AdminRole.ADMIN
    assert (await client.get("/api/admin/probe/settings")).status == 200


async def test_disabled_module_admin_routes_are_404(client: AsgiClient, ctx: FakeCtx) -> None:
    await _login(client, ctx)
    ctx.settings = make_settings(disabled_modules="probe")
    assert (await client.get("/api/admin/probe/view")).status == 404


# ---------------------------------------------------------------------------------------------
# Sliding and absolute expiry, session management helpers
# ---------------------------------------------------------------------------------------------
async def test_sliding_expiry(session_factory: SessionFactory) -> None:
    settings = make_settings(web_session_hours=2)
    t0 = datetime.now(UTC)
    async with session_factory.begin() as s:
        token, row = await auth.create_session(s, settings, OWNER, ip="1.1.1.1", user_agent="ua", now=t0)
    assert row.expires_at == t0 + timedelta(hours=2)

    p = await auth.authenticate_session(session_factory, settings, token, ip="2.2.2.2", now=t0 + timedelta(seconds=10))
    assert p is not None and p.expires_at == t0 + timedelta(hours=2)  # touched recently: not persisted

    t1 = t0 + timedelta(hours=1, minutes=30)
    p = await auth.authenticate_session(session_factory, settings, token, ip="2.2.2.2", now=t1)
    assert p is not None and p.expires_at == t1 + timedelta(hours=2)
    async with session_factory() as s:
        stored = await s.get(AdminSession, row.id)
    assert stored is not None and stored.expires_at == t1 + timedelta(hours=2) and stored.ip == "2.2.2.2"

    late = t1 + timedelta(hours=2, seconds=1)
    assert await auth.authenticate_session(session_factory, settings, token, ip="2.2.2.2", now=late) is None


async def test_absolute_session_lifetime(session_factory: SessionFactory) -> None:
    settings = make_settings(web_session_hours=24)
    t0 = datetime.now(UTC) - auth.MAX_SESSION_AGE + timedelta(hours=1)
    async with session_factory.begin() as s:
        token, row = await auth.create_session(s, settings, OWNER, ip=None, user_agent=None, now=t0)
        row.expires_at = datetime.now(UTC) + timedelta(hours=5)  # kept alive by activity
        row.last_used_at = t0
    now = datetime.now(UTC)
    p = await auth.authenticate_session(session_factory, settings, token, ip="1.1.1.1", now=now)
    assert p is not None and p.expires_at <= t0 + auth.MAX_SESSION_AGE
    assert (
        await auth.authenticate_session(session_factory, settings, token, ip="1", now=now + timedelta(hours=2)) is None
    )


async def test_list_revoke_all_and_purge(session_factory: SessionFactory, settings: Settings) -> None:
    old = datetime.now(UTC) - timedelta(days=90)
    async with session_factory.begin() as s:
        await auth.create_session(s, settings, OWNER, ip=None, user_agent=None)
        await auth.create_session(s, settings, OWNER, ip=None, user_agent=None)
        await auth.create_session(s, settings, STAFF, ip=None, user_agent=None)
        await auth.create_session(s, settings, STAFF, ip=None, user_agent=None, now=old)
    async with session_factory.begin() as s:
        assert len(await auth.list_sessions(s)) == 3
        assert len(await auth.list_sessions(s, admin_id=STAFF)) == 1
        assert await auth.revoke_all(s, OWNER) == 2
        assert len(await auth.list_sessions(s)) == 1
        staff_session = (await auth.list_sessions(s, admin_id=STAFF))[0]
        assert not await auth.revoke_session(s, staff_session.id, admin_id=OWNER)
        assert await auth.revoke_session(s, staff_session.id, admin_id=STAFF)
    assert await auth.purge_sessions(session_factory) == 1


def test_csrf_token_is_bound_to_session() -> None:
    a, b = auth.csrf_token_for("token-a"), auth.csrf_token_for("token-b")
    assert a != b and len(a) == 43 and auth.csrf_token_for("token-a") == a
