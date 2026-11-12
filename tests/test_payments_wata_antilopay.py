"""WATA and Antilopay gateways against a local fake HTTP server, plus RSA webhook verification."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.db.models import PaymentStatus
from app.services.payments import rsa_keys
from app.services.payments.antilopay import AntilopayGateway
from app.services.payments.base import GatewayError, PaymentGateway, WebhookRejected
from app.services.payments.registry import build_gateways, close_gateways
from app.services.payments.wata import WataGateway
from tests.conftest import make_settings
from tests.test_payments_gateways import PAYMENT_ID, TRUSTED, FakeProvider, make_payment, make_user

_PROVIDER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_MERCHANT_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_pem(key: rsa.RSAPrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def _public_b64(key: rsa.RSAPrivateKey) -> str:
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode()


def _private_b64(key: rsa.RSAPrivateKey) -> str:
    der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return base64.b64encode(der).decode()


def _sign(key: rsa.RSAPrivateKey, data: bytes, algorithm: hashes.HashAlgorithm) -> str:
    return base64.b64encode(key.sign(data, padding.PKCS1v15(), algorithm)).decode()


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


# ---------------------------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------------------------
def test_rsa_keys_accept_pem_base64_and_escaped_newlines() -> None:
    pem = _public_pem(_PROVIDER_KEY)
    escaped = pem.replace("\n", "\\n")
    data = b"payload"
    signature = _sign(_PROVIDER_KEY, data, hashes.SHA256())
    for text in (pem, escaped, _public_b64(_PROVIDER_KEY)):
        assert rsa_keys.verify(rsa_keys.load_public_key(text), data, signature, hashes.SHA256())
    # PKCS#8 body under a PKCS#1 label, as some provider examples show it.
    labelled = f"-----BEGIN RSA PRIVATE KEY-----\\n{_private_b64(_MERCHANT_KEY)}\\n-----END RSA PRIVATE KEY-----"
    private = rsa_keys.load_private_key(labelled)
    assert rsa_keys.verify(
        rsa_keys.load_public_key(_public_b64(_MERCHANT_KEY)),
        data,
        rsa_keys.sign(private, data, hashes.SHA256()),
        hashes.SHA256(),
    )
    with pytest.raises(ValueError):
        rsa_keys.load_public_key("not a key")
    with pytest.raises(ValueError):
        rsa_keys.load_private_key(_public_b64(_PROVIDER_KEY))
    assert not rsa_keys.verify(rsa_keys.load_public_key(pem), data, "%%%", hashes.SHA256())
    assert not rsa_keys.verify(rsa_keys.load_public_key(pem), data, None, hashes.SHA256())


# ---------------------------------------------------------------------------------------------
# WATA
# ---------------------------------------------------------------------------------------------
WATA_LINK_ID = "3a16a4f0-27b0-09d1-16da-ba8d5c63eae3"


def _wata(fake: FakeProvider, closing: list[PaymentGateway], **kw: Any) -> WataGateway:
    gw = WataGateway("tok", base_url=fake.base_url, **kw)
    gw._retry_delay = 0
    closing.append(gw)
    return gw


def _wata_tx(status: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": "3a1cf611-abc6-8d30-c4cd-521c9f6eeeb0",
        "type": "SBP",
        "kind": "Payment",
        "amount": 199.0,
        "currency": "RUB",
        "status": status,
        "orderId": PAYMENT_ID,
        "paymentLinkId": WATA_LINK_ID,
        **kw,
    }


def _wata_webhook(status: str = "Paid", **kw: Any) -> bytes:
    body = {
        "transactionType": "CardCrypto",
        "kind": "Payment",
        "id": "3a1cf611-abc6-8d30-c4cd-521c9f6eeeb0",
        "transactionStatus": status,
        "amount": 199.00,
        "currency": "RUB",
        "orderId": PAYMENT_ID,
        "paymentLinkId": WATA_LINK_ID,
        **kw,
    }
    return json.dumps(body, indent=2).encode()  # not compact: the raw bytes are what is signed


async def test_wata_create_link(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply(
        "POST", "/links", (200, {"id": WATA_LINK_ID, "url": "https://payment.wata.pro/pay/x", "status": "Opened"})
    )
    gw = _wata(fake, closing, return_url="https://t.me/bot", link_ttl=timedelta(hours=24))
    invoice = await gw.create(make_payment(19950), make_user(), "Тариф 1 месяц")
    assert invoice.provider_payment_id == WATA_LINK_ID
    assert invoice.url == "https://payment.wata.pro/pay/x"
    assert invoice.provider_amount == "199.50"
    req = fake.requests[0]
    assert req.headers["Authorization"] == "Bearer tok"
    body = req.json()
    assert body["amount"] == 199.5
    assert body["currency"] == "RUB"
    assert body["orderId"] == PAYMENT_ID
    assert body["description"] == "Тариф 1 месяц"
    assert body["successRedirectUrl"] == body["failRedirectUrl"] == "https://t.me/bot"
    assert body["expirationDateTime"].endswith("Z")


async def test_wata_create_rejects_bad_answer(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("POST", "/links", (400, {"error": {"message": "Validation"}}))
    with pytest.raises(GatewayError):
        await _wata(fake, closing).create(make_payment(), make_user(), "x")
    fake.reply("POST", "/links", (200, {"status": "Opened"}))
    with pytest.raises(GatewayError):
        await _wata(fake, closing).create(make_payment(), make_user(), "x")


def test_wata_link_ttl_is_clamped() -> None:
    gw = WataGateway("tok", link_ttl=timedelta(minutes=1))
    assert gw._link_ttl == timedelta(minutes=10)
    assert WataGateway("tok", link_ttl=timedelta(days=90))._link_ttl == timedelta(days=30)
    assert WataGateway("tok", sandbox=True)._base_url == "https://api-sandbox.wata.pro/api/h2h"


async def test_wata_fetch_by_order_id(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _wata(fake, closing)
    fake.reply("GET", "/v2/transactions", (200, {"items": [_wata_tx("Declined"), _wata_tx("Paid", amount=199.5)]}))
    status = await gw.fetch(make_payment())
    assert status.status is PaymentStatus.SUCCEEDED
    assert status.paid_amount_kop == 19950
    assert status.provider_payment_id == WATA_LINK_ID
    assert fake.requests[0].query["OrderId"] == PAYMENT_ID
    assert fake.requests[0].headers["Authorization"] == "Bearer tok"

    fake.reply("GET", "/v2/transactions", (200, {"items": [_wata_tx("Declined"), _wata_tx("Pending")]}))
    assert (await gw.fetch(make_payment())).status is PaymentStatus.PENDING
    fake.reply("GET", "/v2/transactions", (200, {"items": []}))
    assert (await gw.fetch(make_payment())).status is PaymentStatus.PENDING
    # A paid transaction for another order, or a refund, never pays ours.
    fake.reply(
        "GET",
        "/v2/transactions",
        (200, {"items": [_wata_tx("Paid", orderId="other"), _wata_tx("Paid", kind="Refund")]}),
    )
    assert (await gw.fetch(make_payment())).status is PaymentStatus.PENDING
    fake.reply("GET", "/v2/transactions", (200, {"items": [_wata_tx("Paid", currency="USD")]}))
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment())
    fake.reply("GET", "/v2/transactions", (200, {"unexpected": True}))
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment())
    fake.reply("GET", "/v2/transactions", (429, {"error": {"message": "Too many requests"}}))
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment())


async def test_wata_webhook_signature_with_downloaded_key(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("GET", "/public-key", (200, {"value": _public_pem(_PROVIDER_KEY)}))
    gw = _wata(fake, closing)
    raw = _wata_webhook()
    result = await gw.parse_webhook(raw, {"x-signature": _sign(_PROVIDER_KEY, raw, hashes.SHA512())}, "1.2.3.4")
    assert result.payment_ids == [PAYMENT_ID]
    assert result.provider_ids == [WATA_LINK_ID]
    assert result.trusted_status is None
    # The key is cached: a second webhook does not download it again.
    await gw.parse_webhook(raw, {"X-Signature": _sign(_PROVIDER_KEY, raw, hashes.SHA512())}, None)
    assert [r.path for r in fake.requests] == ["/public-key"]

    with pytest.raises(WebhookRejected) as exc:  # SHA-256 is not the documented algorithm
        await gw.parse_webhook(raw, {"X-Signature": _sign(_PROVIDER_KEY, raw, hashes.SHA256())}, None)
    assert exc.value.status_code == 401
    with pytest.raises(WebhookRejected):  # signed by someone else
        await gw.parse_webhook(raw, {"X-Signature": _sign(_OTHER_KEY, raw, hashes.SHA512())}, None)
    with pytest.raises(WebhookRejected):  # body changed after signing
        tampered = raw.replace(b"199.0", b"999.0")
        await gw.parse_webhook(tampered, {"X-Signature": _sign(_PROVIDER_KEY, raw, hashes.SHA512())}, None)
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw, {}, None)
    with pytest.raises(WebhookRejected) as exc:  # signed but malformed
        bad = b"[1, 2]"
        await gw.parse_webhook(bad, {"X-Signature": _sign(_PROVIDER_KEY, bad, hashes.SHA512())}, None)
    assert exc.value.status_code == 400


async def test_wata_webhook_refreshes_rotated_key(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("GET", "/public-key", (200, {"value": _public_pem(_PROVIDER_KEY)}))
    gw = _wata(fake, closing)
    # A key cached long ago that WATA has since rotated.
    gw._key = rsa_keys.load_public_key(_public_pem(_OTHER_KEY))
    gw._key_loaded_at = time.monotonic() - 3600
    raw = _wata_webhook()
    result = await gw.parse_webhook(raw, {"X-Signature": _sign(_PROVIDER_KEY, raw, hashes.SHA512())}, None)
    assert result.payment_ids == [PAYMENT_ID]
    assert len(fake.requests) == 1
    # A fresh key is not downloaded again for every forged webhook.
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw, {"X-Signature": _sign(_OTHER_KEY, raw, hashes.SHA512())}, None)
    assert len(fake.requests) == 1


async def test_wata_webhook_key_unavailable(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("GET", "/public-key", (404, {"error": "no"}))
    gw = _wata(fake, closing)
    raw = _wata_webhook()
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(raw, {"X-Signature": _sign(_PROVIDER_KEY, raw, hashes.SHA512())}, None)
    assert exc.value.status_code == 503


async def test_wata_webhook_pinned_key_and_ignored_events(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _wata(fake, closing, public_key=_public_pem(_PROVIDER_KEY).replace("\n", "\\n"))
    for raw in (_wata_webhook("Created"), _wata_webhook("Declined"), _wata_webhook("Paid", kind="Refund")):
        result = await gw.parse_webhook(raw, {"X-Signature": _sign(_PROVIDER_KEY, raw, hashes.SHA512())}, None)
        assert result.payment_ids == [] and result.provider_ids == []
        assert result.status_code == 200
    raw = _wata_webhook()
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw, {"X-Signature": _sign(_OTHER_KEY, raw, hashes.SHA512())}, None)
    assert fake.requests == []  # a pinned key is never downloaded


# ---------------------------------------------------------------------------------------------
# Antilopay
# ---------------------------------------------------------------------------------------------
AP_PAYMENT_ID = "APAYE98274D61702540483325"
AP_CALLBACK_IP = "81.177.221.226"


def _antilopay(fake: FakeProvider, closing: list[PaymentGateway], **kw: Any) -> AntilopayGateway:
    gw = AntilopayGateway(
        "PE8BED46C045139256",
        "secret-id",
        _private_b64(_MERCHANT_KEY),
        _public_pem(_PROVIDER_KEY),
        base_url=fake.base_url,
        trusted_proxies=TRUSTED,
        **kw,
    )
    gw._retry_delay = 0
    closing.append(gw)
    return gw


def _verify_request(req: Any) -> None:
    assert req.headers["X-Apay-Secret-Id"] == "secret-id"
    assert req.headers["X-Apay-Sign-Version"] == "1"
    assert req.headers["Content-Type"] == "application/json"
    signature = base64.b64decode(req.headers["X-Apay-Sign"])
    _MERCHANT_KEY.public_key().verify(signature, req.body, padding.PKCS1v15(), hashes.SHA256())
    assert b": " not in req.body and b", " not in req.body  # compact JSON is what was signed


def _ap_check(status: str, **kw: Any) -> dict[str, Any]:
    return {
        "code": 0,
        "payment_id": AP_PAYMENT_ID,
        "order_id": PAYMENT_ID,
        "amount": 193.03,
        "original_amount": 199.00,
        "fee": 5.97,
        "status": status,
        "currency": "RUB",
        **kw,
    }


def _ap_callback(status: str = "SUCCESS", **kw: Any) -> bytes:
    body = {
        "type": "payment",
        "payment_id": AP_PAYMENT_ID,
        "order_id": PAYMENT_ID,
        "ctime": "2023-11-29 14:30:00.012345",
        "amount": 193.03,
        "original_amount": 199.00,
        "fee": 5.97,
        "status": status,
        "currency": "RUB",
        **kw,
    }
    return json.dumps(body).encode()


def _ap_headers(raw: bytes, key: rsa.RSAPrivateKey = _PROVIDER_KEY, **extra: str) -> dict[str, str]:
    return {"X-Apay-Callback": _sign(key, raw, hashes.SHA256()), "X-Apay-Callback-Version": "1", **extra}


async def test_antilopay_create(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply(
        "POST",
        "/payment/create",
        (
            200,
            {
                "code": 0,
                "payment_id": AP_PAYMENT_ID,
                "payment_url": f"https://gate.antilopay.com/payment/{AP_PAYMENT_ID}",
            },
        ),
    )
    gw = _antilopay(fake, closing, return_url="https://t.me/bot", prefer_methods=["sbp", " CARD_RU", ""], vat=22)
    invoice = await gw.create(make_payment(19950), make_user(), "Тариф")
    assert invoice.provider_payment_id == AP_PAYMENT_ID
    assert invoice.url == f"https://gate.antilopay.com/payment/{AP_PAYMENT_ID}"
    assert invoice.provider_amount == "199.50"
    req = fake.requests[0]
    _verify_request(req)
    body = req.json()
    assert body["project_identificator"] == "PE8BED46C045139256"
    assert body["amount"] == 199.5
    assert body["order_id"] == PAYMENT_ID
    assert body["currency"] == "RUB"
    assert body["product_type"] == "services"
    assert body["product_name"] == body["description"] == "Тариф"
    assert body["customer"] == {"email": "5512345678@telegram.org"}
    assert body["success_url"] == body["fail_url"] == "https://t.me/bot"
    assert body["prefer_methods"] == ["SBP", "CARD_RU"]
    assert body["vat"] == 22
    assert "Тариф".encode() in req.body  # UTF-8, not \u escapes


async def test_antilopay_create_uses_configured_email(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("POST", "/payment/create", (200, {"code": 0, "payment_id": "p", "payment_url": "https://u"}))
    await _antilopay(fake, closing, customer_email="shop@example.com").create(make_payment(10000), make_user(), "x")
    body = fake.requests[0].json()
    assert body["customer"] == {"email": "shop@example.com"}
    assert body["amount"] == 100
    assert "vat" not in body and "prefer_methods" not in body and "success_url" not in body


async def test_antilopay_create_errors(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _antilopay(fake, closing)
    fake.reply("POST", "/payment/create", (200, {"code": 3, "error": "Invalid signature"}))
    with pytest.raises(GatewayError, match="code 3"):
        await gw.create(make_payment(), make_user(), "x")
    fake.reply("POST", "/payment/create", (502, "bad gateway"))
    with pytest.raises(GatewayError):
        await gw.create(make_payment(), make_user(), "x")
    assert len(fake.requests) == 2  # creates are never retried


async def test_antilopay_fetch_status_mapping(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _antilopay(fake, closing)
    fake.reply("POST", "/payment/check", (200, _ap_check("SUCCESS")))
    status = await gw.fetch(make_payment())
    assert status.status is PaymentStatus.SUCCEEDED
    assert status.paid_amount_kop == 19900  # original_amount, not the net amount after fees
    assert status.provider_payment_id == AP_PAYMENT_ID
    req = fake.requests[0]
    _verify_request(req)
    assert req.json() == {"project_identificator": "PE8BED46C045139256", "order_id": PAYMENT_ID}

    expected = {
        "PENDING": PaymentStatus.PENDING,
        "FAIL": PaymentStatus.CANCELED,
        "CANCEL": PaymentStatus.CANCELED,
        "EXPIRED": PaymentStatus.EXPIRED,
        "CHARGEBACK": PaymentStatus.REFUNDED,
        "REVERSED": PaymentStatus.REFUNDED,
        "SOMETHING_NEW": PaymentStatus.PENDING,
    }
    for value, mapped in expected.items():
        fake.reply("POST", "/payment/check", (200, _ap_check(value)))
        result = await gw.fetch(make_payment())
        assert result.status is mapped
        assert result.paid_amount_kop is None

    fake.reply("POST", "/payment/check", (200, {"code": 8, "error": "Payment not found"}))
    assert (await gw.fetch(make_payment())).status is PaymentStatus.PENDING
    fake.reply("POST", "/payment/check", (200, {"code": 2, "error": "bad merchant"}))
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment())
    fake.reply("POST", "/payment/check", (200, _ap_check("SUCCESS", order_id="other")))
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment())
    fake.reply("POST", "/payment/check", (200, _ap_check("SUCCESS", currency="USD")))
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment())


async def test_antilopay_callback_accepts_signed(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _antilopay(fake, closing)
    raw = _ap_callback()
    result = await gw.parse_webhook(raw, _ap_headers(raw), AP_CALLBACK_IP)
    assert result.payment_ids == [PAYMENT_ID]
    assert result.provider_ids == [AP_PAYMENT_ID]
    assert result.trusted_status is None
    # Through a trusted reverse proxy.
    result = await gw.parse_webhook(raw, _ap_headers(raw, **{"X-Forwarded-For": "87.228.9.243"}), "127.0.0.1")
    assert result.payment_ids == [PAYMENT_ID]
    # Other callback kinds are acknowledged and ignored.
    other = _ap_callback(type="withdraw")
    assert (await gw.parse_webhook(other, _ap_headers(other), AP_CALLBACK_IP)).payment_ids == []


async def test_antilopay_callback_rejections(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _antilopay(fake, closing)
    raw = _ap_callback()
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(raw, _ap_headers(raw), "8.8.8.8")
    assert exc.value.status_code == 403
    with pytest.raises(WebhookRejected) as exc:  # spoofed XFF from an untrusted peer
        await gw.parse_webhook(raw, _ap_headers(raw, **{"X-Forwarded-For": AP_CALLBACK_IP}), "8.8.8.8")
    assert exc.value.status_code == 403
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(raw, _ap_headers(raw, _OTHER_KEY), AP_CALLBACK_IP)
    assert exc.value.status_code == 401
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw.replace(b"199.0", b"999.0"), _ap_headers(raw), AP_CALLBACK_IP)
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw, {}, AP_CALLBACK_IP)
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw, _ap_headers(raw, **{"X-Apay-Callback-Version": "2"}), AP_CALLBACK_IP)
    with pytest.raises(WebhookRejected):  # the merchant's own key must not verify callbacks
        await gw.parse_webhook(raw, _ap_headers(raw, _MERCHANT_KEY), AP_CALLBACK_IP)
    bad = b"not json"
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(bad, _ap_headers(bad), AP_CALLBACK_IP)
    assert exc.value.status_code == 400


async def test_antilopay_callback_ip_check_can_be_disabled(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _antilopay(fake, closing, check_ip=False)
    raw = _ap_callback()
    assert (await gw.parse_webhook(raw, _ap_headers(raw), "8.8.8.8")).payment_ids == [PAYMENT_ID]
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw, _ap_headers(raw, _OTHER_KEY), "8.8.8.8")


def test_antilopay_rejects_bad_keys() -> None:
    with pytest.raises(ValueError):
        AntilopayGateway("p", "s", "not-a-key", _public_pem(_PROVIDER_KEY))
    with pytest.raises(ValueError):
        AntilopayGateway("p", "s", _private_b64(_MERCHANT_KEY), "")


# ---------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------
async def test_registry_builds_wata_and_antilopay() -> None:
    settings = make_settings(
        public_base_url="https://bot.example.com",
        wata_enabled=True,
        wata_token="tok",
        wata_sandbox=True,
        antilopay_enabled=True,
        antilopay_project_id="PE1",
        antilopay_secret_id="sid",
        antilopay_secret_key=_private_b64(_MERCHANT_KEY).replace("\n", "\\n"),
        antilopay_callback_key=_public_pem(_PROVIDER_KEY).replace("\n", "\\n"),
        antilopay_prefer_methods="SBP, CARD_RU",
    )
    gateways = build_gateways(settings, return_url="https://t.me/bot")
    try:
        wata = gateways["wata"]
        antilopay = gateways["antilopay"]
        assert isinstance(wata, WataGateway) and isinstance(antilopay, AntilopayGateway)
        assert wata._base_url == "https://api-sandbox.wata.pro/api/h2h"
        assert antilopay._prefer_methods == ["SBP", "CARD_RU"]
        assert antilopay._return_url == "https://t.me/bot"
    finally:
        await close_gateways(gateways)


def test_settings_require_credentials() -> None:
    with pytest.raises(ValueError, match="WATA"):
        make_settings(public_base_url="https://bot.example.com", wata_enabled=True)
    with pytest.raises(ValueError, match="ANTILOPAY"):
        make_settings(public_base_url="https://bot.example.com", antilopay_enabled=True, antilopay_project_id="p")
