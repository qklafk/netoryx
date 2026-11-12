"""Payments module: method picker, invoices, Telegram Stars confirmations, donations, webhooks and the reconcile job."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from aiogram.methods import AnswerCallbackQuery, AnswerPreCheckoutQuery, CreateInvoiceLink, SendInvoice
from fastapi import APIRouter, FastAPI
from sqlalchemy import func, select

from app.config import Settings
from app.core.modules import ModuleRegistry, module_enabled
from app.db.models import Payment, PaymentStatus, Plan, Subscription, User
from app.keyboards.callbacks import Nav
from app.modules.payments import flow, jobs
from app.modules.payments import module as payments_module
from app.modules.payments.callbacks import PayCb, PayCheckCb, Purpose
from app.modules.payments.web import router as webhook_router
from app.services import plans
from app.services.payments.base import WebhookRejected, WebhookResult
from tests.test_mod_money_support import (
    FakeGateway,
    Harness,
    balance,
    buttons,
    callback_datas,
    money,
    pre_checkout_update,
    refunded_payment_update,
    register,
    successful_payment_update,
)

__all__ = ["money"]

UID = 5001


async def _payments(h: Harness) -> list[Payment]:
    async with h.ctx.session_factory() as session:
        return list((await session.scalars(select(Payment).order_by(Payment.created_at))).all())


async def _plan(h: Harness, *, price_rub: int = 150, days: int = 30, stars: int | None = None) -> int:
    async with h.ctx.session_factory.begin() as session:
        plan = await plans.create(session, title="Month", duration_days=days, price_kop=price_rub * 100)
        plan.price_stars = stars
        return plan.id


# --- method picker and invoices ---------------------------------------------------------------------------------------
async def test_topup_methods_list_enabled_providers(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await h.press(UID, PayCb(a="m", k="t", amt=30_000).pack())

    text = h.api.screens()[-1]
    assert "300 ₽" in text and "Пополнение" in text
    labels = [b.text for b in buttons(h.api.last_markup())]
    assert "Fake provider" in labels[0] or "Криптовалюта" in labels[0]
    assert any("200 ⭐️" in label for label in labels)  # 300 ₽ at 1.5 ₽ per Star


async def test_provider_invoice_is_persisted_and_shown(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await h.press(UID, PayCb(a="go", k="t", amt=50_000, prov="cryptobot").pack())

    [payment] = await _payments(h)
    assert payment.status is PaymentStatus.PENDING and payment.amount_kop == 50_000 and payment.plan_id is None
    markup = h.api.last_markup()
    urls = [b.url for b in buttons(markup) if b.url]
    assert urls == [f"https://pay.example.com/{payment.id}"]
    assert PayCheckCb(id=payment.id).pack() in callback_datas(markup)
    assert h.gateway.created == ["Пополнение баланса TestVPN"]


async def test_forged_amount_is_refused(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await h.press(UID, PayCb(a="go", k="t", amt=1, prov="cryptobot").pack())
    await h.press(UID, PayCb(a="go", k="t", amt=500_000_000, prov="cryptobot").pack())
    assert await _payments(h) == []
    alerts = [c for c in h.api.of(AnswerCallbackQuery) if c.show_alert]
    assert len(alerts) == 2


async def test_cart_pays_only_the_missing_amount_and_buys_the_plan(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID, balance_kop=5_000)
    plan_id = await _plan(h)
    await h.press(UID, PayCb(a="m", k="p", plan=plan_id).pack())
    assert "100 ₽" in h.api.screens()[-1]  # 150 ₽ plan, 50 ₽ on the balance

    await h.press(UID, PayCb(a="go", k="p", plan=plan_id, prov="cryptobot").pack())
    [payment] = await _payments(h)
    assert payment.amount_kop == 10_000 and payment.plan_id == plan_id

    h.gateway.status = PaymentStatus.SUCCEEDED
    await h.press(UID, PayCheckCb(id=payment.id).pack())
    assert len(h.ctx.credited) == 1
    async with h.ctx.session_factory() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.user_id == UID))
    assert sub is not None and sub.plan_id == plan_id
    assert await balance(h.ctx, UID) == (0, 0)


async def test_check_reports_pending_payment(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await h.press(UID, PayCb(a="go", k="t", amt=50_000, prov="cryptobot").pack())
    [payment] = await _payments(h)
    await h.press(UID, PayCheckCb(id=payment.id).pack())
    assert h.api.of(AnswerCallbackQuery)[-1].show_alert
    assert h.ctx.credited == []
    await h.press(UID + 1, PayCheckCb(id=payment.id).pack())  # someone else's payment
    assert h.ctx.credited == []


async def test_inactive_plan_cannot_be_paid(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    plan_id = await _plan(h)
    async with h.ctx.session_factory.begin() as session:
        plan = await session.get(Plan, plan_id)
        assert plan is not None
        plan.is_active = False
    await h.press(UID, PayCb(a="go", k="p", plan=plan_id, prov="cryptobot").pack())
    assert await _payments(h) == []


async def test_hidden_plan_cannot_be_paid_through_a_crafted_button(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    plan_id = await _plan(h)
    async with h.ctx.session_factory.begin() as session:
        plan = await session.get(Plan, plan_id)
        assert plan is not None
        plan.is_hidden = True
    await h.press(UID, PayCb(a="go", k="p", plan=plan_id, prov="cryptobot").pack())
    await h.press(UID, PayCb(a="go", k="g", plan=plan_id, prov="cryptobot").pack())
    assert await _payments(h) == []


# --- receipts ---------------------------------------------------------------------------------------------------------
async def test_yookassa_receipt_asks_for_email_first(money: Any) -> None:
    kassa = FakeGateway(key="yookassa", title="Kassa")
    h: Harness = money(gateways=[kassa], yookassa_receipt_enabled=True)
    await register(h.ctx, UID)
    await h.press(UID, PayCb(a="go", k="t", amt=50_000, prov="yookassa").pack())
    assert "e-mail" in h.api.screens()[-1]
    assert await _payments(h) == []

    await h.say(UID, "not an email")
    assert "ошибка" in h.api.screens()[-1]
    await h.say(UID, "Ann@Example.com")
    [payment] = await _payments(h)
    assert payment.provider == "yookassa" and payment.amount_kop == 50_000
    async with h.ctx.session_factory() as session:
        user = await session.get(User, UID)
        assert user is not None and user.receipt_email == "ann@example.com"


# --- Telegram Stars ---------------------------------------------------------------------------------------------------
async def test_stars_topup_invoice_pre_checkout_and_credit(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await h.press(UID, PayCb(a="go", k="t", amt=15_000, prov="stars").pack())
    [invoice] = h.api.of(SendInvoice)
    [payment] = await _payments(h)
    assert invoice.payload == payment.id and invoice.currency == "XTR"
    assert invoice.prices[0].amount == 100  # 150 ₽ / 1.5

    await h.feed(pre_checkout_update(UID, payment.id, 99))
    await h.feed(pre_checkout_update(UID, payment.id, 100))
    answers = h.api.of(AnswerPreCheckoutQuery)
    assert [a.ok for a in answers] == [False, True]
    assert answers[0].error_message

    await h.feed(successful_payment_update(UID, payment.id, 100, "charge-1"))
    await h.feed(successful_payment_update(UID, payment.id, 100, "charge-1"))  # redelivery
    assert len(h.ctx.credited) == 1
    assert await balance(h.ctx, UID) == (15_000, 0)
    assert any("Баланс пополнен" in text for text in h.api.texts())


async def test_stars_confirmations_work_while_the_module_is_off(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await h.press(UID, PayCb(a="go", k="t", amt=15_000, prov="stars").pack())
    [payment] = await _payments(h)
    h.set(disabled_modules=["payments"])
    await h.feed(pre_checkout_update(UID, payment.id, 100))
    assert h.api.of(AnswerPreCheckoutQuery)[-1].ok is False  # no new purchases while off ...
    await h.feed(successful_payment_update(UID, payment.id, 100, "charge-2"))
    assert await balance(h.ctx, UID) == (15_000, 0)  # ... but money already taken is credited


async def test_stars_plan_uses_the_plan_stars_price_and_subscription_link(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID, balance_kop=10_000)
    plan_id = await _plan(h, stars=99)
    await h.press(UID, PayCb(a="m", k="p", plan=plan_id).pack())
    datas = callback_datas(h.api.last_markup())
    assert PayCb(a="go", k="p", plan=plan_id, prov="stars", r=1).pack() in datas  # 30 days: Stars subscription

    await h.press(UID, PayCb(a="go", k="p", plan=plan_id, prov="stars", r=1).pack())
    [link] = h.api.of(CreateInvoiceLink)
    assert link.subscription_period == 2_592_000 and link.prices[0].amount == 99
    [payment] = await _payments(h)
    assert payment.is_recurring and payment.amount_kop == 15_000 and payment.provider_amount == "99"
    assert "https://t.me/$invoice-link" in [b.url for b in buttons(h.api.last_markup())]

    await h.feed(successful_payment_update(UID, payment.id, 99, "sub-charge", recurring=True, first=True))
    async with h.ctx.session_factory() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.user_id == UID))
    assert sub is not None and sub.stars_charge_id == "sub-charge"
    assert await balance(h.ctx, UID) == (10_000, 0)  # the balance was not touched


async def test_no_stars_subscription_for_other_periods(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    plan_id = await _plan(h, days=90)
    await h.press(UID, PayCb(a="m", k="p", plan=plan_id).pack())
    recurring = PayCb(a="go", k="p", plan=plan_id, prov="stars", r=1).pack()
    assert recurring not in callback_datas(h.api.last_markup())
    await h.press(UID, PayCb(a="go", k="p", plan=plan_id, prov="stars", r=1).pack())
    [payment] = await _payments(h)
    assert not payment.is_recurring  # silently a one-off invoice


async def test_refund_marks_the_payment(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await h.press(UID, PayCb(a="go", k="t", amt=15_000, prov="stars").pack())
    [payment] = await _payments(h)
    await h.feed(successful_payment_update(UID, payment.id, 100, "charge-r"))
    await h.feed(refunded_payment_update(UID, payment.id, 100, "charge-r"))
    [payment] = await _payments(h)
    assert payment.status is PaymentStatus.REFUNDED


# --- donations --------------------------------------------------------------------------------------------------------
async def test_donation_flow(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await h.say(UID, "/donate")
    datas = callback_datas(h.api.last_markup())
    donate = PayCb(a="go", k=Purpose.DONATION.value, amt=100, prov="stars").pack()
    assert donate in datas

    await h.press(UID, donate)
    [payment] = await _payments(h)
    assert payment.is_donation and payment.provider_amount == "100"
    await h.feed(successful_payment_update(UID, payment.id, 100, "don-1"))
    assert await balance(h.ctx, UID) == (0, 0)  # donations never reach the wallet
    assert any("Спасибо за поддержку" in text for text in h.api.texts())

    await h.press(UID, PayCb(a="go", k="d", amt=7, prov="stars").pack())  # not a preset amount
    assert len(await _payments(h)) == 1


async def test_donations_can_be_switched_off(money: Any) -> None:
    h: Harness = money(donations_enabled=False)
    await register(h.ctx, UID)
    await h.press(UID, Nav(to="donate").pack())
    assert "нельзя" in h.api.screens()[-1]


# --- flow helpers -----------------------------------------------------------------------------------------------------
async def test_order_validation(money: Any) -> None:
    h: Harness = money(gifts_enabled=False)
    await register(h.ctx, UID)
    plan_id = await _plan(h)
    order = await flow.build_order(h.ctx, UID, Purpose.PLAN, plan_id=plan_id)  # type: ignore[arg-type]
    assert order.amount_kop == 15_000 and order.plan is not None
    for purpose, kwargs in (
        (Purpose.GIFT, {"plan_id": plan_id}),
        (Purpose.TOPUP, {"amount_kop": 100}),
        ("x", {}),
    ):
        try:
            await flow.build_order(h.ctx, UID, purpose, **kwargs)  # type: ignore[arg-type]
        except flow.OrderError:
            continue
        raise AssertionError(f"{purpose} should be refused")
    assert flow.valid_email(" a.b@c.io ") == "a.b@c.io"
    assert flow.valid_email("a@b") is None


def test_personal_mode_and_switch(settings: Settings) -> None:
    assert module_enabled(payments_module, settings)
    personal = settings.model_copy(update={"app_mode": "personal"})
    assert not module_enabled(payments_module, personal)
    assert "payments" in ModuleRegistry([payments_module])
    assert [job.id for job in payments_module.jobs] == ["payments.reconcile"]
    assert jobs._schedule(settings) == {"seconds": settings.payment_reconcile_interval_sec}


# --- webhooks and reconcile -------------------------------------------------------------------------------------------
def _web_app(h: Harness) -> FastAPI:
    app = FastAPI()
    app.state.ctx = h.ctx
    app.include_router(webhook_router)
    other = APIRouter()

    @other.post("/webhooks/remnawave")
    async def remnawave() -> dict[str, str]:
        return {"panel": "ok"}

    app.include_router(other)
    return app


async def _pending(h: Harness, amount_kop: int = 20_000) -> Payment:
    payment = Payment(
        id=str(uuid.uuid4()),
        provider="cryptobot",
        provider_payment_id=f"inv-{uuid.uuid4().hex[:6]}",
        user_id=UID,
        amount_kop=amount_kop,
        status=PaymentStatus.PENDING,
    )
    async with h.ctx.session_factory.begin() as session:
        session.add(payment)
    return payment


async def test_webhook_credits_after_provider_check(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    payment = await _pending(h)
    h.gateway.webhook = WebhookResult(payment_ids=[payment.id], response_body="ok-body")
    h.gateway.status = PaymentStatus.SUCCEEDED
    transport = httpx.ASGITransport(app=_web_app(h))
    async with httpx.AsyncClient(transport=transport, base_url="https://bot.example.com") as client:
        resp = await client.post("/webhooks/cryptobot", content=b"{}")
        assert resp.status_code == 200 and resp.text == "ok-body"
        again = await client.post("/webhooks/cryptobot", content=b"{}")
        assert again.status_code == 200
        assert (await client.get("/webhooks/cryptobot")).status_code == 405
        assert (await client.post("/webhooks/yookassa", content=b"{}")).status_code == 404  # not enabled
        assert (await client.post("/webhooks/remnawave")).json() == {"panel": "ok"}  # not shadowed

        h.gateway.webhook = WebhookRejected("bad signature", 403)
        assert (await client.post("/webhooks/cryptobot", content=b"{}")).status_code == 403
    assert len(h.ctx.credited) == 1
    assert await balance(h.ctx, UID) == (20_000, 0)


async def test_query_webhooks_accept_get(money: Any) -> None:
    fk = FakeGateway(key="freekassa", webhook_query_fields=True)
    h: Harness = money(gateways=[fk])
    await register(h.ctx, UID)
    payment = await _pending(h)
    async with h.ctx.session_factory.begin() as session:
        stored = await session.get(Payment, payment.id)
        assert stored is not None
        stored.provider = "freekassa"
    fk.webhook = WebhookResult(payment_ids=[payment.id], response_body="YES")
    fk.status = PaymentStatus.SUCCEEDED
    transport = httpx.ASGITransport(app=_web_app(h))
    async with httpx.AsyncClient(transport=transport, base_url="https://bot.example.com") as client:
        resp = await client.get("/webhooks/freekassa?MERCHANT_ORDER_ID=1")
    assert resp.status_code == 200 and resp.text == "YES"
    assert await balance(h.ctx, UID) == (20_000, 0)


async def test_reconcile_job_credits_and_expires(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    paid = await _pending(h)
    h.gateway.status = PaymentStatus.SUCCEEDED
    await jobs.reconcile(h.ctx)  # type: ignore[arg-type]
    assert [r.payment_id for r in h.ctx.credited] == [paid.id]
    async with h.ctx.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Payment).where(Payment.status == "succeeded"))
    assert count == 1
