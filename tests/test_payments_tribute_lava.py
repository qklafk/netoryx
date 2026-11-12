"""Tribute and lava.top gateways against a local fake HTTP server."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest

from app.config import Settings
from app.db.models import PaymentStatus
from app.services.payments import lavatop, tribute
from app.services.payments.base import GatewayError, PaymentGateway, WebhookRejected
from app.services.payments.lavatop import LavaTopGateway
from app.services.payments.registry import build_gateways, close_gateways
from app.services.payments.tribute import TributeGateway
from tests.conftest import make_settings
from tests.test_payments_gateways import (  # noqa: F401  (fixtures)
    PAYMENT_ID,
    FakeProvider,
    _no_retry_delay,
    closing,
    fake,
    make_payment,
    make_user,
)

ORDER_UUID = "550e8400-e29b-41d4-a716-446655440000"
CONTRACT_ID = "7ea82675-4ded-4133-95a7-a6efbaf165cc"


# ---------------------------------------------------------------------------------------------
# Tribute
# ---------------------------------------------------------------------------------------------
def _order(**kw: Any) -> dict[str, Any]:
    return {
        "uuid": ORDER_UUID,
        "shopId": 1,
        "amount": 19950,
        "currency": "rub",
        "title": "Top-up",
        "description": "Top-up",
        "status": "pending",
        "successUrl": "https://t.me/TestBot",
        "failUrl": "https://t.me/TestBot",
        "paymentUrl": f"https://web.tribute.tg/shop/pay/{ORDER_UUID}",
        "webappPaymentUrl": "https://t.me/tribute/app?startapp=abc",
        "createdAt": "2025-11-13T15:04:05Z",
        "period": "onetime",
        **kw,
    }


@pytest.fixture
def trib(fake: FakeProvider, closing: list[PaymentGateway]) -> TributeGateway:  # noqa: F811
    gw = _no_retry_delay(
        TributeGateway("trib-key", shop_id=7, return_url="https://t.me/TestBot", base_url=fake.base_url)
    )
    closing.append(gw)
    return gw


async def test_tribute_create_order(trib: TributeGateway, fake: FakeProvider) -> None:  # noqa: F811
    fake.reply("POST", "/shop/orders", (200, _order()))
    invoice = await trib.create(make_payment(19950), make_user(receipt_email="a@b.example"), "Пополнение " + "x" * 400)
    assert invoice.provider_payment_id == ORDER_UUID
    assert invoice.url == f"https://web.tribute.tg/shop/pay/{ORDER_UUID}"
    assert invoice.provider_amount == "199.50" and invoice.provider_currency == "RUB"
    req = fake.requests[0]
    assert req.headers["Api-Key"] == "trib-key"
    body = req.json()
    assert body["amount"] == 19950 and body["currency"] == "rub" and body["period"] == "onetime"
    assert body["shopId"] == 7 and body["customerId"] == "5512345678" and body["email"] == "a@b.example"
    assert len(body["title"]) == 100 and len(body["description"]) == 300
    assert body["successUrl"] == body["failUrl"] == "https://t.me/TestBot"


async def test_tribute_create_rejects_mismatched_order(trib: TributeGateway, fake: FakeProvider) -> None:  # noqa: F811
    fake.reply("POST", "/shop/orders", (200, _order(amount=100)))
    with pytest.raises(GatewayError):
        await trib.create(make_payment(19950), make_user(), "topup")
    fake.reply("POST", "/shop/orders", (400, {"code": "error_amount_too_small", "message": "minimum"}))
    with pytest.raises(GatewayError):
        await trib.create(make_payment(50), make_user(), "topup")


async def test_tribute_create_without_https_return_url(fake: FakeProvider, closing: list[PaymentGateway]) -> None:  # noqa: F811
    gw = TributeGateway("k", return_url="http://insecure.example", base_url=fake.base_url)
    closing.append(gw)
    fake.reply("POST", "/shop/orders", (200, _order(paymentUrl=None)))
    invoice = await gw.create(make_payment(19950), make_user(), "")
    body = fake.requests[0].json()
    assert "successUrl" not in body and "shopId" not in body and "email" not in body
    assert body["title"] == "VPN"
    assert invoice.url == "https://t.me/tribute/app?startapp=abc"


async def test_tribute_fetch(trib: TributeGateway, fake: FakeProvider) -> None:  # noqa: F811
    path = f"/shop/orders/{ORDER_UUID}"
    fake.reply("GET", path, (200, _order(status="paid")))
    st = await trib.fetch(make_payment(19950, provider_payment_id=ORDER_UUID))
    assert st.status is PaymentStatus.SUCCEEDED and st.paid_amount_kop == 19950 and st.provider_payment_id == ORDER_UUID
    assert fake.requests[0].headers["Api-Key"] == "trib-key"
    fake.reply("GET", path, (200, _order(uuid="another")))
    with pytest.raises(GatewayError):
        await trib.fetch(make_payment(provider_payment_id=ORDER_UUID))
    with pytest.raises(GatewayError):
        await trib.fetch(make_payment())


def test_tribute_status_mapping() -> None:
    assert tribute.status(_order(status="pending")).status is PaymentStatus.PENDING
    assert tribute.status(_order(status="prepaid")).status is PaymentStatus.PENDING
    assert tribute.status(_order(status="failed")).status is PaymentStatus.CANCELED
    paid_usd = tribute.status(_order(status="paid", currency="usd"))
    assert paid_usd.status is PaymentStatus.SUCCEEDED and paid_usd.paid_amount_kop is None
    assert tribute.status(_order(status="paid", amount="199")).paid_amount_kop is None


async def test_tribute_webhook(trib: TributeGateway) -> None:
    raw = json.dumps(
        {
            "name": "shop_order",
            "created_at": "2025-03-20T01:15:58.33246Z",
            "sent_at": "2025-03-20T01:15:58.542279448Z",
            "payload": {
                "uuid": ORDER_UUID,
                "shopId": 1,
                "amount": 19950,
                "currency": "rub",
                "fee": 800,
                "status": "paid",
                "isRecurrent": False,
            },
        }
    ).encode()
    digest = hmac.new(b"trib-key", raw, hashlib.sha256)
    result = await trib.parse_webhook(raw, {"Trbt-Signature": digest.hexdigest()}, "1.2.3.4")
    assert result.provider_ids == [ORDER_UUID] and result.payment_ids == [] and result.trusted_status is None
    b64 = base64.b64encode(digest.digest()).decode()
    assert (await trib.parse_webhook(raw, {"trbt-signature": b64}, None)).provider_ids == [ORDER_UUID]
    assert tribute.sign(raw, "trib-key") == digest.hexdigest()

    for headers in (
        {},
        {"trbt-signature": "00" * 32},
        {"trbt-signature": tribute.sign(raw, "other")},
        {"trbt-signature": "not-hex!"},
    ):
        with pytest.raises(WebhookRejected) as exc:
            await trib.parse_webhook(raw, headers, None)
        assert exc.value.status_code == 401
    tampered = raw.replace(b"19950", b"99999")
    with pytest.raises(WebhookRejected):
        await trib.parse_webhook(tampered, {"trbt-signature": digest.hexdigest()}, None)


async def test_tribute_webhook_bodies(trib: TributeGateway) -> None:
    async def parse(body: bytes) -> Any:
        return await trib.parse_webhook(body, {"trbt-signature": tribute.sign(body, "trib-key")}, None)

    with pytest.raises(WebhookRejected) as exc:
        await parse(b"{not json")
    assert exc.value.status_code == 400
    with pytest.raises(WebhookRejected):
        await parse(b'{"name": "shop_order"}')
    other = json.dumps({"name": "new_subscription", "payload": {"subscription_id": 1}}).encode()
    assert (await parse(other)).provider_ids == []
    cancelled = json.dumps({"name": "shop_order_cancelled", "payload": {"uuid": ORDER_UUID}}).encode()
    assert (await parse(cancelled)).provider_ids == [ORDER_UUID]


# ---------------------------------------------------------------------------------------------
# lava.top
# ---------------------------------------------------------------------------------------------
OFFER = "836b9fc5-7ae9-4a27-9642-592bc44072b7"


@pytest.fixture
def lava(fake: FakeProvider, closing: list[PaymentGateway]) -> LavaTopGateway:  # noqa: F811
    gw = _no_retry_delay(
        LavaTopGateway(
            "lava-key",
            OFFER,
            "hook-key",
            email_domain="bot.example",
            return_url="https://t.me/TestBot",
            base_url=fake.base_url,
        )
    )
    closing.append(gw)
    return gw


def _invoice(**kw: Any) -> dict[str, Any]:
    return {
        "id": CONTRACT_ID,
        "type": "ONE_TIME",
        "datetime": "2024-02-05T09:38:27.33277Z",
        "status": "COMPLETED",
        "receipt": {"amount": 199.5, "currency": "RUB", "fee": 10.0},
        "buyer": {"email": "tg5512345678@bot.example"},
        **kw,
    }


async def test_lavatop_create(lava: LavaTopGateway, fake: FakeProvider) -> None:  # noqa: F811
    fake.reply(
        "POST",
        "/api/v3/invoice",
        (
            201,
            {
                "id": CONTRACT_ID,
                "status": "new",
                "amountTotal": {"currency": "RUB", "amount": 199},
                "paymentUrl": "https://app.lava.top/pay/x",
            },
        ),
    )
    invoice = await lava.create(make_payment(19950), make_user(), "topup")
    assert invoice.provider_payment_id == CONTRACT_ID and invoice.url == "https://app.lava.top/pay/x"
    assert invoice.provider_amount == "199.50" and invoice.provider_currency == "RUB"
    req = fake.requests[0]
    assert req.headers["X-Api-Key"] == "lava-key"
    body = req.json()
    assert body["offerId"] == OFFER and body["currency"] == "RUB" and body["amount"] == 199.5
    assert body["email"] == "tg5512345678@bot.example"
    assert body["successful_return_url"] == "https://t.me/TestBot"
    assert "paymentMethod" not in body and "paymentProvider" not in body

    await lava.create(make_payment(20000), make_user(receipt_email="me@mail.example"), "topup")
    body = fake.requests[1].json()
    assert body["email"] == "me@mail.example" and body["amount"] == 200


async def test_lavatop_create_sbp_and_errors(fake: FakeProvider, closing: list[PaymentGateway]) -> None:  # noqa: F811
    gw = LavaTopGateway("k", OFFER, "h", payment_method="sbp", base_url=fake.base_url)
    closing.append(gw)
    with pytest.raises(GatewayError):  # no buyer email and no domain to build one
        await gw.create(make_payment(), make_user(), "topup")
    fake.reply("POST", "/api/v3/invoice", (201, {"id": CONTRACT_ID, "paymentUrl": "https://pay"}))
    await gw.create(make_payment(), make_user(receipt_email="me@mail.example"), "topup")
    body = fake.requests[0].json()
    assert body["paymentProvider"] == "PAY2ME" and body["paymentMethod"] == "SBP"
    fake.reply("POST", "/api/v3/invoice", (404, {"error": "Product with offer id not found"}))
    with pytest.raises(GatewayError):
        await gw.create(make_payment(), make_user(receipt_email="me@mail.example"), "topup")
    fake.reply("POST", "/api/v3/invoice", (201, {"id": CONTRACT_ID, "paymentUrl": None}))
    with pytest.raises(GatewayError):
        await gw.create(make_payment(), make_user(receipt_email="me@mail.example"), "topup")


async def test_lavatop_fetch(lava: LavaTopGateway, fake: FakeProvider) -> None:  # noqa: F811
    path = f"/api/v1/invoices/{CONTRACT_ID}"
    fake.reply("GET", path, (200, _invoice()))
    st = await lava.fetch(make_payment(19950, provider_payment_id=CONTRACT_ID))
    assert st.status is PaymentStatus.SUCCEEDED and st.paid_amount_kop == 19950
    assert fake.requests[0].headers["X-Api-Key"] == "lava-key"
    fake.reply("GET", path, (200, _invoice(id="other")))
    with pytest.raises(GatewayError):
        await lava.fetch(make_payment(provider_payment_id=CONTRACT_ID))
    with pytest.raises(GatewayError):
        await lava.fetch(make_payment())


def test_lavatop_status_mapping() -> None:
    assert lavatop.status(_invoice(status="NEW")).status is PaymentStatus.PENDING
    assert lavatop.status(_invoice(status="IN_PROGRESS")).status is PaymentStatus.PENDING
    assert lavatop.status(_invoice(status="FAILED")).status is PaymentStatus.CANCELED
    assert lavatop.status(_invoice(status="cancelled")).status is PaymentStatus.CANCELED
    assert lavatop.status(_invoice(status="completed")).paid_amount_kop == 19950
    usd = lavatop.status(_invoice(receipt={"amount": 2.5, "currency": "USD"}))
    assert usd.status is PaymentStatus.SUCCEEDED and usd.paid_amount_kop is None
    assert lavatop.status(_invoice(receipt={"amount": "bad", "currency": "RUB"})).paid_amount_kop is None


async def test_lavatop_webhook(lava: LavaTopGateway) -> None:
    raw = json.dumps(
        {
            "eventType": "payment.success",
            "product": {"id": "d31384b8-e412-4be5-a2ec-297ae6666c8f", "title": "VPN"},
            "buyer": {"email": "tg5512345678@bot.example"},
            "contractId": CONTRACT_ID,
            "amount": 199.5,
            "currency": "RUB",
            "timestamp": "2024-02-05T09:38:27.33277Z",
            "status": "completed",
            "errorMessage": "",
        }
    ).encode()
    result = await lava.parse_webhook(raw, {"x-api-key": "hook-key"}, "1.2.3.4")
    assert result.provider_ids == [CONTRACT_ID] and result.trusted_status is None
    basic = "Basic " + base64.b64encode(b"hook-key").decode()
    assert (await lava.parse_webhook(raw, {"Authorization": basic}, None)).provider_ids == [CONTRACT_ID]

    bad_basic = "Basic " + base64.b64encode(b"wrong").decode()
    for headers in (
        {},
        {"X-Api-Key": "wrong"},
        {"X-Api-Key": "lava-key"},
        {"Authorization": bad_basic},
        {"Authorization": "Basic !!!"},
        {"Authorization": "Bearer hook-key"},
    ):
        with pytest.raises(WebhookRejected) as exc:
            await lava.parse_webhook(raw, headers, None)
        assert exc.value.status_code == 401
    for body in (b"[1]", b"{bad", b'{"eventType": "payment.success"}'):
        with pytest.raises(WebhookRejected) as exc:
            await lava.parse_webhook(body, {"X-Api-Key": "hook-key"}, None)
        assert exc.value.status_code == 400


def test_lavatop_requires_credentials() -> None:
    with pytest.raises(ValueError):
        LavaTopGateway("k", "", "h")
    with pytest.raises(ValueError):
        TributeGateway("")


# ---------------------------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------------------------
async def test_registry_builds_tribute_and_lavatop() -> None:
    settings = make_settings(
        tribute_enabled=True,
        tribute_api_key="t",
        lavatop_enabled=True,
        lavatop_api_key="l",
        lavatop_offer_id=OFFER,
        lavatop_webhook_secret="w",
        public_base_url="https://bot.example",
    )
    gateways = build_gateways(settings, 5, return_url="https://t.me/TestBot")
    try:
        assert isinstance(gateways["tribute"], TributeGateway)
        lava_gw = gateways["lavatop"]
        assert isinstance(lava_gw, LavaTopGateway)
        assert lava_gw._email_domain == "bot.example"
        for key in ("tribute", "lavatop"):
            assert gateways[key].key == key and gateways[key].title and gateways[key].min_amount_kop > 0
    finally:
        await close_gateways(gateways)


def test_settings_validation() -> None:
    with pytest.raises(ValueError):
        make_settings(tribute_enabled=True, public_base_url="https://bot.example")
    with pytest.raises(ValueError):
        make_settings(lavatop_enabled=True, lavatop_api_key="l", public_base_url="https://bot.example")
    with pytest.raises(ValueError):
        make_settings(
            lavatop_enabled=True,
            lavatop_api_key="l",
            lavatop_offer_id=OFFER,
            lavatop_webhook_secret="w",
            lavatop_payment_method="crypto",
            public_base_url="https://bot.example",
        )
    assert isinstance(make_settings(), Settings)
