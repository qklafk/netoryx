"""Buying a plan: plans list, plan card, payment from the wallet, provider invoices, Stars, e-mail for receipts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from aiogram.methods import SendInvoice
from sqlalchemy import func, select

from app.core.context import DevGateway
from app.core.modules import FeatureModule
from app.db.models import Payment, PaymentStatus, Purchase
from app.db.session import SessionFactory
from app.keyboards import Nav
from app.modules.core_user import module as core_user_module
from app.modules.subscription import module as subscription_module
from app.modules.subscription.callbacks import CheckCb, PayCb, PlanCb
from app.services.payments.base import GatewayError, ProviderStatus
from app.services.payments.stars import StarsGateway
from tests.test_mod_user_support import USER, Harness, add_plan, get_sub, get_user, make_harness, top_up

Make = Callable[..., Harness]
# Provider checkout needs the payments module on (it confirms the payments); a stand-in is enough here.
PAYMENTS = FeatureModule(name="payments", title_ru="Платежи", title_en="Payments", shop_only=True)
MODULES = [core_user_module, subscription_module, PAYMENTS]


@pytest.fixture
async def make(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Make]:
    harnesses: list[Harness] = []

    def factory(**settings: Any) -> Harness:
        harness = make_harness(session_factory, tmp_path / str(len(harnesses)), modules=MODULES, **settings)
        harnesses.append(harness)
        return harness

    yield factory
    for harness in harnesses:
        await harness.close()


class PollingGateway(DevGateway):
    """A provider whose status can be polled; ``paid`` decides what the provider reports."""

    key = "platega"
    supports_polling = True

    def __init__(self) -> None:
        super().__init__("https://bot.example.com")
        self.paid = False

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if self.paid:
            return ProviderStatus(PaymentStatus.SUCCEEDED, payment.amount_kop)
        return ProviderStatus(PaymentStatus.PENDING)


class BrokenGateway(DevGateway):
    key = "cryptobot"

    async def create(self, payment: Payment, user: Any, description: str) -> Any:
        raise GatewayError("down")


def _nonce(h: Harness, text_part: str) -> PayCb:
    data = h.button(text_part).callback_data
    return PayCb.unpack(data)


async def _purchases(sf: SessionFactory) -> int:
    async with sf() as session:
        return int(await session.scalar(select(func.count()).select_from(Purchase)) or 0)


async def test_plans_list_with_discounts(make: Make) -> None:
    h = make()
    await add_plan(h.sf, title="1 месяц", duration_days=30, price_kop=15_000)
    await add_plan(h.sf, title="3 месяца", duration_days=90, price_kop=40_000, sort_order=1)
    await h.send("/start")
    await h.press(Nav(to="buy"))
    text = h.last_text()
    assert "<b>1 месяц</b> — 150 ₽" in text
    assert "выгода 11%" in text and "133 ₽/мес" in text
    assert "3 месяца — 400 ₽ · −11%" in h.button_texts()


async def test_buy_from_balance_charges_once_per_tap(make: Make) -> None:
    h = make()
    plan_id = await add_plan(h.sf, title="Месяц", price_kop=15_000)
    await h.send("/start")
    await top_up(h.sf, USER, 20_000)
    await h.press(PlanCb(id=plan_id))
    pay = _nonce(h, "Оплатить с баланса")
    await h.press(pay)
    await h.press(pay)  # an impatient double tap
    assert (await get_user(h.sf)).balance_kop == 5_000
    assert await _purchases(h.sf) == 1
    sub = await get_sub(h.sf)
    assert sub is not None and sub.plan_id == plan_id
    assert "Подписка «Месяц» активна" in h.last_text()


async def test_not_enough_money_offers_providers_for_the_difference(make: Make) -> None:
    h = make()
    h.ctx.gateways.items["test"] = DevGateway("https://bot.example.com")
    plan_id = await add_plan(h.sf, title="Месяц", price_kop=15_000)
    await h.send("/start")
    await top_up(h.sf, USER, 5_000)
    await h.press(PlanCb(id=plan_id))
    assert "Не хватает <b>100 ₽</b>" in h.last_text()
    await h.press(_nonce(h, h.t("subscription.gw.test")))
    async with h.sf() as session:
        payment = await session.scalar(select(Payment))
    assert payment is not None
    assert payment.amount_kop == 10_000 and payment.plan_id == plan_id
    assert h.button(h.t("subscription.invoice.pay")).url == f"https://bot.example.com/dev/pay/{payment.id}"
    assert "Счёт на 100 ₽" in h.last_text()


async def test_stars_pays_the_full_price_with_an_invoice(make: Make) -> None:
    h = make(stars_enabled=True, stars_rub_per_star=1.5)
    h.ctx.gateways.items["stars"] = StarsGateway(h.ctx.settings)
    plan_id = await add_plan(h.sf, title="Месяц", price_kop=15_000, price_stars=99)
    await h.send("/start")
    await top_up(h.sf, USER, 5_000)
    await h.press(PlanCb(id=plan_id))
    assert any("99 ⭐️" in text for text in h.button_texts())
    await h.press(_nonce(h, "Telegram Stars"))
    invoices = h.api.of(SendInvoice)
    async with h.sf() as session:
        payment = await session.scalar(select(Payment))
    assert payment is not None and payment.amount_kop == 15_000 and payment.provider_amount == "99"
    assert invoices and invoices[0].payload == payment.id and invoices[0].prices[0].amount == 99
    assert "Счёт на подписку «Месяц» отправлен ниже" in h.last_text()


async def test_check_payment_polls_the_provider(make: Make) -> None:
    h = make()
    gateway = PollingGateway()
    h.ctx.gateways.items[gateway.key] = gateway
    plan_id = await add_plan(h.sf, price_kop=15_000)
    await h.send("/start")
    await h.press(PlanCb(id=plan_id))
    await h.press(_nonce(h, h.t("subscription.gw.platega")))
    async with h.sf() as session:
        payment = await session.scalar(select(Payment))
    assert payment is not None
    await h.press(CheckCb(id=payment.id))
    assert h.t("subscription.pay.not_yet") in h.popups()
    gateway.paid = True
    await h.press(CheckCb(id=payment.id))
    assert h.ctx.credited and h.ctx.credited[0].payment_id == payment.id
    assert "Оплата получена" in h.last_text()


async def test_someone_elses_payment_cannot_be_checked(make: Make) -> None:
    h = make()
    h.ctx.gateways.items["test"] = DevGateway("https://bot.example.com")
    plan_id = await add_plan(h.sf)
    await h.send("/start")
    await h.press(PlanCb(id=plan_id))
    await h.press(_nonce(h, h.t("subscription.gw.test")))
    async with h.sf() as session:
        payment = await session.scalar(select(Payment))
    assert payment is not None
    await h.send("/start", uid=7002)
    await h.press(CheckCb(id=payment.id), uid=7002)
    assert h.t("error.expired_button") in h.popups()


async def test_provider_failure_is_reported(make: Make) -> None:
    h = make()
    h.ctx.gateways.items["cryptobot"] = BrokenGateway("https://bot.example.com")
    plan_id = await add_plan(h.sf)
    await h.send("/start")
    await h.press(PlanCb(id=plan_id))
    await h.press(_nonce(h, h.t("subscription.gw.cryptobot")))
    assert h.t("subscription.pay.failed") in h.popups()
    async with h.sf() as session:
        payment = await session.scalar(select(Payment))
    assert payment is not None and payment.status == PaymentStatus.CANCELED


async def test_yookassa_receipt_asks_for_email(make: Make) -> None:
    h = make(
        yookassa_enabled=True,
        yookassa_shop_id="1",
        yookassa_secret_key="k",
        yookassa_receipt_enabled=True,
    )

    class Yoo(DevGateway):
        key = "yookassa"

    h.ctx.gateways.items["yookassa"] = Yoo("https://bot.example.com")
    plan_id = await add_plan(h.sf)
    await h.send("/start")
    await h.press(PlanCb(id=plan_id))
    await h.press(_nonce(h, h.t("subscription.gw.yookassa")))
    assert h.t("subscription.email.ask") in h.last_text()
    await h.send("not an email")
    assert h.t("subscription.email.invalid") in h.last_text()
    await h.send("ann@example.com")
    assert (await get_user(h.sf)).receipt_email == "ann@example.com"
    assert "Счёт на 150 ₽" in h.last_text()


async def test_buying_is_unavailable_in_personal_mode(make: Make) -> None:
    h = make(app_mode="personal")
    await h.send("/start", uid=1000)
    await h.press(Nav(to="buy"), uid=1000)
    assert h.t("subscription.plans.unavailable") in h.popups()
