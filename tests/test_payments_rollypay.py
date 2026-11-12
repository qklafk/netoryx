"""RollyPay gateway against a local fake HTTP server, plus callback signature verification."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.db.models import PaymentProvider, PaymentStatus
from app.services.payments.base import GatewayError, PaymentGateway, WebhookRejected
from app.services.payments.registry import build_gateways, close_gateways
from app.services.payments.rollypay import RollyPayGateway, payment_status, sign
from tests.conftest import make_settings
from tests.test_payments_gateways import PAYMENT_ID, TRUSTED, FakeProvider, make_payment, make_user

SECRET = "whsec_test"
ROLLY_ID = "9d4e0c1a-1111-4222-8333-444455556666"


@pytest.fixture
async def fake() -> AsyncIterator[FakeProvider]:
    provider = FakeProvider()
    app = web.Application()

    async def route(request: web.Request) -> web.Response:
        return await provider.handle(request)

    app.router.add_route("*", "/{tail:.*}", route)
    server = TestServer(app)
    await server.start_server()
    provider.base_url = str(server.make_url("")).rstrip("/")
    try:
        yield provider
    finally:
        await server.close()


@pytest.fixture
async def closing() -> AsyncIterator[list[PaymentGateway]]:
    gateways: list[PaymentGateway] = []
    yield gateways
    for gw in gateways:
        await gw.close()


def _gw(fake: FakeProvider, closing: list[PaymentGateway], **kw: Any) -> RollyPayGateway:
    gw = RollyPayGateway("key-1", SECRET, base_url=fake.base_url, **kw)
    gw._retry_delay = 0
    closing.append(gw)
    return gw


def _payment_obj(status: str, **kw: Any) -> dict[str, Any]:
    return {
        "payment_id": ROLLY_ID,
        "order_id": PAYMENT_ID,
        "status": status,
        "amount": "199.00",
        "payment_currency": "RUB",
        **kw,
    }


def _signed(
    body: dict[str, Any], *, secret: str = SECRET, timestamp: str = "1760000000"
) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    return raw, {"X-Signature": sign(secret, timestamp, raw), "X-Timestamp": timestamp}


def _callback(event: str = "payment.paid", status: str = "paid", **kw: Any) -> dict[str, Any]:
    return {
        "event_type": event,
        "payment_id": ROLLY_ID,
        "order_id": PAYMENT_ID,
        "status": status,
        "amount": "199.00",
        "currency": "RUB",
        "test": False,
        **kw,
    }


async def test_create_request_shape(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply(
        "POST", "/api/v1/payments", (200, {"payment_id": ROLLY_ID, "order_id": PAYMENT_ID, "pay_url": "https://pay/x"})
    )
    gw = _gw(fake, closing, payment_method="sbp", return_url="https://t.me/bot")
    invoice = await gw.create(make_payment(19950), make_user(), "VPN 30d")
    assert invoice.url == "https://pay/x"
    assert invoice.provider_payment_id == ROLLY_ID
    assert invoice.provider_amount == "199.50"
    req = fake.requests[0]
    assert req.headers["X-API-Key"] == "key-1"
    assert req.headers["X-Nonce"]
    assert req.json() == {
        "amount": "199.50",
        "payment_currency": "RUB",
        "order_id": PAYMENT_ID,
        "description": "VPN 30d",
        "payment_method": "sbp",
        "success_redirect_url": "https://t.me/bot",
        "fail_redirect_url": "https://t.me/bot",
    }


async def test_nonce_is_unique_per_request(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("GET", f"/api/v1/payments/{ROLLY_ID}", (200, _payment_obj("created")))
    gw = _gw(fake, closing)
    payment = make_payment(provider_payment_id=ROLLY_ID)
    await gw.fetch(payment)
    await gw.fetch(payment)
    assert fake.requests[0].headers["X-Nonce"] != fake.requests[1].headers["X-Nonce"]


async def test_create_without_method_and_wrapped_response(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("POST", "/api/v1/payments", (200, {"data": {"payment_id": ROLLY_ID, "pay_url": "https://pay/y"}}))
    gw = _gw(fake, closing)
    invoice = await gw.create(make_payment(), make_user(), "")
    assert invoice.url == "https://pay/y"
    body = fake.requests[0].json()
    assert "payment_method" not in body
    assert "success_redirect_url" not in body
    assert body["description"] == "VPN"


async def test_create_rejects_bad_answers(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _gw(fake, closing)
    fake.reply("POST", "/api/v1/payments", (400, {"error": "amount must be positive"}))
    with pytest.raises(GatewayError):
        await gw.create(make_payment(), make_user(), "x")
    fake.reply("POST", "/api/v1/payments", (200, {"payment_id": ROLLY_ID}))
    with pytest.raises(GatewayError):
        await gw.create(make_payment(), make_user(), "x")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("created", PaymentStatus.PENDING),
        ("processing", PaymentStatus.PENDING),
        ("paid", PaymentStatus.SUCCEEDED),
        ("expired", PaymentStatus.EXPIRED),
        ("canceled", PaymentStatus.CANCELED),
        ("refunded", PaymentStatus.REFUNDED),
        ("chargeback", PaymentStatus.REFUNDED),
    ],
)
async def test_fetch_status_mapping(
    fake: FakeProvider, closing: list[PaymentGateway], raw: str, expected: PaymentStatus
) -> None:
    fake.reply("GET", f"/api/v1/payments/{ROLLY_ID}", (200, _payment_obj(raw)))
    status = await _gw(fake, closing).fetch(make_payment(provider_payment_id=ROLLY_ID))
    assert status.status is expected
    assert status.paid_amount_kop == (19900 if expected is PaymentStatus.SUCCEEDED else None)


async def test_fetch_without_provider_id_is_pending(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    status = await _gw(fake, closing).fetch(make_payment())
    assert status.status is PaymentStatus.PENDING
    assert not fake.requests


def test_status_rejects_foreign_order_unknown_status_and_currency() -> None:
    with pytest.raises(GatewayError):
        payment_status(_payment_obj("paid", order_id="other"), PAYMENT_ID)
    with pytest.raises(GatewayError):
        payment_status(_payment_obj("weird"))
    with pytest.raises(GatewayError):
        payment_status(_payment_obj("paid", payment_currency="EUR"))
    with pytest.raises(GatewayError):
        payment_status([])


async def test_webhook_accepts_signed_callback(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    raw, headers = _signed(_callback())
    result = await _gw(fake, closing).parse_webhook(raw, headers, "203.0.113.5")
    assert result.payment_ids == [PAYMENT_ID]
    assert result.provider_ids == [ROLLY_ID]
    assert result.trusted_status is None  # the status is always re-fetched


async def test_webhook_header_names_are_case_insensitive(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    raw, headers = _signed(_callback())
    lowered = {k.lower(): v for k, v in headers.items()}
    assert (await _gw(fake, closing).parse_webhook(raw, lowered, None)).payment_ids == [PAYMENT_ID]


async def test_webhook_rejects_bad_signature_timestamp_or_missing_headers(
    fake: FakeProvider, closing: list[PaymentGateway]
) -> None:
    gw = _gw(fake, closing)
    raw, headers = _signed(_callback(), secret="wrong")
    with pytest.raises(WebhookRejected) as bad:
        await gw.parse_webhook(raw, headers, None)
    assert bad.value.status_code == 401
    raw, headers = _signed(_callback())
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw + b" ", headers, None)
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw, {**headers, "X-Timestamp": "1760000001"}, None)
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw, {"X-Signature": headers["X-Signature"]}, None)
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw, {"X-Timestamp": "1"}, None)


async def test_webhook_rejects_malformed_and_unreferenced_bodies(
    fake: FakeProvider, closing: list[PaymentGateway]
) -> None:
    gw = _gw(fake, closing)
    raw = b"not json"
    headers = {"X-Signature": sign(SECRET, "1", raw), "X-Timestamp": "1"}
    with pytest.raises(WebhookRejected) as malformed:
        await gw.parse_webhook(raw, headers, None)
    assert malformed.value.status_code == 400
    raw, headers = _signed({"event_type": "payment.paid", "status": "paid"})
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw, headers, None)


async def test_sandbox_callbacks_are_ignored(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _gw(fake, closing)
    raw, headers = _signed(_callback(test=True))
    assert (await gw.parse_webhook(raw, headers, None)).payment_ids == []
    raw, headers = _signed(_callback())
    assert (await gw.parse_webhook(raw, {**headers, "X-Test-Mode": "true"}, None)).payment_ids == []


def test_constructor_validation() -> None:
    with pytest.raises(ValueError):
        RollyPayGateway("", SECRET)
    with pytest.raises(ValueError):
        RollyPayGateway("k", "")
    with pytest.raises(ValueError):
        RollyPayGateway("k", SECRET, payment_method="crypto")


async def test_registry_builds_gateway_and_validates_settings() -> None:
    env: dict[str, Any] = {
        "rollypay_enabled": True,
        "rollypay_api_key": "k",
        "rollypay_signing_secret": "s",
        "rollypay_payment_method": "card",
        "public_base_url": "https://bot.example.com",
        "trusted_proxies": TRUSTED,
    }
    gateways = build_gateways(make_settings(**env))
    try:
        assert gateways[PaymentProvider.ROLLYPAY.value].supports_polling
    finally:
        await close_gateways(gateways)
    for broken in (
        {"rollypay_api_key": None},
        {"rollypay_signing_secret": None},
        {"rollypay_payment_method": "crypto"},
    ):
        with pytest.raises(ValueError):
            make_settings(**{**env, **broken})
    with pytest.raises(ValueError):
        make_settings(**{**env, "public_base_url": None})
