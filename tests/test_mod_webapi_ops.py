from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.db.models import (
    AdminRole,
    Broadcast,
    BroadcastStatus,
    Event,
    LedgerAccount,
    Purchase,
    Subscription,
    TicketStatus,
    User,
    Withdrawal,
    WithdrawalStatus,
)
from app.modules.admin_api.deps import drain_background
from app.services import tickets
from tests.test_mod_webapi_support import Api, add_plan, add_user, api, as_role, ctx

__all__ = ["api", "ctx"]


# ---------------------------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------------------------
async def test_dashboard_series_events(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 1, sub_days=5)
    await add_user(ctx, 2, referred_by=1)
    async with ctx.session_factory.begin() as s:
        s.add(Event(type="user.registered", user_id=2, data=json.dumps({"source": "ad"})))
        s.add(Event(type="payment.succeeded", user_id=1, amount_kop=100))
    body = (await api.get("/dashboard")).json()
    assert body["users_total"] == 2 and body["subscriptions_active"] == 1
    assert body["panels"] == {"total": 0, "active": 0, "healthy": 0, "down": []}
    assert body["top_referrers"][0]["tg_id"] == 1 and body["top_referrers"][0]["referrals"] == 1
    series = (await api.get("/dashboard/series", params={"days": 7})).json()
    assert series["days"] == 7 and len(series["points"]) == 7
    assert series["points"][-1]["registrations"] == 2
    assert (await api.get("/dashboard/series", params={"days": 0})).status_code == 422
    events = (await api.get("/dashboard/events", params={"type": "user."})).json()
    assert [e["type"] for e in events] == ["user.registered"] and events[0]["data"] == {"source": "ad"}


# ---------------------------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------------------------
async def test_subscriptions_list_and_compensation(api: Api) -> None:
    ctx = api.ctx
    plan_id = await add_plan(ctx)
    await add_user(ctx, 10, sub_days=2, plan_id=plan_id)
    await add_user(ctx, 11, sub_days=20)
    await add_user(ctx, 12, sub_days=-1)
    listed = (await api.get("/subscriptions", params={"status": "active"})).json()
    assert [s["user_id"] for s in listed["items"]] == [10, 11]
    expiring = (await api.get("/subscriptions", params={"expiring_in_days": 3})).json()
    assert [s["user_id"] for s in expiring["items"]] == [10]
    assert (await api.get("/subscriptions", params={"status": "expired"})).json()["total"] == 1
    assert (await api.get("/subscriptions", params={"plan_id": plan_id})).json()["total"] == 1

    dry = (await api.post("/subscriptions/compensation", json={"days": 3, "reason": "outage", "dry_run": True})).json()
    assert dry == {"affected": 2, "dry_run": True, "started_jobs": []}
    body = {"days": 3, "reason": "outage", "request_id": "outage-2026-11-04"}
    done = (await api.post("/subscriptions/compensation", json=body)).json()
    assert done["affected"] == 2 and done["started_jobs"] == ["provisioning.reconcile"]
    assert (await api.post("/subscriptions/compensation", json=body)).status_code == 409
    async with ctx.session_factory() as s:
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == 10))
        purchases = await s.scalar(select(func.count(Purchase.id)))
    assert sub is not None and sub.expires_at > datetime.now(UTC) + timedelta(days=4)
    assert purchases == 2

    support = await as_role(api, AdminRole.SUPPORT)
    assert (await support.post("/subscriptions/resync", json={})).status_code == 403
    resync = (await api.post("/subscriptions/resync", json={})).json()
    assert resync["marked"] == 0
    assert (await api.post("/subscriptions/resync", json={"panel_id": 99})).status_code == 409


# ---------------------------------------------------------------------------------------------
# Referral & withdrawals
# ---------------------------------------------------------------------------------------------
async def test_referral_overview_and_withdrawals(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 20, username="partner")
    await add_user(ctx, 21, referred_by=20)
    async with ctx.session_factory.begin() as s:
        partner = await s.get(User, 20)
        assert partner is not None
        partner.referral_balance_kop = 0
        s.add(Withdrawal(user_id=20, amount_kop=50000, method="card", details="4111", status=WithdrawalStatus.PENDING))
        s.add(Withdrawal(user_id=20, amount_kop=10000, method="sbp", details="+7", status=WithdrawalStatus.PENDING))
    overview = (await api.get("/referral")).json()
    assert overview["referred_users"] == 1 and overview["pending_withdrawals"] == 2
    assert overview["pending_withdrawal_kop"] == 60000 and overview["top_partners"][0]["tg_id"] == 20
    stats = (await api.get("/referral/partners/20")).json()
    assert stats["total_users"] >= 1 and stats["pending_withdrawal_kop"] == 60000

    queue = (await api.get("/withdrawals")).json()
    assert queue["total"] == 2 and queue["items"][0]["username"] == "partner"
    first, second = (item["withdrawal"]["id"] for item in queue["items"])
    approved = (await api.post(f"/withdrawals/{first}/approve", json={})).json()
    assert approved["withdrawal"]["status"] == "approved"
    paid = (await api.post(f"/withdrawals/{first}/paid", json={"comment": "done"})).json()
    assert paid["withdrawal"]["status"] == "paid"
    assert (await api.post(f"/withdrawals/{first}/reject", json={})).status_code == 409
    rejected = (await api.post(f"/withdrawals/{second}/reject", json={"comment": "<bad details>"})).json()
    assert rejected["withdrawal"]["status"] == "rejected"
    assert (await api.post("/withdrawals/999/approve", json={})).status_code == 404
    async with ctx.session_factory() as s:
        partner = await s.get(User, 20)
    assert partner is not None and partner.referral_balance_kop == 10000
    await drain_background()
    texts = ctx.bot.texts_to(20)
    assert len(texts) == 3 and "&lt;bad details&gt;" in texts[-1]
    history = (await api.get("/withdrawals", params={"open_only": False})).json()
    assert history["total"] == 2
    support = await as_role(api, AdminRole.SUPPORT)
    assert (await support.get("/withdrawals")).status_code == 403
    assert LedgerAccount.REFERRAL.value == "referral"


# ---------------------------------------------------------------------------------------------
# Broadcasts
# ---------------------------------------------------------------------------------------------
async def test_broadcast_flow(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 30, sub_days=3)
    await add_user(ctx, 31)
    count = (await api.post("/broadcasts/audience/count", json={"kind": "active"})).json()
    assert count == {"recipients": 1}
    bad = await api.post("/broadcasts/audience/count", json={"kind": "custom"})
    assert bad.status_code == 400 and bad.json()["error"] == "bad_audience"

    media = await api.post(
        "/broadcasts/media",
        params={"type": "photo", "filename": "banner.jpg"},
        content=b"\xff\xd8\xff fake jpeg",
        headers={"content-type": "application/octet-stream"},
    )
    assert media.json() == {"media_type": "photo", "media": "photo-file-id"}
    assert ctx.bot.deleted

    body = {
        "audience": {"kind": "all"},
        "text": "<b>Новости</b>",
        "buttons": [[{"text": "Канал", "url": "https://t.me/channel"}]],
        "media_type": "photo",
        "media": "photo-file-id",
    }
    created = await api.post("/broadcasts", json=body)
    assert created.status_code == 201, created.text
    broadcast = created.json()
    assert broadcast["status"] == "draft" and broadcast["buttons"][0][0]["text"] == "Канал"
    assert (await api.post("/broadcasts", json={**body, "text": "<script>"})).status_code == 400
    assert (await api.post("/broadcasts", json={"audience": {"kind": "all"}})).status_code == 400

    when = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    scheduled = (await api.post(f"/broadcasts/{broadcast['id']}/schedule", json={"scheduled_at": when})).json()
    assert scheduled["scheduled_at"] is not None
    past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    assert (await api.post(f"/broadcasts/{broadcast['id']}/schedule", json={"scheduled_at": past})).status_code == 400

    preview = (await api.post(f"/broadcasts/{broadcast['id']}/preview")).json()
    assert preview == {"delivered": True}
    started = (await api.post(f"/broadcasts/{broadcast['id']}/start")).json()
    assert started["status"] == "running" and started["total"] == 2
    assert "broadcasts.run" in (ctx.scheduler.ran if ctx.scheduler else [])
    assert (await api.post(f"/broadcasts/{broadcast['id']}/cancel")).json() == {"ok": True}
    assert (await api.post(f"/broadcasts/{broadcast['id']}/cancel")).status_code == 409
    history = (await api.get("/broadcasts")).json()
    assert history["total"] == 1 and history["items"][0]["status"] == "canceled"
    stream = await api.get(f"/broadcasts/{broadcast['id']}/events")
    assert "event: progress" in stream.text and "event: done" in stream.text
    async with ctx.session_factory() as s:
        row = await s.get(Broadcast, broadcast["id"])
    assert row is not None and row.status == BroadcastStatus.CANCELED
    assert (await api.get("/broadcasts/999")).status_code == 404


# ---------------------------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------------------------
async def test_tickets_inbox_reply_close(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 40, username="asker", language="en")
    async with ctx.session_factory.begin() as s:
        ticket = await tickets.create(s, 40, "VPN does not connect <help>")
        ticket_id = ticket.id
        s.add(ticket)
    async with ctx.session_factory.begin() as s:
        from app.db.models import TicketMessage

        s.add(TicketMessage(ticket_id=ticket_id, author_id=40, is_admin=False, text=None, file_id="photo-1"))
    counts = (await api.get("/tickets/counts")).json()
    assert counts == {"open": 1, "answered": 0, "closed": 0}
    inbox = (await api.get("/tickets")).json()
    assert inbox["total"] == 1 and inbox["items"][0]["messages"] == 2 and inbox["items"][0]["username"] == "asker"
    view = (await api.get(f"/tickets/{ticket_id}")).json()
    assert view["language"] == "en" and len(view["messages"]) == 2
    file_message = view["messages"][1]["id"]
    attachment = await api.get(f"/tickets/{ticket_id}/messages/{file_message}/file")
    assert attachment.status_code == 200 and attachment.content == b"JPEG"

    replied = (await api.post(f"/tickets/{ticket_id}/reply", json={"text": "Try <another> server"})).json()
    assert replied["delivered"] is True and replied["message"]["is_admin"] is True
    sent = ctx.bot.texts_to(40)[-1]
    assert "Support replied" in sent and "&lt;another&gt;" in sent
    assert (await api.get("/tickets", params={"status": "answered"})).json()["total"] == 1
    closed = (await api.post(f"/tickets/{ticket_id}/close")).json()
    assert closed["ticket"]["status"] == "closed"
    assert "closed" in ctx.bot.texts_to(40)[-1]
    assert (await api.post("/tickets/999/reply", json={"text": "x"})).status_code == 404
    support = await as_role(api, AdminRole.SUPPORT)
    assert (await support.get("/tickets")).status_code == 200
    viewer = await as_role(api, AdminRole.VIEWER, tg_id=2001)
    assert (await viewer.get("/tickets")).status_code == 403
    assert TicketStatus.CLOSED.value == "closed"
