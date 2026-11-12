from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import func, select

from app.db.models import (
    AdminAuditLog,
    AdminRole,
    BalanceTransaction,
    Panel,
    PanelKind,
    Provision,
    ProvisionState,
    Purchase,
    PurchaseKind,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.modules.admin_api.deps import drain_background
from app.web.app import create_web_app
from tests.test_mod_webapi_support import (
    OWNER,
    Api,
    ApiCtx,
    add_plan,
    add_user,
    api,
    as_role,
    build_ctx,
    ctx,
)

__all__ = ["api", "ctx"]


async def _audit(ctx: ApiCtx, action: str) -> AdminAuditLog | None:
    async with ctx.session_factory() as s:
        return await s.scalar(
            select(AdminAuditLog).where(AdminAuditLog.action == action).order_by(AdminAuditLog.id.desc())
        )


async def _settle() -> None:
    await drain_background()


# ---------------------------------------------------------------------------------------------
# Authentication, CSRF, permissions
# ---------------------------------------------------------------------------------------------
async def test_requires_login_and_csrf(api: Api) -> None:
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_web_app(api.ctx)),  # type: ignore[arg-type]
        base_url="http://testserver",
    )
    async with anonymous:
        resp = await anonymous.get("/api/admin/users")
        assert resp.status_code == 401
    no_csrf = await api.client.post("/api/admin/users/1/rotate-link", headers={"cookie": api.cookie})
    assert no_csrf.status_code == 403 and no_csrf.json()["error"] == "csrf_failed"
    assert (await api.get("/users")).status_code == 200


async def test_permissions_per_role(api: Api) -> None:
    await add_user(api.ctx, 10, sub_days=5)
    viewer = await as_role(api, AdminRole.VIEWER)
    assert (await viewer.get("/users")).status_code == 200
    assert (await viewer.get("/dashboard")).status_code == 200
    denied = await viewer.post("/users/10/days", json={"days": 3})
    assert denied.status_code == 403 and denied.json()["error"] == "forbidden"
    assert (await viewer.get("/plans")).status_code == 200
    assert (await viewer.post("/plans", json={"title": "x", "duration_days": 1, "price_kop": 0})).status_code == 403

    support = await as_role(api, AdminRole.SUPPORT, tg_id=2001)
    assert (await support.post("/users/10/days", json={"days": 3})).status_code == 200
    money = await support.post("/users/10/balance", json={"amount_kop": 100, "reason": "bonus"})
    assert money.status_code == 403
    assert (await support.get("/settings")).status_code == 403

    admin = await as_role(api, AdminRole.ADMIN, tg_id=2002)
    assert (await admin.get("/admins")).status_code == 403  # owner only
    assert (await admin.get("/settings")).status_code == 200


async def test_disabled_web_admin_is_404(tmp_path: Path) -> None:
    ctx, engine = await build_ctx(tmp_path, web_admin_enabled=False)
    try:
        app = create_web_app(ctx)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            session = await Api(ctx, client).login()
            assert (await session.get("/users")).status_code == 404
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------------
# Lists and the card
# ---------------------------------------------------------------------------------------------
async def test_list_filter_search_and_paginate(api: Api) -> None:
    ctx = api.ctx
    plan_id = await add_plan(ctx)
    await add_user(ctx, 11, username="alice", first_name="Алиса", sub_days=10, plan_id=plan_id)
    await add_user(ctx, 12, username="bob", sub_days=-3)
    await add_user(ctx, 13, first_name="Carol", is_banned=True)
    await add_user(ctx, 14, bot_blocked=True)

    body = (await api.get("/users", params={"limit": 2})).json()
    assert body["total"] == 4 and len(body["items"]) == 2 and body["limit"] == 2

    active = (await api.get("/users", params={"status": "active"})).json()
    assert [u["tg_id"] for u in active["items"]] == [11]
    assert active["items"][0]["sub_plan_id"] == plan_id
    assert [u["tg_id"] for u in (await api.get("/users", params={"status": "expired"})).json()["items"]] == [12]
    assert [u["tg_id"] for u in (await api.get("/users", params={"status": "banned"})).json()["items"]] == [13]
    assert [u["tg_id"] for u in (await api.get("/users", params={"status": "blocked"})).json()["items"]] == [14]
    no_sub = (await api.get("/users", params={"status": "no_subscription"})).json()
    assert {u["tg_id"] for u in no_sub["items"]} == {13, 14}
    assert (await api.get("/users", params={"status": "never_paid"})).json()["total"] == 4

    assert [u["tg_id"] for u in (await api.get("/users", params={"q": "@alice"})).json()["items"]] == [11]
    assert [u["tg_id"] for u in (await api.get("/users", params={"q": "12"})).json()["items"]] == [12]
    assert [u["tg_id"] for u in (await api.get("/users", params={"q": "алиса"})).json()["items"]] == [11]
    assert (await api.get("/users", params={"q": "100%_"})).json()["total"] == 0
    assert (await api.get("/users", params={"plan_id": plan_id})).json()["total"] == 1


async def test_user_card(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 20, username="parent")
    await add_user(ctx, 21, username="child", referred_by=20, sub_days=7, balance=500)
    card = (await api.get("/users/21")).json()
    assert card["user"]["username"] == "child" and card["user"]["balance_kop"] == 500
    assert card["subscription"]["active"] is True and card["subscription"]["days_left"] in (6, 7)
    assert card["subscription_url"] == "https://bot.example.com/sub/" + "token" + f"{21:0>20}"
    assert card["referrals"]["inviter"]["tg_id"] == 20
    assert card["is_admin"] is False
    parent = (await api.get("/users/20")).json()
    assert parent["referrals"]["invited"] == 1 and parent["referrals"]["levels"][0] == 1
    refs = (await api.get("/users/20/referrals")).json()
    assert [u["tg_id"] for u in refs["items"]] == [21]
    assert (await api.get("/users/999")).status_code == 404


# ---------------------------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------------------------
async def test_balance_adjust_is_idempotent_and_audited(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 30, balance=1000)
    body = {"amount_kop": 2500, "reason": "compensation", "request_id": "req-0000001", "notify": True}
    first = (await api.post("/users/30/balance", json=body)).json()
    assert first == {"applied": True, "balance_kop": 3500, "referral_balance_kop": 0}
    again = (await api.post("/users/30/balance", json=body)).json()
    assert again["applied"] is False and again["balance_kop"] == 3500
    await _settle()
    assert any("зачислено" in t for t in ctx.bot.texts_to(30))

    too_much = await api.post("/users/30/balance", json={"amount_kop": -10_000, "reason": "mistake"})
    assert too_much.status_code == 409 and too_much.json()["error"] == "insufficient_funds"
    partner = await api.post("/users/30/balance", json={"amount_kop": 700, "reason": "manual", "account": "referral"})
    assert partner.json()["referral_balance_kop"] == 700
    assert (await api.post("/users/30/balance", json={"amount_kop": 0, "reason": "zero"})).status_code == 422

    entry = await _audit(ctx, "user.balance_adjust")
    assert entry is not None and entry.source == "web" and entry.ip == "203.0.113.5"
    async with ctx.session_factory() as s:
        rows = await s.scalar(select(func.count(BalanceTransaction.id)).where(BalanceTransaction.user_id == 30))
    assert rows == 2


# ---------------------------------------------------------------------------------------------
# Subscription actions
# ---------------------------------------------------------------------------------------------
async def test_grant_and_remove_days(api: Api) -> None:
    ctx = api.ctx
    plan_id = await add_plan(ctx)
    await add_user(ctx, 40)
    granted = await api.post("/users/40/days", json={"days": 10, "plan_id": plan_id, "notify": True})
    assert granted.status_code == 200
    sub = granted.json()
    assert sub["active"] and sub["plan_id"] == plan_id and sub["days_left"] in (9, 10)
    async with ctx.session_factory() as s:
        purchase = await s.scalar(select(Purchase).where(Purchase.user_id == 40))
    assert purchase is not None and purchase.kind == PurchaseKind.ADMIN_GRANT and purchase.actor_id == OWNER
    await _settle()
    assert any("10" in t for t in ctx.bot.texts_to(40))

    removed = (await api.post("/users/40/days/remove", json={"days": 4})).json()
    assert removed["days_left"] in (5, 6)
    gone = (await api.post("/users/40/days/remove", json={"days": 30})).json()
    assert gone["active"] is False
    exact = datetime.now(UTC) + timedelta(days=100)
    back = (await api.put("/users/40/expiry", json={"expires_at": exact.isoformat()})).json()
    assert back["active"] is True and back["status"] == "active"
    naive = await api.put("/users/40/expiry", json={"expires_at": "2030-01-01T00:00:00"})
    assert naive.status_code == 422
    assert (await api.post("/users/40/days", json={"days": 1, "plan_id": 999})).status_code == 400
    entry = await _audit(ctx, "subscription.remove_days")
    assert entry is not None and entry.source == "web"


async def test_limits_flags_status(api: Api) -> None:
    ctx = api.ctx
    plan_id = await add_plan(ctx, title="VIP", price_kop=0)
    await add_user(ctx, 50, sub_days=10)
    limits = (await api.put("/users/50/limits", json={"device_limit": 7, "traffic_limit_gb": 100})).json()
    assert limits["device_limit"] == 7 and limits["traffic_limit_gb"] == 100
    plan = (await api.put("/users/50/limits", json={"plan_id": plan_id, "apply_plan_limits": True})).json()
    assert plan["plan_id"] == plan_id and plan["device_limit"] == 3
    assert (await api.put("/users/50/limits", json={})).status_code == 400

    assert (await api.put("/users/50/auto-renew", json={"value": False})).json()["auto_renew"] is False
    comp = (await api.put("/users/50/complimentary", json={"value": True})).json()
    assert comp["is_complimentary"] is True and comp["auto_renew"] is False
    blocked = (await api.put("/users/50/status", json={"status": "disabled"})).json()
    assert blocked["status"] == "disabled" and blocked["active"] is False
    assert (await api.put("/users/50/status", json={"status": "active"})).json()["status"] == "active"
    assert (await api.put("/users/51/status", json={"status": "active"})).status_code == 404


async def test_ban_rotate_message(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 60, sub_days=3)
    async with ctx.session_factory() as s:
        before = await s.scalar(select(Subscription.revision).where(Subscription.user_id == 60))
    banned = (await api.put("/users/60/ban", json={"value": True})).json()
    assert banned["is_banned"] is True
    async with ctx.session_factory() as s:
        after = await s.scalar(select(Subscription.revision).where(Subscription.user_id == 60))
    assert after == (before or 0) + 1
    assert (await api.put(f"/users/{OWNER}/ban", json={"value": True})).status_code == 409

    old = (await api.get("/users/60")).json()["subscription_url"]
    rotated = (await api.post("/users/60/rotate-link")).json()["subscription_url"]
    assert rotated and rotated != old

    sent = await api.post("/users/60/message", json={"text": "<b>Привет</b>"})
    assert sent.json() == {"delivered": True}
    assert ctx.bot.texts_to(60) == ["<b>Привет</b>"]
    bad = await api.post("/users/60/message", json={"text": "<script>x</script>"})
    assert bad.status_code == 400 and bad.json()["error"] == "bad_html"


async def test_bulk_ban_skips_staff_and_reports_unknown_users(api: Api) -> None:
    await add_user(api.ctx, 61, sub_days=3)
    await add_user(api.ctx, OWNER)
    result = await api.post("/users/bulk-ban", json={"user_ids": [61, OWNER, 999_999], "reason": "abuse"})
    assert result.status_code == 200
    assert result.json() == {"changed": 1, "unknown": 1, "skipped_staff": 1}
    assert (await api.get("/users/61")).json()["user"]["is_banned"] is True
    unban = await api.post("/users/bulk-ban", json={"user_ids": [61], "value": False})
    assert unban.json()["changed"] == 1


async def test_devices_and_resync(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 70, sub_days=3)
    async with ctx.session_factory.begin() as s:
        panel = Panel(name="rw", kind=PanelKind.REMNAWAVE, url="https://rw.example.com")
        s.add(panel)
        await s.flush()
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == 70))
        assert sub is not None
        s.add(
            Provision(
                subscription_id=sub.id,
                panel_id=panel.id,
                inbound_key=0,
                remote_email="tg_70",
                desired_state=ProvisionState.ENABLED,
                actual_state=ProvisionState.ENABLED,
            )
        )
        panel_id = panel.id
    ctx.panels.backend(panel_id).kind = PanelKind.REMNAWAVE
    devices = (await api.get("/users/70/devices")).json()
    assert devices[0]["panel_id"] == panel_id and devices[0]["supported"] is True
    reset = (await api.post("/users/70/devices/reset", json={})).json()
    assert reset["reset"] == 1 and reset["failed"] == 0
    synced = (await api.post("/users/70/resync")).json()
    assert synced["found"] is True and synced["failed"] == 0
    assert any(call[0] == "upsert" or call[0] == "reset_devices" for call in ctx.panels.backend(panel_id).calls)


async def test_delete_user_removes_panel_accounts(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 80, sub_days=3)
    async with ctx.session_factory.begin() as s:
        panel = Panel(name="rw", kind=PanelKind.REMNAWAVE, url="https://rw.example.com")
        s.add(panel)
        await s.flush()
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == 80))
        assert sub is not None
        s.add(
            Provision(
                subscription_id=sub.id,
                panel_id=panel.id,
                inbound_key=0,
                remote_email="tg_80",
                desired_state=ProvisionState.ENABLED,
                actual_state=ProvisionState.ENABLED,
            )
        )
        panel_id = panel.id
    backend = ctx.panels.backend(panel_id)
    backend.down = True
    refused = await api.delete("/users/80")
    assert refused.status_code == 409 and refused.json()["error"] == "panels_failed"
    backend.down = False
    deleted = (await api.delete("/users/80")).json()
    assert deleted == {"deleted": True, "accounts_removed": 1, "errors": []}
    assert ("delete", None, "tg_80") in backend.calls
    async with ctx.session_factory() as s:
        assert await s.get(User, 80) is None
        assert await s.scalar(select(func.count(Subscription.id))) == 0
    assert (await api.delete(f"/users/{OWNER}")).status_code in (404, 409)


async def test_export_csv_is_safe(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 90, first_name="=HYPERLINK(1)", sub_days=1)
    await add_user(ctx, 91, username="plain")
    resp = await api.get("/users/export.csv")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    lines = resp.text.lstrip("﻿").strip().splitlines()
    assert lines[0].startswith("tg_id,username") and len(lines) == 3
    assert "'=HYPERLINK(1)" in resp.text
    support = await as_role(api, AdminRole.SUPPORT)
    assert (await support.get("/users/export.csv")).status_code == 403


async def test_subscription_status_expired_when_unblocking_past_expiry(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 95, sub_days=-1)
    async with ctx.session_factory.begin() as s:
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == 95))
        assert sub is not None
        sub.status = SubscriptionStatus.DISABLED
    out = (await api.put("/users/95/status", json={"status": "active"})).json()
    assert out["status"] == "expired"


async def _link_stars(ctx: ApiCtx, user_id: int, charge_id: str) -> None:
    async with ctx.session_factory.begin() as s:
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == user_id))
        assert sub is not None
        sub.stars_charge_id = charge_id
        sub.auto_renew = True


async def _charge_id(ctx: ApiCtx, user_id: int) -> str | None:
    async with ctx.session_factory() as s:
        return await s.scalar(select(Subscription.stars_charge_id).where(Subscription.user_id == user_id))


async def test_delete_user_cancels_stars_subscription(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 81, sub_days=3)
    await _link_stars(ctx, 81, "charge-81")
    ctx.bot.fail_stars = True
    refused = await api.delete("/users/81")
    assert refused.status_code == 409 and refused.json()["error"] == "stars_cancel_failed"
    async with ctx.session_factory() as s:
        assert await s.get(User, 81) is not None
    ctx.bot.fail_stars = False
    assert (await api.delete("/users/81")).json()["deleted"] is True
    assert ctx.bot.stars_canceled == [(81, "charge-81")]


async def test_delete_user_forced_when_stars_cancel_fails(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 82, sub_days=3)
    await _link_stars(ctx, 82, "charge-82")
    ctx.bot.fail_stars = True
    forced = (await api.delete("/users/82", params={"force": True})).json()
    assert forced["deleted"] is True and any("Stars" in e for e in forced["errors"])


async def test_auto_renew_off_and_complimentary_cancel_stars_subscription(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 83, sub_days=3)
    await _link_stars(ctx, 83, "charge-83")
    ctx.bot.fail_stars = True
    failed = await api.put("/users/83/auto-renew", json={"value": False})
    assert failed.status_code == 409 and failed.json()["error"] == "stars_cancel_failed"
    assert await _charge_id(ctx, 83) == "charge-83"
    ctx.bot.fail_stars = False
    assert (await api.put("/users/83/auto-renew", json={"value": False})).json()["auto_renew"] is False
    assert ctx.bot.stars_canceled == [(83, "charge-83")] and await _charge_id(ctx, 83) is None

    await add_user(ctx, 84, sub_days=3)
    await _link_stars(ctx, 84, "charge-84")
    # Turning auto-renew on or taking complimentary access away leaves Stars alone.
    assert (await api.put("/users/84/auto-renew", json={"value": True})).status_code == 200
    assert (await api.put("/users/84/complimentary", json={"value": False})).status_code == 200
    assert await _charge_id(ctx, 84) == "charge-84"
    comp = (await api.put("/users/84/complimentary", json={"value": True})).json()
    assert comp["is_complimentary"] is True and comp["auto_renew"] is False
    assert (84, "charge-84") in ctx.bot.stars_canceled and await _charge_id(ctx, 84) is None
