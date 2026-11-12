from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.models import AdminAuditLog, AdminRole, Payment, PaymentStatus, User
from tests.test_mod_webapi_support import Api, ApiCtx, FakeGateway, FakeGateways, add_user, api, as_role, ctx

__all__ = ["api", "ctx"]


async def _payment(ctx: ApiCtx, user_id: int, **fields: object) -> str:
    payment_id = str(uuid.uuid4())
    values: dict[str, object] = {
        "provider": "cryptobot",
        "amount_kop": 19900,
        "status": PaymentStatus.PENDING,
        **fields,
    }
    async with ctx.session_factory.begin() as s:
        s.add(Payment(id=payment_id, user_id=user_id, **values))
    return payment_id


async def test_list_filter_and_detail(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 1, username="buyer")
    pending = await _payment(ctx, 1, provider_payment_id="inv-1")
    done = await _payment(ctx, 1, provider="stars", status=PaymentStatus.SUCCEEDED, credited_at=datetime.now(UTC))
    all_ = (await api.get("/payments")).json()
    assert all_["total"] == 2
    assert [p["id"] for p in (await api.get("/payments", params={"status": "pending"})).json()["items"]] == [pending]
    assert [p["id"] for p in (await api.get("/payments", params={"provider": "stars"})).json()["items"]] == [done]
    assert [p["id"] for p in (await api.get("/payments", params={"q": "inv-1"})).json()["items"]] == [pending]
    detail = (await api.get(f"/payments/{pending}")).json()
    assert detail["payment"]["id"] == pending and detail["username"] == "buyer"
    assert (await api.get("/payments/not-a-real-id")).status_code == 404
    assert (await api.get("/payments/bad id")).status_code == 422


async def test_mark_paid_credits_once_and_runs_hook(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 2)
    payment_id = await _payment(ctx, 2)
    viewer = await as_role(api, AdminRole.SUPPORT)
    assert (
        await viewer.post(f"/payments/{payment_id}/mark-paid", json={"reason": "seen in cabinet"})
    ).status_code == 403
    paid = (await api.post(f"/payments/{payment_id}/mark-paid", json={"reason": "seen in cabinet"})).json()
    assert paid == {"credited": True, "amount_kop": 19900, "new_balance_kop": 19900, "hook_ran": True}
    assert [r.payment_id for r in ctx.credited] == [payment_id]
    again = await api.post(f"/payments/{payment_id}/mark-paid", json={"reason": "twice"})
    assert again.status_code == 409
    async with ctx.session_factory() as s:
        user = await s.get(User, 2)
        entry = await s.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "payment.mark_paid"))
    assert user is not None and user.balance_kop == 19900
    assert entry is not None and entry.source == "web"

    rerun = (await api.post(f"/payments/{payment_id}/rerun")).json()
    assert rerun["hook_ran"] is True and len(ctx.credited) == 2
    other = await _payment(ctx, 2)
    assert (await api.post(f"/payments/{other}/rerun")).status_code == 409


async def test_cancel_open_payment(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 3)
    payment_id = await _payment(ctx, 3)
    out = (await api.post(f"/payments/{payment_id}/status", json={"status": "canceled"})).json()
    assert out["status"] == "canceled"
    assert (await api.post(f"/payments/{payment_id}/status", json={"status": "expired"})).status_code == 409


async def test_stars_refund(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 4, balance=5000)
    payment_id = await _payment(
        ctx,
        4,
        provider="stars",
        status=PaymentStatus.SUCCEEDED,
        provider_payment_id="charge-1",
        credited_at=datetime.now(UTC),
    )
    out = (await api.post(f"/payments/{payment_id}/refund", json={"reason": "asked for it"})).json()
    assert out == {"refunded": True, "debited_kop": 5000}
    assert ctx.bot.refunds == [(4, "charge-1")]
    async with ctx.session_factory() as s:
        payment = await s.get(Payment, payment_id)
        user = await s.get(User, 4)
    assert payment is not None and payment.status == PaymentStatus.REFUNDED
    assert user is not None and user.balance_kop == 0
    assert (await api.post(f"/payments/{payment_id}/refund", json={"reason": "again"})).status_code == 409
    other = await _payment(ctx, 4, status=PaymentStatus.SUCCEEDED)
    assert (await api.post(f"/payments/{other}/refund", json={"reason": "not stars"})).status_code == 400


async def test_providers(api: Api) -> None:
    ctx = api.ctx
    gateway = FakeGateway("cryptobot", title="CryptoBot")
    ctx.gateways = FakeGateways(gateway)
    await add_user(ctx, 5)
    await _payment(ctx, 5, status=PaymentStatus.SUCCEEDED, credited_at=datetime.now(UTC))
    providers = {p["key"]: p for p in (await api.get("/payments/providers")).json()}
    crypto = providers["cryptobot"]
    assert crypto["active"] is True and crypto["payments_30d"] == 1 and crypto["amount_30d_kop"] == 19900
    assert crypto["webhook_url"] == "https://bot.example.com/webhooks/cryptobot"
    assert crypto["settings_group"] == "payments_cryptobot"
    assert providers["stars"]["webhook_url"] is None and providers["stars"]["active"] is False
    tested = (await api.post("/payments/providers/cryptobot/test")).json()
    assert tested["ok"] is True and gateway.checked == 1
    off = (await api.post("/payments/providers/yookassa/test")).json()
    assert off["ok"] is False
