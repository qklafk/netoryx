"""Provider gateways against local fake HTTP servers, plus webhook verification vectors."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.db.models import Payment, PaymentStatus, User
from app.services.payments import cryptobot, freekassa, heleket, plisio, robokassa, yoomoney
from app.services.payments.base import GatewayError, PaymentGateway, WebhookRejected
from app.services.payments.cryptobot import CryptoBotGateway
from app.services.payments.freekassa import FreeKassaGateway
from app.services.payments.heleket import HeleketGateway
from app.services.payments.manual import ManualTransferGateway
from app.services.payments.nowpayments import NowPaymentsGateway
from app.services.payments.nowpayments import signature as nowpayments_signature
from app.services.payments.platega import PlategaGateway
from app.services.payments.plisio import PlisioGateway
from app.services.payments.registry import build_gateways, close_gateways
from app.services.payments.robokassa import RobokassaGateway
from app.services.payments.stars import StarsGateway
from app.services.payments.yookassa import YooKassaGateway
from app.services.payments.yoomoney import YooMoneyGateway
from tests.conftest import make_settings

PAYMENT_ID = "0b9c6a8e-6a4e-4a8f-9a59-1f1f7d0e2c11"
TRUSTED = ["127.0.0.1", "::1", "10.0.0.0/8", "172.16.0.0/12"]


def make_payment(amount_kop: int = 19900, provider_payment_id: str | None = None, **kw: Any) -> Payment:
    return Payment(
        id=PAYMENT_ID,
        provider="test",
        user_id=5512345678,
        amount_kop=amount_kop,
        provider_payment_id=provider_payment_id,
        status=PaymentStatus.CREATED,
        **kw,
    )


def make_user(**kw: Any) -> User:
    return User(tg_id=5512345678, referral_code="R", sub_token="T", **kw)


# ---------------------------------------------------------------------------------------------
# Fake provider HTTP server
# ---------------------------------------------------------------------------------------------
@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


Reply = tuple[int, Any] | Callable[[Recorded], tuple[int, Any]]


@dataclass
class FakeProvider:
    """Answers ``(method, path)`` with queued replies (the last one repeats) and records requests."""

    base_url: str = ""
    routes: dict[tuple[str, str], list[Reply]] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)

    def reply(self, method: str, path: str, *replies: Reply) -> None:
        self.routes[(method, path)] = list(replies)

    async def handle(self, request: web.Request) -> web.Response:
        rec = Recorded(request.method, request.path, dict(request.query), dict(request.headers), await request.read())
        self.requests.append(rec)
        queue = self.routes.get((request.method, request.path))
        if not queue:
            return web.json_response({"error": "no route"}, status=404)
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        status, body = reply(rec) if callable(reply) else reply
        if isinstance(body, str):
            return web.Response(status=status, text=body)
        return web.json_response(body, status=status)


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
    """Gateways appended here are closed after the test."""
    gateways: list[PaymentGateway] = []
    yield gateways
    for gw in gateways:
        await gw.close()


def _no_retry_delay(gw: Any) -> Any:
    gw._retry_delay = 0
    return gw


# ---------------------------------------------------------------------------------------------
# CryptoBot
# ---------------------------------------------------------------------------------------------
CB_TOKEN = "12345:AAtestTOKEN"
CB_INVOICE = {
    "invoice_id": 777,
    "status": "active",
    "bot_invoice_url": "https://t.me/CryptoBot?start=IVabc",
    "mini_app_invoice_url": "https://t.me/CryptoBot/app?startapp=invoice-IVabc",
    "currency_type": "fiat",
    "fiat": "RUB",
    "amount": "199",
    "payload": PAYMENT_ID,
}


@pytest.fixture
def cb(fake: FakeProvider, closing: list[PaymentGateway]) -> CryptoBotGateway:
    gw = _no_retry_delay(CryptoBotGateway(CB_TOKEN, base_url=fake.base_url + "/api", accepted_assets="usdt, ton"))
    closing.append(gw)
    return gw


async def test_cryptobot_create(cb: CryptoBotGateway, fake: FakeProvider) -> None:
    fake.reply("POST", "/api/createInvoice", (200, {"ok": True, "result": CB_INVOICE}))
    invoice = await cb.create(make_payment(19950), make_user(), "Пополнение баланса")
    assert invoice.provider_payment_id == "777"
    assert invoice.url == CB_INVOICE["bot_invoice_url"]
    assert invoice.provider_currency == "RUB"
    req = fake.requests[0]
    assert req.headers["Crypto-Pay-API-Token"] == CB_TOKEN
    body = req.json()
    assert body["currency_type"] == "fiat"
    assert body["fiat"] == "RUB"
    assert body["amount"] == "199.5"
    assert body["payload"] == PAYMENT_ID
    assert body["accepted_assets"] == "USDT,TON"
    assert body["expires_in"] == 24 * 3600


async def test_cryptobot_create_api_error_is_not_retried(cb: CryptoBotGateway, fake: FakeProvider) -> None:
    fake.reply("POST", "/api/createInvoice", (400, {"ok": False, "error": {"code": 400, "name": "AMOUNT_TOO_SMALL"}}))
    with pytest.raises(GatewayError, match="AMOUNT_TOO_SMALL"):
        await cb.create(make_payment(), make_user(), "x")
    fake.reply("POST", "/api/createInvoice", (502, "bad gateway"))
    with pytest.raises(GatewayError):
        await cb.create(make_payment(), make_user(), "x")
    assert len(fake.requests) == 2  # one attempt each


@pytest.mark.parametrize(
    ("invoice", "status", "paid"),
    [
        ({**CB_INVOICE, "status": "paid", "paid_asset": "USDT"}, PaymentStatus.SUCCEEDED, 19900),
        ({**CB_INVOICE, "status": "paid", "fiat": "USD"}, PaymentStatus.SUCCEEDED, None),
        ({**CB_INVOICE, "status": "active"}, PaymentStatus.PENDING, None),
        ({**CB_INVOICE, "status": "expired"}, PaymentStatus.EXPIRED, None),
    ],
)
async def test_cryptobot_fetch(
    cb: CryptoBotGateway, fake: FakeProvider, invoice: dict[str, Any], status: PaymentStatus, paid: int | None
) -> None:
    fake.reply("POST", "/api/getInvoices", (200, {"ok": True, "result": {"items": [invoice]}}))
    result = await cb.fetch(make_payment(provider_payment_id="777"))
    assert (result.status, result.paid_amount_kop, result.provider_payment_id) == (status, paid, "777")
    assert fake.requests[0].json()["invoice_ids"] == "777"


async def test_cryptobot_fetch_retries_and_missing(cb: CryptoBotGateway, fake: FakeProvider) -> None:
    fake.reply(
        "POST",
        "/api/getInvoices",
        (503, "busy"),
        (200, {"ok": True, "result": {"items": [{**CB_INVOICE, "status": "paid"}]}}),
    )
    assert (await cb.fetch(make_payment(provider_payment_id="777"))).status is PaymentStatus.SUCCEEDED
    assert len(fake.requests) == 2
    fake.reply("POST", "/api/getInvoices", (200, {"ok": True, "result": {"items": []}}))
    with pytest.raises(GatewayError, match="not found"):
        await cb.fetch(make_payment(provider_payment_id="777"))
    with pytest.raises(GatewayError):
        await cb.fetch(make_payment(provider_payment_id=None))


def test_cryptobot_signature_vector() -> None:
    # hex(HMAC_SHA256(key=SHA256(token), msg=body)), computed independently
    body = b'{"update_id":1,"update_type":"invoice_paid"}'
    expected = "46d8bd4f06c910c4e09cae0dfc2e9c213206bbed0f64008ff7542cf2f54a02d7"
    assert cryptobot.sign_body(body, CB_TOKEN) == expected
    assert cryptobot.verify_signature(body, expected, CB_TOKEN)
    assert cryptobot.verify_signature(body, expected.upper(), CB_TOKEN)
    assert not cryptobot.verify_signature(body + b" ", expected, CB_TOKEN)
    assert not cryptobot.verify_signature(body, expected, CB_TOKEN + "x")
    assert not cryptobot.verify_signature(body, None, CB_TOKEN)
    assert not cryptobot.verify_signature(body, "", CB_TOKEN)


async def test_cryptobot_webhook(cb: CryptoBotGateway) -> None:
    raw = json.dumps(
        {"update_id": 5, "update_type": "invoice_paid", "request_date": "2026-11-04T00:00:00Z", "payload": CB_INVOICE}
    ).encode()
    sig = cryptobot.sign_body(raw, CB_TOKEN)
    result = await cb.parse_webhook(raw, {"Crypto-Pay-Api-Signature": sig}, "1.2.3.4")
    assert result.payment_ids == [PAYMENT_ID]
    assert result.provider_ids == ["777"]
    assert result.trusted_status is None  # must be re-fetched

    with pytest.raises(WebhookRejected) as exc:
        await cb.parse_webhook(raw, {"crypto-pay-api-signature": "0" * 64}, None)
    assert exc.value.status_code == 401
    with pytest.raises(WebhookRejected):
        await cb.parse_webhook(raw, {}, None)

    other = json.dumps({"update_id": 6, "update_type": "something_else", "payload": {}}).encode()
    result = await cb.parse_webhook(other, {"crypto-pay-api-signature": cryptobot.sign_body(other, CB_TOKEN)}, None)
    assert result.payment_ids == [] and result.provider_ids == []


def test_cryptobot_testnet_and_expiry_bounds() -> None:
    assert CryptoBotGateway("t", testnet=True)._base_url == cryptobot.TESTNET_URL
    assert CryptoBotGateway("t")._base_url == cryptobot.MAINNET_URL
    assert CryptoBotGateway("t", expires_in_sec=10**9)._expires_in == cryptobot.MAX_EXPIRES_IN
    assert CryptoBotGateway("t", expires_in_sec=0)._expires_in == 1


# ---------------------------------------------------------------------------------------------
# Platega
# ---------------------------------------------------------------------------------------------
PG_MERCHANT = "11111111-2222-3333-4444-555555555555"
PG_SECRET = "pg-secret"


def _platega(fake: FakeProvider, closing: list[PaymentGateway], **kw: Any) -> PlategaGateway:
    gw = _no_retry_delay(PlategaGateway(PG_MERCHANT, PG_SECRET, base_url=fake.base_url, **kw))
    closing.append(gw)
    return gw


async def test_platega_create_v2(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _platega(fake, closing, return_url="https://t.me/TestBot")
    fake.reply(
        "POST",
        "/v2/transaction/process",
        (200, {"transactionId": "tx-1", "status": "PENDING", "url": "https://pay.platega.io/tx-1"}),
    )
    invoice = await gw.create(make_payment(19900), make_user(), "VPN")
    assert (invoice.provider_payment_id, invoice.url) == ("tx-1", "https://pay.platega.io/tx-1")
    req = fake.requests[0]
    assert req.headers["X-MerchantId"] == PG_MERCHANT
    assert req.headers["X-Secret"] == PG_SECRET
    body = req.json()
    assert body["paymentDetails"] == {"amount": 199, "currency": "RUB"}
    assert body["payload"] == PAYMENT_ID
    assert body["metadata"] == {"user_email": "5512345678@telegram.org", "userId": "5512345678"}
    assert body["return"] == body["failedUrl"] == "https://t.me/TestBot"
    assert "paymentMethod" not in body


async def test_platega_create_v1_fixed_method(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _platega(fake, closing, payment_method=2)
    fake.reply("POST", "/transaction/process", (200, {"transactionId": "tx-2", "redirect": "https://r/tx-2"}))
    invoice = await gw.create(make_payment(19950), make_user(), "VPN")
    assert invoice.url == "https://r/tx-2"
    body = fake.requests[0].json()
    assert body["paymentMethod"] == 2
    assert body["paymentDetails"]["amount"] == 199.5
    assert body["return"] == body["failedUrl"] == "https://t.me"  # required by the API: default


async def test_platega_create_errors(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _platega(fake, closing)
    fake.reply("POST", "/v2/transaction/process", (401, {"code": "Auth:GEN_1001"}))
    with pytest.raises(GatewayError, match="401"):
        await gw.create(make_payment(), make_user(), "VPN")
    fake.reply("POST", "/v2/transaction/process", (200, {"transactionId": "tx"}))
    with pytest.raises(GatewayError, match="URL"):
        await gw.create(make_payment(), make_user(), "VPN")
    fake.reply("POST", "/v2/transaction/process", (200, {"url": "https://x"}))
    with pytest.raises(GatewayError, match="transactionId"):
        await gw.create(make_payment(), make_user(), "VPN")


@pytest.mark.parametrize(
    ("body", "status", "paid"),
    [
        ({"status": "CONFIRMED", "paymentDetails": {"amount": 199, "currency": "RUB"}}, PaymentStatus.SUCCEEDED, 19900),
        ({"status": "CONFIRMED", "paymentDetails": {"amount": 2.5, "currency": "USDT"}}, PaymentStatus.SUCCEEDED, None),
        ({"status": "PENDING"}, PaymentStatus.PENDING, None),
        ({"status": "CANCELED"}, PaymentStatus.CANCELED, None),
        ({"status": "CHARGEBACKED"}, PaymentStatus.REFUNDED, None),
    ],
)
async def test_platega_fetch(
    fake: FakeProvider, closing: list[PaymentGateway], body: dict[str, Any], status: PaymentStatus, paid: int | None
) -> None:
    gw = _platega(fake, closing)
    fake.reply("GET", "/transaction/tx-1", (200, {"id": "tx-1", **body}))
    result = await gw.fetch(make_payment(provider_payment_id="tx-1"))
    assert (result.status, result.paid_amount_kop, result.provider_payment_id) == (status, paid, "tx-1")
    assert fake.requests[0].headers["X-Secret"] == PG_SECRET


async def test_platega_fetch_unknown_status(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _platega(fake, closing)
    fake.reply("GET", "/transaction/tx-1", (200, {"status": "WEIRD"}))
    with pytest.raises(GatewayError, match="WEIRD"):
        await gw.fetch(make_payment(provider_payment_id="tx-1"))
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment())


async def test_platega_webhook_header_check(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _platega(fake, closing)
    raw = json.dumps(
        {
            "id": "tx-1",
            "amount": 199,
            "currency": "RUB",
            "status": "CONFIRMED",
            "paymentMethod": 2,
            "payload": PAYMENT_ID,
        }
    ).encode()
    good = {"X-MerchantId": PG_MERCHANT, "X-Secret": PG_SECRET, "User-Agent": "Platega Payment"}
    result = await gw.parse_webhook(raw, good, "5.6.7.8")
    assert (result.payment_ids, result.provider_ids, result.trusted_status) == ([PAYMENT_ID], ["tx-1"], None)
    lower = {k.lower(): v for k, v in good.items()}
    assert (await gw.parse_webhook(raw, lower, None)).provider_ids == ["tx-1"]

    for headers in (
        {**good, "X-Secret": "wrong"},
        {**good, "X-MerchantId": "other"},
        {"X-MerchantId": PG_MERCHANT},
        {},
    ):
        with pytest.raises(WebhookRejected) as exc:
            await gw.parse_webhook(raw, headers, None)
        assert exc.value.status_code == 401
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(b"{oops", good, None)
    assert exc.value.status_code == 400


async def test_platega_webhook_undecodable_header_is_rejected(
    fake: FakeProvider, closing: list[PaymentGateway]
) -> None:
    # aiohttp decodes header bytes that are not valid UTF-8 into lone surrogates.
    gw = _platega(fake, closing)
    garbage = b"\xff".decode("utf-8", "surrogateescape")
    good = {"X-MerchantId": PG_MERCHANT, "X-Secret": PG_SECRET}
    for headers in ({**good, "X-Secret": garbage}, {**good, "X-MerchantId": garbage}):
        with pytest.raises(WebhookRejected) as exc:
            await gw.parse_webhook(b"{}", headers, None)
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------------------------
# YooKassa
# ---------------------------------------------------------------------------------------------
YK_PAYMENT = {
    "id": "2d5b2c5e-000f-5000-9000-1b2a3c4d5e6f",
    "status": "pending",
    "paid": False,
    "amount": {"value": "199.00", "currency": "RUB"},
    "confirmation": {"type": "redirect", "confirmation_url": "https://yoomoney.ru/api-pages/v2/payment-confirm/epl?x"},
    "metadata": {"payment_id": PAYMENT_ID},
}


def _yookassa(fake: FakeProvider | None, closing: list[PaymentGateway], **kw: Any) -> YooKassaGateway:
    base = fake.base_url + "/v3" if fake else "http://unused"
    gw = _no_retry_delay(YooKassaGateway("123456", "live_secret", base_url=base, trusted_proxies=TRUSTED, **kw))
    closing.append(gw)
    return gw


async def test_yookassa_create(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _yookassa(fake, closing, return_url="https://t.me/TestBot")
    fake.reply("POST", "/v3/payments", (200, YK_PAYMENT))
    invoice = await gw.create(make_payment(19900), make_user(), "x" * 200)
    assert invoice.provider_payment_id == YK_PAYMENT["id"]
    assert invoice.url == YK_PAYMENT["confirmation"]["confirmation_url"]  # type: ignore[index]
    assert invoice.provider_amount == "199.00"
    req = fake.requests[0]
    assert req.headers["Authorization"] == "Basic " + base64.b64encode(b"123456:live_secret").decode()
    assert req.headers["Idempotence-Key"] == PAYMENT_ID
    body = req.json()
    assert body["amount"] == {"value": "199.00", "currency": "RUB"}
    assert body["capture"] is True
    assert body["confirmation"] == {"type": "redirect", "return_url": "https://t.me/TestBot"}
    assert len(body["description"]) == 128
    assert body["metadata"] == {"payment_id": PAYMENT_ID, "user_id": "5512345678"}
    assert "receipt" not in body


async def test_yookassa_create_retries_with_same_idempotence_key(
    fake: FakeProvider, closing: list[PaymentGateway]
) -> None:
    gw = _yookassa(fake, closing)
    fake.reply("POST", "/v3/payments", (500, {"type": "error"}), (200, YK_PAYMENT))
    await gw.create(make_payment(), make_user(), "VPN")
    assert len(fake.requests) == 2
    assert {r.headers["Idempotence-Key"] for r in fake.requests} == {PAYMENT_ID}
    assert fake.requests[0].json()["confirmation"]["return_url"] == "https://t.me"


async def test_yookassa_receipt(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _yookassa(fake, closing, receipt_enabled=True, vat_code=4)
    fake.reply("POST", "/v3/payments", (200, YK_PAYMENT))
    await gw.create(make_payment(), make_user(receipt_email="u@example.com"), "Доступ к сервису")
    receipt = fake.requests[0].json()["receipt"]
    assert receipt["customer"] == {"email": "u@example.com"}
    item = receipt["items"][0]
    assert item["amount"] == {"value": "199.00", "currency": "RUB"}
    assert (item["vat_code"], item["payment_mode"], item["payment_subject"], item["quantity"]) == (
        4,
        "full_payment",
        "service",
        "1.00",
    )

    with pytest.raises(GatewayError, match="e-mail"):
        await gw.create(make_payment(), make_user(), "x")
    assert len(fake.requests) == 1  # nothing sent without an e-mail

    fixed = _yookassa(fake, closing, receipt_enabled=True, receipt_email="shop@example.com")
    await fixed.create(make_payment(), make_user(receipt_email="u@example.com"), "x")
    assert fake.requests[1].json()["receipt"]["customer"] == {"email": "shop@example.com"}


@pytest.mark.parametrize(
    ("body", "status", "paid"),
    [
        ({"status": "succeeded", "paid": True}, PaymentStatus.SUCCEEDED, 19900),
        ({"status": "succeeded", "amount": {"value": "2.00", "currency": "USD"}}, PaymentStatus.SUCCEEDED, None),
        ({"status": "waiting_for_capture"}, PaymentStatus.PENDING, None),
        ({"status": "pending"}, PaymentStatus.PENDING, None),
        ({"status": "canceled"}, PaymentStatus.CANCELED, None),
    ],
)
async def test_yookassa_fetch(
    fake: FakeProvider, closing: list[PaymentGateway], body: dict[str, Any], status: PaymentStatus, paid: int | None
) -> None:
    gw = _yookassa(fake, closing)
    pid = YK_PAYMENT["id"]
    fake.reply("GET", f"/v3/payments/{pid}", (200, {**YK_PAYMENT, **body}))
    result = await gw.fetch(make_payment(provider_payment_id=str(pid)))
    assert (result.status, result.paid_amount_kop, result.provider_payment_id) == (status, paid, pid)


async def test_yookassa_fetch_errors(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _yookassa(fake, closing)
    fake.reply("GET", "/v3/payments/x", (200, {"id": "x", "status": "mystery"}))
    with pytest.raises(GatewayError, match="mystery"):
        await gw.fetch(make_payment(provider_payment_id="x"))
    fake.reply("GET", "/v3/payments/y", (401, {"type": "error", "code": "invalid_credentials"}))
    with pytest.raises(GatewayError, match="401"):
        await gw.fetch(make_payment(provider_payment_id="y"))
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment())


YK_EVENT = json.dumps({"type": "notification", "event": "payment.succeeded", "object": YK_PAYMENT}).encode()


@pytest.mark.parametrize(
    ("remote", "headers"),
    [
        ("185.71.76.1", {}),
        ("185.71.76.31", {}),
        ("185.71.77.10", {}),
        ("77.75.153.127", {}),
        ("77.75.156.11", {}),
        ("77.75.156.35", {}),
        ("77.75.154.200", {}),
        ("2a02:5180::1", {}),
        ("2a02:5180:ffff:ffff::1", {}),
        ("127.0.0.1", {"X-Forwarded-For": "185.71.76.5"}),
        ("10.1.2.3", {"X-Forwarded-For": "2a02:5180::10, 172.17.0.1"}),
        ("::1", {"X-Real-IP": "77.75.156.35"}),
    ],
)
async def test_yookassa_webhook_allowed_sources(
    closing: list[PaymentGateway], remote: str, headers: dict[str, str]
) -> None:
    gw = _yookassa(None, closing)
    result = await gw.parse_webhook(YK_EVENT, headers, remote)
    assert result.provider_ids == [YK_PAYMENT["id"]]
    assert result.payment_ids == [PAYMENT_ID]
    assert result.trusted_status is None  # always re-fetched


@pytest.mark.parametrize(
    ("remote", "headers"),
    [
        ("1.2.3.4", {}),
        ("185.71.76.32", {}),  # just outside 185.71.76.0/27
        ("77.75.156.12", {}),
        ("2a02:5181::1", {}),
        (None, {}),
        # X-Forwarded-For from an untrusted peer is ignored
        ("8.8.8.8", {"X-Forwarded-For": "185.71.76.5"}),
        # a spoofed left entry does not help: the rightmost untrusted hop is the client
        ("127.0.0.1", {"X-Forwarded-For": "185.71.76.5, 6.6.6.6"}),
        ("127.0.0.1", {}),  # the proxy itself is not YooKassa
    ],
)
async def test_yookassa_webhook_rejected_sources(
    closing: list[PaymentGateway], remote: str | None, headers: dict[str, str]
) -> None:
    gw = _yookassa(None, closing)
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(YK_EVENT, headers, remote)
    assert exc.value.status_code == 403


async def test_yookassa_webhook_ip_check_disabled_and_other_events(closing: list[PaymentGateway]) -> None:
    gw = _yookassa(None, closing, check_ip=False)
    assert (await gw.parse_webhook(YK_EVENT, {}, "1.2.3.4")).provider_ids == [YK_PAYMENT["id"]]
    refund = json.dumps({"event": "refund.succeeded", "object": {"id": "r1", "payment_id": "p1"}}).encode()
    assert (await gw.parse_webhook(refund, {}, None)).provider_ids == []
    no_meta = json.dumps({"event": "payment.canceled", "object": {"id": "p2"}}).encode()
    result = await gw.parse_webhook(no_meta, {}, None)
    assert (result.payment_ids, result.provider_ids) == ([], ["p2"])
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(b"<html>", {}, None)


# ---------------------------------------------------------------------------------------------
# YooMoney
# ---------------------------------------------------------------------------------------------
# Official example: https://yoomoney.ru/docs/payment-buttons/using-api/notifications (key "secret123")
YM_OFFICIAL = {
    "notification_type": "p2p-incoming",
    "operation_id": "441361714955017004",
    "amount": "98.00",
    "withdraw_amount": "100.00",
    "currency": "643",
    "datetime": "2013-12-26T08:28:34Z",
    "sender": "41000000000",
    "codepro": "false",
    "label": "ML23045",
    "unaccepted": "false",
    "sha1_hash": "ac13833bd6ba9eff1fa9e4bed76f3d6ebb57f6c0",
    "sign": "a452af731650e2c5b39abcdc7c28dd27db7b3b654c2230ad2c386e64afb98605",
}
YM_SECRET = "secret123"


def _signed_form(fields: dict[str, str], secret: str = YM_SECRET) -> bytes:
    data = {k: v for k, v in fields.items() if k != "sign"}
    data["sign"] = yoomoney.notification_sign(data, secret)
    return urlencode(data).encode()


def test_yoomoney_official_signature_vector() -> None:
    assert yoomoney.notification_sign(YM_OFFICIAL, YM_SECRET) == YM_OFFICIAL["sign"]
    assert yoomoney.verify_notification(YM_OFFICIAL, YM_SECRET)
    assert not yoomoney.verify_notification({**YM_OFFICIAL, "amount": "99.00"}, YM_SECRET)
    assert not yoomoney.verify_notification(YM_OFFICIAL, "secret124")
    assert not yoomoney.verify_notification({k: v for k, v in YM_OFFICIAL.items() if k != "sign"}, YM_SECRET)


async def test_yoomoney_webhook_official_example() -> None:
    gw = YooMoneyGateway("4100111", YM_SECRET)
    raw = urlencode(YM_OFFICIAL).encode()  # form-encoded exactly as YooMoney posts it
    result = await gw.parse_webhook(raw, {"Content-Type": "application/x-www-form-urlencoded"}, "1.2.3.4")
    assert result.payment_ids == ["ML23045"]
    assert result.provider_ids == ["441361714955017004"]
    assert result.trusted_status is not None
    assert result.trusted_status.status is PaymentStatus.SUCCEEDED
    assert result.trusted_status.paid_amount_kop == 10000  # withdraw_amount: what the payer paid
    assert result.trusted_status.provider_payment_id == "441361714955017004"


async def test_yoomoney_webhook_variants() -> None:
    gw = YooMoneyGateway("4100111", YM_SECRET)
    base = {k: v for k, v in YM_OFFICIAL.items() if k not in ("sign", "sha1_hash")}

    held = await gw.parse_webhook(_signed_form({**base, "unaccepted": "true"}), {}, None)
    assert held.trusted_status is not None and held.trusted_status.status is PaymentStatus.PENDING

    card = {**base, "notification_type": "card-incoming", "sender": "", "withdraw_amount": ""}
    result = await gw.parse_webhook(_signed_form(card), {}, None)
    assert result.trusted_status is not None and result.trusted_status.paid_amount_kop == 9800  # falls back to amount

    test_ping = await gw.parse_webhook(_signed_form({**base, "label": ""}), {}, None)
    assert test_ping.payment_ids == [] and test_ping.trusted_status is None

    usd = await gw.parse_webhook(_signed_form({**base, "currency": "840"}), {}, None)
    assert usd.payment_ids == []

    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(_signed_form(base, "wrong"), {}, None)
    assert exc.value.status_code == 401
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(urlencode(base).encode(), {}, None)  # unsigned (sha1_hash is not accepted)
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(b"\xff\xfe", {}, None)
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(b"", {}, None)


async def test_yoomoney_create_link_and_no_polling() -> None:
    gw = YooMoneyGateway("4100111", YM_SECRET, payment_type="PC", return_url="https://t.me/TestBot")
    assert gw.title == "👛 Кошелёк ЮMoney"
    assert YooMoneyGateway("1", "s").title.startswith("💳")
    invoice = await gw.create(make_payment(15050), make_user(), "Пополнение баланса")
    parts = urlsplit(invoice.url or "")
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == yoomoney.QUICKPAY_URL
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert query == {
        "receiver": "4100111",
        "quickpay-form": "button",
        "paymentType": "PC",
        "sum": "150.50",
        "label": PAYMENT_ID,
        "targets": "Пополнение баланса",
        "successURL": "https://t.me/TestBot",
    }
    assert invoice.provider_payment_id is None and invoice.provider_amount == "150.50"
    assert not gw.supports_polling
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment())
    long_id = make_payment()
    long_id.id = "x" * 65
    with pytest.raises(GatewayError):
        await gw.create(long_id, make_user(), "x")
    await gw.close()


# ---------------------------------------------------------------------------------------------
# FreeKassa
# ---------------------------------------------------------------------------------------------
FK_SHOP, FK_S1, FK_S2, FK_API = "7012", "secret", "second-secret", "apikey"
FK_IP = "168.119.157.136"


def _fk(closing: list[PaymentGateway], fake: FakeProvider | None = None, **kw: Any) -> FreeKassaGateway:
    if fake is not None:
        kw.setdefault("api_url", fake.base_url + "/v1")
    gw = FreeKassaGateway(FK_SHOP, FK_S1, FK_S2, trusted_proxies=TRUSTED, **kw)
    closing.append(gw)
    return gw


def _fk_notification(
    amount: str = "199.00", order: str = PAYMENT_ID, secret: str = FK_S2, shop: str = FK_SHOP
) -> bytes:
    fields = {
        "MERCHANT_ID": shop,
        "AMOUNT": amount,
        "intid": "98765",
        "MERCHANT_ORDER_ID": order,
        "P_EMAIL": "u@example.com",
        "CUR_ID": "1",
        "SIGN": freekassa.notification_sign(shop, amount, secret, order),
    }
    return urlencode(fields).encode()


def test_freekassa_signature_vectors() -> None:
    # Official example: md5('7012:100.11:secret:RUB:154')
    assert freekassa.form_sign("7012", "100.11", "secret", "RUB", "154") == "64d0581f4a08af485a619950e023696a"
    assert freekassa.notification_sign("7012", "100.11", "s2", "154") == freekassa._md5("7012:100.11:s2:154")
    # API: HMAC-SHA256(api_key, values sorted by key joined with "|") -> "1|x|777"
    data = {"shopId": 777, "nonce": 1, "paymentId": "x"}
    expected = "38244108470d2e6db99d38be5f390538c8c86b7575ff1b38f97020bbe2f440f6"
    assert freekassa.api_signature(data, "apikey") == expected
    assert freekassa.api_signature({**data, "signature": "ignored"}, "apikey") == expected


async def test_freekassa_create_link(closing: list[PaymentGateway]) -> None:
    gw = _fk(closing)
    invoice = await gw.create(make_payment(19900), make_user(), "x")
    query = {k: v[0] for k, v in parse_qs(urlsplit(invoice.url or "").query).items()}
    assert query["m"] == FK_SHOP and query["oa"] == "199.00" and query["currency"] == "RUB"
    assert query["o"] == PAYMENT_ID
    assert query["s"] == freekassa.form_sign(FK_SHOP, "199.00", FK_S1, "RUB", PAYMENT_ID)
    assert not gw.supports_polling
    assert _fk(closing, api_key="k").supports_polling


@pytest.mark.parametrize(
    ("remote", "headers"),
    [(FK_IP, {}), ("51.250.54.238", {}), ("127.0.0.1", {"X-Forwarded-For": "178.154.197.79"})],
)
async def test_freekassa_webhook_ok(closing: list[PaymentGateway], remote: str, headers: dict[str, str]) -> None:
    gw = _fk(closing)
    result = await gw.parse_webhook(_fk_notification(), headers, remote)
    assert result.response_body == "YES"
    assert (result.payment_ids, result.provider_ids) == ([PAYMENT_ID], ["98765"])
    assert result.trusted_status is not None
    assert result.trusted_status.status is PaymentStatus.SUCCEEDED
    assert result.trusted_status.paid_amount_kop == 19900


async def test_freekassa_webhook_rejections(closing: list[PaymentGateway]) -> None:
    gw = _fk(closing)
    cases: list[tuple[bytes, str | None, dict[str, str], int]] = [
        (_fk_notification(), "1.2.3.4", {}, 403),
        (_fk_notification(), "8.8.8.8", {"X-Forwarded-For": FK_IP}, 403),
        (_fk_notification(secret="wrong"), FK_IP, {}, 401),
        (_fk_notification(shop="9999"), FK_IP, {}, 400),
        (_fk_notification(amount="abc"), FK_IP, {}, 400),
        (_fk_notification(order=""), FK_IP, {}, 400),
        (b"\xff", FK_IP, {}, 400),
    ]
    for raw, remote, headers, code in cases:
        with pytest.raises(WebhookRejected) as exc:
            await gw.parse_webhook(raw, headers, remote)
        assert exc.value.status_code == code, raw
    unchecked = _fk(closing, check_ip=False)
    assert (await unchecked.parse_webhook(_fk_notification(), {}, "1.2.3.4")).payment_ids == [PAYMENT_ID]


async def test_freekassa_fetch(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _fk(closing, fake, api_key=FK_API)
    order = {"merchant_order_id": PAYMENT_ID, "fk_order_id": 555, "amount": "199.00", "currency": "RUB", "status": 1}
    fake.reply("POST", "/v1/orders", (200, {"type": "success", "pages": 1, "orders": [order]}))
    status = await gw.fetch(make_payment())
    assert (status.status, status.paid_amount_kop, status.provider_payment_id) == (
        PaymentStatus.SUCCEEDED,
        19900,
        "555",
    )
    body = fake.requests[0].json()
    assert body["shopId"] == 7012 and body["paymentId"] == PAYMENT_ID
    assert body["signature"] == freekassa.api_signature(body, FK_API)

    second = await gw.fetch(make_payment())
    assert second.status is PaymentStatus.SUCCEEDED
    assert fake.requests[1].json()["nonce"] > body["nonce"]  # strictly increasing

    fake.reply("POST", "/v1/orders", (200, {"type": "success", "orders": []}))
    assert (await gw.fetch(make_payment())).status is PaymentStatus.PENDING
    for code, expected in ((0, PaymentStatus.PENDING), (6, PaymentStatus.REFUNDED), (9, PaymentStatus.CANCELED)):
        fake.reply("POST", "/v1/orders", (200, {"orders": [{**order, "status": code}]}))
        assert (await gw.fetch(make_payment())).status is expected
    fake.reply("POST", "/v1/orders", (200, {"orders": [{**order, "status": 42}]}))
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment())
    fake.reply("POST", "/v1/orders", (400, {"type": "error", "message": "Wrong signature"}))
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment())


async def test_freekassa_fetch_without_api_key(closing: list[PaymentGateway]) -> None:
    with pytest.raises(GatewayError, match="API_KEY"):
        await _fk(closing).fetch(make_payment())


async def test_freekassa_nonce_strictly_increasing_in_send_order_under_concurrency(
    closing: list[PaymentGateway], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FreeKassa rejects a nonce that is not greater than the previous one it received.

    The reconciler polls up to 8 payments at once, so nonces computed in the same millisecond must still
    differ, and requests must reach FreeKassa in nonce order even if earlier ones are slow.
    """
    gw = _fk(closing, api_key=FK_API)
    frozen_ms = 4_000_000_000_000  # a frozen clock, ahead of the time the gateway was built
    monkeypatch.setattr(freekassa.time, "time_ns", lambda: frozen_ms * 1_000_000)
    sent: list[int] = []
    calls = 0

    async def fake_request(method: str, url: str, **kw: Any) -> Any:
        nonlocal calls
        calls += 1
        nonce = kw["json"]["nonce"]
        await asyncio.sleep(0.01 if calls == 1 else 0)  # the first request is slow
        sent.append(nonce)  # the order in which FreeKassa receives them
        return {"orders": []}

    monkeypatch.setattr(gw, "_request_json", fake_request)
    semaphore = asyncio.Semaphore(8)

    async def poll() -> None:
        async with semaphore:
            await gw.fetch(make_payment())

    await asyncio.gather(*(poll() for _ in range(8)))
    assert len(sent) == 8
    assert sent == sorted(sent) and len(set(sent)) == 8
    assert sent[0] >= frozen_ms


# ---------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------
def test_registry_empty_by_default() -> None:
    assert build_gateways(make_settings()) == {}


async def test_manual_transfer_gateway_requires_administrator_confirmation() -> None:
    gateway = ManualTransferGateway()
    invoice = await gateway.create(make_payment(19950), make_user(), "manual transfer")
    assert invoice.provider_payment_id == PAYMENT_ID
    assert invoice.url == "" and invoice.provider_amount == "199.5" and invoice.provider_currency == "RUB"
    with pytest.raises(GatewayError, match="administrator"):
        await gateway.fetch(make_payment())
    with pytest.raises(WebhookRejected) as exc:
        await gateway.parse_webhook(b"{}", {}, None)
    assert exc.value.status_code == 404


async def test_robokassa_link_and_result_url_signature() -> None:
    gateway = RobokassaGateway("shop", "pass1", "pass2", check_ip=False, test_mode=True)
    payment = make_payment(19950)
    invoice = await gateway.create(payment, make_user(), "VPN access")
    sent = dict(parse_qsl(urlsplit(invoice.url or "").query))
    assert invoice.provider_payment_id == sent["InvId"]
    assert sent["OutSum"] == "199.50" and sent["Shp_payment_id"] == PAYMENT_ID and sent["IsTest"] == "1"
    assert sent["SignatureValue"] == robokassa.payment_signature(
        "shop", "199.50", sent["InvId"], "pass1", {"Shp_payment_id": PAYMENT_ID}, "md5"
    )

    fields = {"OutSum": "199.500000", "InvId": sent["InvId"], "Shp_payment_id": PAYMENT_ID}
    fields["SignatureValue"] = robokassa.result_signature(fields["OutSum"], fields["InvId"], "pass2", fields, "md5")
    result = await gateway.parse_webhook(urlencode(fields).encode(), {}, "203.0.113.1")
    assert result.payment_ids == [PAYMENT_ID] and result.provider_ids == [sent["InvId"]]
    assert result.response_body == f"OK{sent['InvId']}"
    assert result.trusted_status is not None and result.trusted_status.paid_amount_kop == 19950

    fields["SignatureValue"] = "0" * 32
    with pytest.raises(WebhookRejected) as exc:
        await gateway.parse_webhook(urlencode(fields).encode(), {}, "203.0.113.1")
    assert exc.value.status_code == 401
    assert not RobokassaGateway("shop", "one", "two").is_allowed_source("203.0.113.1", {})


async def test_registry_builds_enabled_in_ui_order() -> None:
    settings = make_settings(
        stars_enabled=True,
        cryptobot_enabled=True,
        cryptobot_token="tok",
        platega_enabled=True,
        platega_merchant_id="m",
        platega_secret="s",
        yookassa_enabled=True,
        yookassa_shop_id="1",
        yookassa_secret_key="k",
        yoomoney_enabled=True,
        yoomoney_wallet="4100",
        yoomoney_notification_secret="n",
        freekassa_enabled=True,
        freekassa_shop_id="1",
        freekassa_secret1="a",
        freekassa_secret2="b",
        robokassa_enabled=True,
        robokassa_merchant_login="roboshop",
        robokassa_password1="one",
        robokassa_password2="two",
        heleket_enabled=True,
        heleket_merchant_id="merchant",
        heleket_api_key="secret",
        nowpayments_enabled=True,
        nowpayments_api_key="now-key",
        nowpayments_ipn_secret="now-ipn",
        plisio_enabled=True,
        plisio_api_key="plisio-key",
        manual_transfer_enabled=True,
        manual_transfer_details="Transfer to +7 900 000-00-00",
        public_base_url="https://bot.example",
    )
    gateways = build_gateways(settings, 5, return_url="https://t.me/TestBot")
    assert list(gateways) == [
        "platega",
        "yookassa",
        "yoomoney",
        "freekassa",
        "robokassa",
        "cryptobot",
        "heleket",
        "nowpayments",
        "plisio",
        "manual",
        "stars",
    ]
    for key, gw in gateways.items():
        assert isinstance(gw, PaymentGateway)
        assert gw.key == key and gw.title and gw.min_amount_kop > 0
    assert isinstance(gateways["stars"], StarsGateway)
    assert isinstance(gateways["manual"], ManualTransferGateway)
    assert isinstance(gateways["robokassa"], RobokassaGateway)
    assert gateways["yookassa"]._return_url == "https://t.me/TestBot"  # type: ignore[attr-defined]
    assert not gateways["freekassa"].supports_polling
    await close_gateways(gateways)


def test_registry_yookassa_return_url_override() -> None:
    settings = make_settings(
        yookassa_enabled=True, yookassa_shop_id="1", yookassa_secret_key="k", yookassa_return_url="https://x.example"
    )
    gw = build_gateways(settings, return_url="https://t.me/TestBot")["yookassa"]
    assert gw._return_url == "https://x.example"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------------------------
# Regressions
# ---------------------------------------------------------------------------------------------
def test_signature_helpers_reject_non_ascii_signatures() -> None:
    assert not yoomoney.verify_notification({"a": "1", "sign": "é"}, YM_SECRET)
    assert not cryptobot.verify_signature(b"{}", "é", CB_TOKEN)


async def test_webhooks_with_non_ascii_signature_are_rejected(closing: list[PaymentGateway]) -> None:
    ym = YooMoneyGateway("4100111", YM_SECRET)
    with pytest.raises(WebhookRejected) as exc:
        await ym.parse_webhook(b"a=1&sign=%C3%A9", {}, None)
    assert exc.value.status_code == 401

    cb = CryptoBotGateway(CB_TOKEN)
    closing.append(cb)
    with pytest.raises(WebhookRejected) as exc:
        await cb.parse_webhook(b"{}", {"crypto-pay-api-signature": "é"}, None)
    assert exc.value.status_code == 401

    fk = _fk(closing, check_ip=False)
    raw = _fk_notification().replace(b"SIGN=", b"SIGN=%C3%A9")
    with pytest.raises(WebhookRejected) as exc:
        await fk.parse_webhook(raw, {}, None)
    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        # nginx with only `proxy_set_header X-Real-IP $remote_addr` passes the client's own XFF through
        {"X-Forwarded-For": "185.71.76.1", "X-Real-IP": "203.0.113.9"},
        # a proxy that sets only XFF passes the client's own X-Real-IP through
        {"X-Forwarded-For": "203.0.113.9", "X-Real-IP": "185.71.76.1"},
    ],
)
async def test_yookassa_webhook_spoofed_forwarding_header_is_rejected(
    closing: list[PaymentGateway], headers: dict[str, str]
) -> None:
    gw = _yookassa(None, closing)
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(YK_EVENT, headers, "127.0.0.1")
    assert exc.value.status_code == 403


async def test_platega_create_always_sends_return_urls(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _platega(fake, closing)
    fake.reply("POST", "/v2/transaction/process", (200, {"transactionId": "tx-3", "url": "https://p/tx-3"}))
    await gw.create(make_payment(), make_user(), "VPN")
    body = fake.requests[0].json()
    assert body["return"] == body["failedUrl"] == "https://t.me"


def test_freekassa_polls_by_our_payment_id(closing: list[PaymentGateway]) -> None:
    assert _fk(closing, api_key="k").polls_by_payment_id


def _multipart(fields: dict[str, str], boundary: str = "----fkBoundary7MA4YWxk") -> tuple[bytes, dict[str, str]]:
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        for name, value in fields.items()
    ]
    body = ("".join(parts) + f"--{boundary}--\r\n").encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


async def test_freekassa_webhook_multipart_form_data(closing: list[PaymentGateway]) -> None:
    """FreeKassa documents its notifications as form-data: a multipart body must verify like a urlencoded one."""
    gw = _fk(closing)
    fields = dict(parse_qsl(_fk_notification().decode()))
    raw, headers = _multipart(fields)
    result = await gw.parse_webhook(raw, {**headers, "X-Forwarded-For": FK_IP}, "127.0.0.1")
    assert result.response_body == "YES"
    assert (result.payment_ids, result.provider_ids) == ([PAYMENT_ID], ["98765"])
    assert result.trusted_status is not None and result.trusted_status.paid_amount_kop == 19900
    # lower-case header names (plain dicts from some frameworks) work too
    lowered = {"content-type": headers["Content-Type"]}
    assert (await gw.parse_webhook(raw, lowered, FK_IP)).payment_ids == [PAYMENT_ID]
    # a forged multipart notification is still rejected
    forged, _ = _multipart({**fields, "AMOUNT": "1.00"})
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(forged, headers, FK_IP)
    assert exc.value.status_code == 401
    # a broken multipart body is a 400, not a crash
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(b"\xff\xfe garbage", {"Content-Type": "multipart/form-data"}, FK_IP)
    assert exc.value.status_code == 400


async def test_freekassa_accepts_get_notifications(closing: list[PaymentGateway]) -> None:
    """The notification method (GET or POST) is a cabinet setting: GET puts the fields in the query string."""
    gw = _fk(closing)
    assert gw.webhook_query_fields is True
    result = await gw.parse_webhook(_fk_notification(), {}, FK_IP)  # the query string, passed as the payload
    assert result.response_body == "YES" and result.payment_ids == [PAYMENT_ID]


async def test_heleket_invoice_poll_and_webhook(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply(
        "POST",
        "/payment",
        (200, {"state": 0, "result": {"uuid": "invoice-1", "url": "https://pay.example", "amount": "199"}}),
    )
    fake.reply(
        "POST",
        "/payment/info",
        (200, {"state": 0, "result": {"uuid": "invoice-1", "payment_status": "paid", "amount": "199"}}),
    )
    gw = _no_retry_delay(
        HeleketGateway(
            "merchant", "secret", callback_url="https://bot.example/webhooks/heleket", base_url=fake.base_url
        )
    )
    closing.append(gw)
    invoice = await gw.create(make_payment(), make_user(), "topup")
    assert invoice.provider_payment_id == "invoice-1" and invoice.url == "https://pay.example"
    sent = fake.requests[0]
    assert sent.headers["merchant"] == "merchant" and sent.headers["sign"] == heleket.sign(sent.json(), "secret")
    assert (await gw.fetch(make_payment())).status is PaymentStatus.SUCCEEDED
    payload = {"order_id": PAYMENT_ID, "uuid": "invoice-1", "txid": "has/slash", "status": "paid"}
    raw = json.dumps({**payload, "sign": heleket.sign(payload, "secret")}, separators=(",", ":")).encode()
    result = await gw.parse_webhook(raw, {}, None)
    assert result.payment_ids == [PAYMENT_ID] and result.provider_ids == ["invoice-1"]
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw.replace(b"paid", b"fail"), {}, None)


def test_heleket_statuses_and_php_slashes() -> None:
    assert b"\\/" in heleket.canonical_json({"url": "https://example.com/a"})
    assert heleket.status({"payment_status": "paid_over", "amount": "2.50"}).paid_amount_kop == 250
    assert heleket.status({"payment_status": "cancel"}).status is PaymentStatus.CANCELED


async def test_nowpayments_invoice_poll_and_ipn(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply(
        "POST",
        "/payment",
        (200, {"payment_id": 7, "pay_address": "wallet", "pay_amount": "2", "pay_currency": "usdttrc20"}),
    )
    fake.reply("GET", "/payment/7", (200, {"payment_id": 7, "payment_status": "finished", "price_amount": "199"}))
    gw = _no_retry_delay(
        NowPaymentsGateway(
            "key", "ipn", callback_url="https://bot.example/webhooks/nowpayments", base_url=fake.base_url
        )
    )
    closing.append(gw)
    assert (await gw.create(make_payment(), make_user(), "topup")).provider_payment_id == "7"
    assert (await gw.fetch(make_payment(provider_payment_id="7"))).paid_amount_kop == 19900
    payload = {"order_id": PAYMENT_ID, "payment_id": 7, "payment_status": "finished"}
    result = await gw.parse_webhook(
        json.dumps(payload).encode(), {"x-nowpayments-sig": nowpayments_signature(payload, "ipn")}, None
    )
    assert result.payment_ids == [PAYMENT_ID] and result.provider_ids == ["7"]


async def test_plisio_invoice_poll_and_webhook(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply(
        "GET",
        "/invoices/new",
        (200, {"status": "success", "data": {"txn_id": "tx-1", "invoice_url": "https://pay.example"}}),
    )
    fake.reply(
        "GET",
        "/operations/tx-1",
        (200, {"status": "success", "data": {"id": "tx-1", "status": "completed", "source_amount": "199"}}),
    )
    gw = _no_retry_delay(
        PlisioGateway(
            "key", currency="USDT", callback_url="https://bot.example/webhooks/plisio", base_url=fake.base_url
        )
    )
    closing.append(gw)
    invoice = await gw.create(make_payment(), make_user(), "topup")
    assert invoice.provider_payment_id == "tx-1" and invoice.url == "https://pay.example"
    sent = fake.requests[0]
    assert sent.query["api_key"] == "key" and sent.query["currency"] == "USDT"
    assert sent.query["callback_url"] == "https://bot.example/webhooks/plisio?json=true"
    assert "success_callback_url" not in sent.query
    assert (await gw.fetch(make_payment(provider_payment_id="tx-1"))).paid_amount_kop == 19900
    payload = {"order_number": PAYMENT_ID, "txn_id": "tx-1", "status": "completed", "comment": "Привет"}
    raw = json.dumps({**payload, "verify_hash": plisio.signature(payload, "key")}, ensure_ascii=False).encode()
    result = await gw.parse_webhook(raw, {}, None)
    assert result.payment_ids == [PAYMENT_ID] and result.provider_ids == ["tx-1"]
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw.replace(b"completed", b"pendingxx"), {}, None)


def test_plisio_statuses_and_callback_url() -> None:
    assert plisio.status({"status": "mismatch", "source_amount": "2.5"}).paid_amount_kop == 250
    assert plisio.status({"status": "cancelled"}).status is PaymentStatus.CANCELED
    assert plisio.json_callback_url("https://bot.example/hook?x=1") == "https://bot.example/hook?x=1&json=true"
