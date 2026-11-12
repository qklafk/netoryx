"""cisPay, TabPay, SeverPay, Cashera, ParityPay and Aurapay against a local fake HTTP server.

Each gateway: invoice request shape, status mapping, webhook verification (good / bad / replayed) and, through the
payment service, the rule that a webhook only triggers a re-fetch and credits exactly once.
"""

from __future__ import annotations

# ruff: noqa: F811  (pytest fixtures imported from the gateway tests)
import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

from app.db.models import BalanceTransaction, Payment, PaymentProvider, PaymentStatus
from app.db.session import SessionFactory
from app.services.payments import aurapay, cashera, cispay, concat_signature, paritypay, service, severpay, tabpay
from app.services.payments.aurapay import AuraPayGateway
from app.services.payments.base import GatewayError, PaymentGateway, WebhookRejected
from app.services.payments.cashera import CasheraGateway
from app.services.payments.cispay import CisPayGateway
from app.services.payments.paritypay import ParityPayGateway
from app.services.payments.registry import build_gateways, close_gateways
from app.services.payments.severpay import SeverPayGateway
from app.services.payments.tabpay import TabPayGateway
from tests.conftest import make_settings
from tests.test_payments_gateways import (  # noqa: F401  (fixtures)
    PAYMENT_ID,
    TRUSTED,
    FakeProvider,
    closing,
    fake,
    make_payment,
    make_user,
)
from tests.test_subs_support import add_user

USER_ID = 5512345678
PROVIDER_ID = "9d4e0c1a-1111-4222-8333-444455556666"


def _mac(secret: str, data: bytes) -> str:
    return hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------------------------
# cisPay
# ---------------------------------------------------------------------------------------------
CIS_KEY = "cis_sec_test"


def _cis(fake: FakeProvider, closing: list[PaymentGateway], **kw: Any) -> CisPayGateway:
    gw = CisPayGateway("shop-uuid", CIS_KEY, base_url=fake.base_url, **kw)
    gw._retry_delay = 0
    closing.append(gw)
    return gw


def _cis_obj(status: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": PROVIDER_ID,
        "order_id": PAYMENT_ID,
        "status": status,
        "amount": 19900,
        "charged_amount": 19900,
        "currency": "RUB",
        **kw,
    }


async def test_cispay_create_request_shape(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("POST", "/payments", (201, _cis_obj("PENDING", payment_url="https://pay.cispay/x")))
    gw = _cis(fake, closing, payment_method="sbp", return_url="https://t.me/bot")
    invoice = await gw.create(make_payment(19950), make_user(), "VPN 30d")
    assert (invoice.url, invoice.provider_payment_id, invoice.provider_amount) == (
        "https://pay.cispay/x",
        PROVIDER_ID,
        "199.50",
    )
    req = fake.requests[0]
    assert req.headers["X-Shop-ID"] == "shop-uuid" and req.headers["X-Api-Key"] == CIS_KEY
    assert req.json() == {
        "amount": 19950,
        "currency": "RUB",
        "order_id": PAYMENT_ID,
        "customer_id": "5512345678",
        "description": "VPN 30d",
        "payment_method": "SBP",
        "redirect_success_url": "https://t.me/bot",
        "redirect_fail_url": "https://t.me/bot",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PENDING", PaymentStatus.PENDING),
        ("PAID", PaymentStatus.SUCCEEDED),
        ("FAILED", PaymentStatus.CANCELED),
        ("EXPIRED", PaymentStatus.EXPIRED),
        ("REFUNDED", PaymentStatus.REFUNDED),
    ],
)
async def test_cispay_fetch_status_mapping(
    fake: FakeProvider, closing: list[PaymentGateway], raw: str, expected: PaymentStatus
) -> None:
    fake.reply("GET", "/payments/status", (200, _cis_obj(raw)))
    status = await _cis(fake, closing).fetch(make_payment(provider_payment_id=PROVIDER_ID))
    assert status.status is expected
    assert status.paid_amount_kop == (19900 if expected is PaymentStatus.SUCCEEDED else None)
    assert fake.requests[0].query == {"id": PROVIDER_ID}


async def test_cispay_fetch_by_order_id_and_sandbox_is_not_money(
    fake: FakeProvider, closing: list[PaymentGateway]
) -> None:
    fake.reply("GET", "/payments/status", (200, _cis_obj("PAID", is_sandbox=True)))
    status = await _cis(fake, closing).fetch(make_payment())
    assert fake.requests[0].query == {"order_id": PAYMENT_ID}
    assert status.status is PaymentStatus.PENDING and status.paid_amount_kop is None


def test_cispay_status_rejects_foreign_order_unknown_status_and_currency() -> None:
    for bad in (_cis_obj("PAID", order_id="x"), _cis_obj("WEIRD"), _cis_obj("PAID", currency="USD")):
        with pytest.raises(GatewayError):
            cispay.payment_status(bad, PAYMENT_ID)


async def test_cispay_webhook(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _cis(fake, closing)
    raw = json.dumps(_cis_obj("PAID"), separators=(",", ":")).encode()
    result = await gw.parse_webhook(raw, {"x-signature": cispay.sign(CIS_KEY, raw)}, None)
    assert result.payment_ids == [PAYMENT_ID] and result.provider_ids == [PROVIDER_ID]
    for headers in ({}, {"X-Signature": cispay.sign("other", raw)}, {"X-Signature": "00"}):
        with pytest.raises(WebhookRejected):
            await gw.parse_webhook(raw, headers, None)
    tampered = raw.replace(b"19900", b"99999")
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(tampered, {"X-Signature": cispay.sign(CIS_KEY, raw)}, None)
    sub = json.dumps({"event": "subscription.cancelled", "object": "subscription", "id": "s"}).encode()
    ignored = await gw.parse_webhook(sub, {"X-Signature": cispay.sign(CIS_KEY, sub)}, None)
    assert ignored.payment_ids == [] and ignored.provider_ids == []


# ---------------------------------------------------------------------------------------------
# TabPay
# ---------------------------------------------------------------------------------------------
TAB_SECRET = "tab_whsec"


def _tab(fake: FakeProvider, closing: list[PaymentGateway], **kw: Any) -> TabPayGateway:
    gw = TabPayGateway("tp_key", TAB_SECRET, base_url=fake.base_url, **kw)
    gw._retry_delay = 0
    closing.append(gw)
    return gw


def _tab_obj(status: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": PROVIDER_ID,
        "orderId": PAYMENT_ID,
        "status": status,
        "amountKopecks": 19900,
        "isTest": False,
        **kw,
    }


async def test_tabpay_create_request_shape(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("POST", "/v1/payments", (201, _tab_obj("CREATED", payUrl="https://tabpay.org/pay/x")))
    gw = _tab(fake, closing, payment_method="card", return_url="https://t.me/bot")
    invoice = await gw.create(make_payment(19950), make_user(), "VPN")
    assert invoice.url == "https://tabpay.org/pay/x" and invoice.provider_payment_id == PROVIDER_ID
    req = fake.requests[0]
    assert req.headers["X-Api-Key"] == "tp_key"
    assert req.json() == {
        "orderId": PAYMENT_ID,
        "amountKopecks": 19950,
        "description": "VPN",
        "telegramId": "5512345678",
        "method": "CARD",
        "successUrl": "https://t.me/bot",
        "failUrl": "https://t.me/bot",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CREATED", PaymentStatus.PENDING),
        ("PENDING", PaymentStatus.PENDING),
        ("SUCCESS", PaymentStatus.SUCCEEDED),
        ("FAILED", PaymentStatus.CANCELED),
        ("CANCELED", PaymentStatus.CANCELED),
        ("EXPIRED", PaymentStatus.EXPIRED),
        ("REFUNDED", PaymentStatus.REFUNDED),
    ],
)
async def test_tabpay_fetch_status_mapping(
    fake: FakeProvider, closing: list[PaymentGateway], raw: str, expected: PaymentStatus
) -> None:
    fake.reply("GET", f"/v1/payments/{PROVIDER_ID}", (200, _tab_obj(raw)))
    status = await _tab(fake, closing).fetch(make_payment(provider_payment_id=PROVIDER_ID))
    assert status.status is expected
    assert status.paid_amount_kop == (19900 if expected is PaymentStatus.SUCCEEDED else None)


async def test_tabpay_fetch_by_order_id_and_test_payment(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("GET", "/v1/payments", (200, _tab_obj("SUCCESS", isTest=True)))
    status = await _tab(fake, closing).fetch(make_payment())
    assert fake.requests[0].query == {"orderId": PAYMENT_ID}
    assert status.status is PaymentStatus.PENDING and status.paid_amount_kop is None
    with pytest.raises(GatewayError):
        tabpay.payment_status(_tab_obj("SUCCESS", orderId="other"), PAYMENT_ID)


def _tab_signed(
    body: dict[str, Any], *, ts: int | None = None, secret: str = TAB_SECRET
) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    stamp = str(int(time.time()) if ts is None else ts)
    return raw, {
        "X-Timestamp": stamp,
        "X-Signature-V2": tabpay.sign_v2(secret, stamp, raw),
        "X-Signature": tabpay.sign_v1(secret, raw),
    }


async def test_tabpay_webhook(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _tab(fake, closing)
    body = {"id": PROVIDER_ID, "orderId": PAYMENT_ID, "status": "SUCCESS", "amountKopecks": 19900, "test": False}
    raw, headers = _tab_signed(body)
    result = await gw.parse_webhook(raw, headers, None)
    assert result.payment_ids == [PAYMENT_ID] and result.provider_ids == [PROVIDER_ID]
    # the legacy signature alone is still accepted
    legacy = await gw.parse_webhook(raw, {"x-signature": headers["X-Signature"]}, None)
    assert legacy.payment_ids == [PAYMENT_ID]
    # forged, replayed (stale timestamp) and unsigned deliveries
    forged_raw, forged = _tab_signed(body, secret="other")
    for bad_raw, bad_headers in (
        (forged_raw, forged),
        (_tab_signed(body, ts=int(time.time()) - 3600)[0], _tab_signed(body, ts=int(time.time()) - 3600)[1]),
        (raw, {}),
        (raw.replace(b"19900", b"1"), headers),
    ):
        with pytest.raises(WebhookRejected):
            await gw.parse_webhook(bad_raw, bad_headers, None)
    test_raw, test_headers = _tab_signed({**body, "id": "test-1", "test": True})
    assert (await gw.parse_webhook(test_raw, test_headers, None)).payment_ids == []


# ---------------------------------------------------------------------------------------------
# SeverPay
# ---------------------------------------------------------------------------------------------
SEVER_TOKEN = "sever_token"


def _sever(fake: FakeProvider, closing: list[PaymentGateway], **kw: Any) -> SeverPayGateway:
    gw = SeverPayGateway(7, SEVER_TOKEN, base_url=fake.base_url, **kw)
    gw._retry_delay = 0
    closing.append(gw)
    return gw


def _sever_data(status: str, **kw: Any) -> dict[str, Any]:
    return {"id": 123, "uid": "U1", "order_id": PAYMENT_ID, "amount": 199.5, "currency": "RUB", "status": status, **kw}


async def test_severpay_create_is_signed_over_the_sorted_body(
    fake: FakeProvider, closing: list[PaymentGateway]
) -> None:
    fake.reply("POST", "/payin/create", (200, {"status": True, "msg": "", "data": {"id": 123, "url": "https://sp/x"}}))
    gw = _sever(fake, closing, return_url="https://t.me/bot")
    invoice = await gw.create(make_payment(19950), make_user(), "VPN")
    assert invoice.url == "https://sp/x" and invoice.provider_payment_id == "123"
    req = fake.requests[0]
    body = req.json()
    sign = body.pop("sign")
    assert body["mid"] == 7 and body["salt"]
    assert body["order_id"] == PAYMENT_ID and body["amount"] == 199.5 and body["currency"] == "RUB"
    assert body["client_email"] == "5512345678@telegram.org" and body["client_id"] == "5512345678"
    assert body["url_return"] == "https://t.me/bot"
    assert list(body) == sorted(body)  # keys are sorted
    signed_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    assert sign == _mac(SEVER_TOKEN, signed_text.encode())


async def test_severpay_whole_rubles_are_sent_as_integers(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("POST", "/payin/create", (200, {"status": True, "data": {"id": 1, "url": "u"}}))
    await _sever(fake, closing).create(make_payment(19900), make_user(), "VPN")
    assert fake.requests[0].json()["amount"] == 199


async def test_severpay_errors(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("POST", "/payin/create", (200, {"status": False, "msg": "Invalid sign", "data": None}))
    with pytest.raises(GatewayError, match="Invalid sign"):
        await _sever(fake, closing).create(make_payment(), make_user(), "VPN")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("new", PaymentStatus.PENDING),
        ("process", PaymentStatus.PENDING),
        ("success", PaymentStatus.SUCCEEDED),
        ("decline", PaymentStatus.CANCELED),
        ("fail", PaymentStatus.CANCELED),
    ],
)
async def test_severpay_fetch_status_mapping(
    fake: FakeProvider, closing: list[PaymentGateway], raw: str, expected: PaymentStatus
) -> None:
    fake.reply("POST", "/payin/get", (200, {"status": True, "msg": "", "data": _sever_data(raw)}))
    status = await _sever(fake, closing).fetch(make_payment(provider_payment_id="123"))
    assert status.status is expected
    assert status.paid_amount_kop == (19950 if expected is PaymentStatus.SUCCEEDED else None)
    assert fake.requests[0].json()["id"] == 123


async def test_severpay_fetch_without_provider_id_is_pending(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    status = await _sever(fake, closing).fetch(make_payment())
    assert status.status is PaymentStatus.PENDING and not fake.requests


def test_severpay_status_rejects_foreign_order_and_currency() -> None:
    for bad in (_sever_data("success", order_id="x"), _sever_data("success", currency="USD"), _sever_data("odd")):
        with pytest.raises(GatewayError):
            severpay.payment_status(bad, PAYMENT_ID)


def _sever_webhook(body: dict[str, Any], *, php: bool = True, token: str = SEVER_TOKEN) -> bytes:
    text = json.dumps(body, ensure_ascii=php, separators=(",", ":"))
    if php:
        text = text.replace("/", "\\/")
    return json.dumps({**body, "sign": _mac(token, text.encode())}).encode()


async def test_severpay_webhook(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _sever(fake, closing)
    body = {
        "type": "payin",
        "data": {"id": 123, "order_id": PAYMENT_ID, "amount": 100.50, "currency": "RUB", "status": "success"},
        "salt": "a9f3c2d8e4b1",
    }
    result = await gw.parse_webhook(_sever_webhook(body), {}, None)
    assert result.payment_ids == [PAYMENT_ID] and result.provider_ids == ["123"]
    assert result.response_body == '{"status": true}'
    # unicode and slashes are encoded the PHP way, but an unescaped signing scheme is accepted as well
    odd = {**body, "data": {**body["data"], "note": "оплата a/b"}}
    assert (await gw.parse_webhook(_sever_webhook(odd), {}, None)).payment_ids == [PAYMENT_ID]
    assert (await gw.parse_webhook(_sever_webhook(odd, php=False), {}, None)).payment_ids == [PAYMENT_ID]
    for raw in (
        _sever_webhook(body, token="other"),
        json.dumps(body).encode(),
        _sever_webhook(body).replace(b"success", b"fail_x"),
    ):
        with pytest.raises(WebhookRejected):
            await gw.parse_webhook(raw, {}, None)
    ping = {"type": "test", "data": {"timestamp": 1735555200}, "salt": "c4"}
    assert (await gw.parse_webhook(_sever_webhook(ping), {}, None)).payment_ids == []


# ---------------------------------------------------------------------------------------------
# Cashera
# ---------------------------------------------------------------------------------------------
def _cashera(fake: FakeProvider, closing: list[PaymentGateway], **kw: Any) -> CasheraGateway:
    gw = CasheraGateway("pk_live", "sk_live", base_url=fake.base_url, **kw)
    gw._retry_delay = 0
    closing.append(gw)
    return gw


def _cashera_obj(status: str, **kw: Any) -> dict[str, Any]:
    return {
        "uuid": PROVIDER_ID,
        "type": "deposit",
        "amount": 19900,
        "currency": "RUB",
        "status": status,
        "external_id": PAYMENT_ID,
        **kw,
    }


async def test_cashera_create_request_shape(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("POST", "/transactions", (201, _cashera_obj("pending", payment_url="pay.cashera.cash/abc")))
    gw = _cashera(
        fake,
        closing,
        payment_method="sbp",
        callback_url="https://bot.example/webhooks/cashera",
        return_url="https://t.me/b",
    )
    invoice = await gw.create(make_payment(19950), make_user(), "VPN")
    assert invoice.url == "https://pay.cashera.cash/abc"  # the API omits the scheme
    assert invoice.provider_payment_id == PROVIDER_ID
    req = fake.requests[0]
    assert req.headers["X-Api-Key"] == "pk_live"
    assert req.json() == {
        "amount": 19950,
        "currency": "RUB",
        "external_id": PAYMENT_ID,
        "description": "VPN",
        "payment_method": "sbp",
        "callback_url": "https://bot.example/webhooks/cashera",
        "success_url": "https://t.me/b",
        "fail_url": "https://t.me/b",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("pending", PaymentStatus.PENDING),
        ("paid", PaymentStatus.SUCCEEDED),
        ("canceled", PaymentStatus.CANCELED),
        ("failed", PaymentStatus.CANCELED),
        ("expired", PaymentStatus.EXPIRED),
        ("refunded", PaymentStatus.REFUNDED),
        ("chargeback", PaymentStatus.REFUNDED),
    ],
)
async def test_cashera_fetch_status_mapping(
    fake: FakeProvider, closing: list[PaymentGateway], raw: str, expected: PaymentStatus
) -> None:
    fake.reply("GET", f"/transactions/{PROVIDER_ID}", (200, _cashera_obj(raw)))
    status = await _cashera(fake, closing).fetch(make_payment(provider_payment_id=PROVIDER_ID))
    assert status.status is expected
    assert status.paid_amount_kop == (19900 if expected is PaymentStatus.SUCCEEDED else None)


async def test_cashera_fetch_by_external_id_and_validation(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("GET", f"/transactions/by-external-id/{PAYMENT_ID}", (200, _cashera_obj("paid")))
    assert (await _cashera(fake, closing).fetch(make_payment())).status is PaymentStatus.SUCCEEDED
    for bad in (
        _cashera_obj("paid", external_id="other"),
        _cashera_obj("paid", currency="USD"),
        _cashera_obj("paid", type="payout"),
        _cashera_obj("odd"),
    ):
        with pytest.raises(GatewayError):
            cashera.payment_status(bad, PAYMENT_ID)


async def test_cashera_webhook(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _cashera(fake, closing)
    raw = json.dumps(
        {"event": "transaction.status_updated", "transaction": _cashera_obj("paid")},
    ).encode()
    good = {"x-api-key": "pk_live", "x-secret": "sk_live"}
    result = await gw.parse_webhook(raw, good, None)
    assert result.payment_ids == [PAYMENT_ID] and result.provider_ids == [PROVIDER_ID]
    for headers in (
        {},
        {"X-Api-Key": "pk_live"},
        {"X-Api-Key": "pk_live", "X-Secret": "nope"},
        {"X-Secret": "sk_live"},
    ):
        with pytest.raises(WebhookRejected):
            await gw.parse_webhook(raw, headers, None)
    payout = json.dumps({"event": "payout.status_updated", "payout": {"uuid": "x"}}).encode()
    assert (await gw.parse_webhook(payout, good, None)).payment_ids == []


# ---------------------------------------------------------------------------------------------
# ParityPay and Aurapay (concatenated-values signature)
# ---------------------------------------------------------------------------------------------
KEY2 = "secret-key-2"


def test_concat_signature_rules() -> None:
    body = {"b": "2", "a": None, "c": Decimal("957.0"), "d": 5, "e": True}
    assert concat_signature.concatenated(body, plain=False) == "" + "2" + "957.0" + "5" + "1"
    assert concat_signature.concatenated(body, plain=True) == "2" + "957" + "5" + "1"
    expected = _mac(KEY2, b"2" + b"957.0" + b"5" + b"1")
    assert concat_signature.verify(KEY2, body, expected)
    assert concat_signature.verify(KEY2, body, _mac(KEY2, b"29575" + b"1"))  # PHP-style number rendering
    assert not concat_signature.verify(KEY2, body, _mac("other", b"2957.051"))
    assert not concat_signature.verify(KEY2, {"x": [1]}, "00")  # nested values cannot be signed
    parsed = concat_signature.parse_body(b'{"credited": 1209.01, "amount": "1250.00"}')
    assert parsed == {"credited": Decimal("1209.01"), "amount": "1250.00"}
    with pytest.raises(ValueError):
        concat_signature.parse_body(b"[1]")


def _parity(fake: FakeProvider, closing: list[PaymentGateway], **kw: Any) -> ParityPayGateway:
    gw = ParityPayGateway("shop-1", "key1", KEY2, base_url=fake.base_url, **kw)
    gw._retry_delay = 0
    closing.append(gw)
    return gw


def _aura(fake: FakeProvider, closing: list[PaymentGateway], **kw: Any) -> AuraPayGateway:
    gw = AuraPayGateway("shop-1", "api-1", KEY2, base_url=fake.base_url, **kw)
    gw._retry_delay = 0
    closing.append(gw)
    return gw


def _invoice(status: str, **kw: Any) -> dict[str, Any]:
    return {"id": PROVIDER_ID, "order_id": PAYMENT_ID, "amount": 199, "status": status, **kw}


async def test_paritypay_create_request_shape(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("POST", "/v2/invoice/create", (200, _invoice("NEW", link="https://pay.paritypay.net/x")))
    gw = _parity(
        fake, closing, service="card", callback_url="https://b/webhooks/paritypay", return_url="https://t.me/b"
    )
    invoice = await gw.create(make_payment(19950), make_user(), "VPN")
    assert invoice.url == "https://pay.paritypay.net/x" and invoice.provider_payment_id == PROVIDER_ID
    req = fake.requests[0]
    assert req.headers["X-ShopId"] == "shop-1" and req.headers["X-SecretKey"] == "key1"
    assert req.json() == {
        "order_id": PAYMENT_ID,
        "amount": 199.5,
        "comment": "VPN",
        "service": "card",
        "callback_url": "https://b/webhooks/paritypay",
        "success_url": "https://t.me/b",
        "fail_url": "https://t.me/b",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NEW", PaymentStatus.PENDING),
        ("PAID", PaymentStatus.SUCCEEDED),
        ("EXPIRED", PaymentStatus.EXPIRED),
        ("ERROR", PaymentStatus.CANCELED),
        ("REFUNDED", PaymentStatus.REFUNDED),
    ],
)
async def test_paritypay_fetch_status_mapping(
    fake: FakeProvider, closing: list[PaymentGateway], raw: str, expected: PaymentStatus
) -> None:
    fake.reply("GET", "/v2/invoice/status", (200, _invoice(raw, amount=199.5)))
    status = await _parity(fake, closing).fetch(make_payment(provider_payment_id=PROVIDER_ID))
    assert fake.requests[0].query == {"order_id": PAYMENT_ID}
    assert status.status is expected
    assert status.paid_amount_kop == (19950 if expected is PaymentStatus.SUCCEEDED else None)
    with pytest.raises(GatewayError):
        paritypay.payment_status(_invoice("PAID", order_id="x"), PAYMENT_ID)


async def test_aurapay_create_and_fetch(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("POST", "/invoice/create", (200, {**_invoice("PENDING"), "payment_data": {"url": "https://pay.aura/x"}}))
    fake.reply("POST", "/invoice/status", (200, _invoice("PAID", amount=199)))
    gw = _aura(fake, closing, service="sbp")
    invoice = await gw.create(make_payment(19900), make_user(), "")
    assert invoice.url == "https://pay.aura/x" and invoice.provider_payment_id == PROVIDER_ID
    create = fake.requests[0]
    assert create.headers["X-ApiKey"] == "api-1" and create.headers["X-ShopId"] == "shop-1"
    assert create.json() == {"order_id": PAYMENT_ID, "amount": 199, "comment": "VPN", "service": "sbp"}
    status = await gw.fetch(make_payment(provider_payment_id=PROVIDER_ID))
    assert fake.requests[1].json() == {"order_id": PAYMENT_ID}
    assert status.status is PaymentStatus.SUCCEEDED and status.paid_amount_kop == 19900
    for raw, expected in (("PENDING", PaymentStatus.PENDING), ("EXPIRED", PaymentStatus.EXPIRED)):
        assert aurapay.payment_status(_invoice(raw), PAYMENT_ID).status is expected
    with pytest.raises(GatewayError):
        aurapay.payment_status(_invoice("ERROR"), PAYMENT_ID)


def _concat_signed(body: dict[str, Any], key: str = KEY2) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    return raw, {"X-SIGNATURE": concat_signature.sign(key, concat_signature.parse_body(raw))}


@pytest.mark.parametrize("name", ["parity", "aura"])
async def test_concat_webhooks(fake: FakeProvider, closing: list[PaymentGateway], name: str) -> None:
    gw = _parity(fake, closing) if name == "parity" else _aura(fake, closing)
    body = {
        "id": PROVIDER_ID,
        "order_id": PAYMENT_ID,
        "shop_id": "shop-1",
        "amount": "199.00",
        "credited": 192.5,
        "comment": None,
        "service": "sbp",
        "status": "PAID",
    }
    raw, headers = _concat_signed(body)
    result = await gw.parse_webhook(raw, {k.lower(): v for k, v in headers.items()}, None)
    assert result.payment_ids == [PAYMENT_ID] and result.provider_ids == [PROVIDER_ID]
    forged_raw, forged = _concat_signed(body, key="other")
    for bad_raw, bad_headers in (
        (forged_raw, forged),
        (raw, {}),
        (raw.replace(b"199.00", b"1.00"), headers),
        (b"[1]", headers),
    ):
        with pytest.raises(WebhookRejected):
            await gw.parse_webhook(bad_raw, bad_headers, None)


# ---------------------------------------------------------------------------------------------
# Payment service: a webhook only triggers a re-fetch; credit is idempotent
# ---------------------------------------------------------------------------------------------
async def _pending(sf: SessionFactory, provider: str) -> None:
    async with sf.begin() as s:
        await add_user(s, USER_ID)
        s.add(
            Payment(id=PAYMENT_ID, provider=provider, user_id=USER_ID, amount_kop=19900, status=PaymentStatus.PENDING)
        )


async def _credits(sf: SessionFactory) -> int:
    async with sf() as s:
        return int(await s.scalar(select(func.count()).select_from(BalanceTransaction)) or 0)


async def test_service_credits_after_refetch_exactly_once(
    session_factory: SessionFactory, fake: FakeProvider, closing: list[PaymentGateway]
) -> None:
    settings = make_settings()
    gw = _cis(fake, closing)
    await _pending(session_factory, gw.key)
    raw = json.dumps(_cis_obj("PAID"), separators=(",", ":")).encode()
    headers = {"X-Signature": cispay.sign(CIS_KEY, raw)}

    # Signed "paid" notice, but the provider still reports the payment as unpaid: nothing is credited.
    fake.reply("GET", "/payments/status", (200, _cis_obj("PENDING")))
    await service.handle_webhook(session_factory, settings, gw, raw, headers, None)
    assert await _credits(session_factory) == 0

    # The provider confirms: credited once, and a redelivery changes nothing.
    fake.reply("GET", "/payments/status", (200, _cis_obj("PAID")))
    await service.handle_webhook(session_factory, settings, gw, raw, headers, None)
    await service.handle_webhook(session_factory, settings, gw, raw, headers, None)
    assert await _credits(session_factory) == 1
    async with session_factory() as s:
        payment = await s.get(Payment, PAYMENT_ID)
        assert payment is not None and payment.status is PaymentStatus.SUCCEEDED


async def test_service_underpayment_is_not_credited_in_full(
    session_factory: SessionFactory, fake: FakeProvider, closing: list[PaymentGateway]
) -> None:
    settings = make_settings()
    gw = _tab(fake, closing)
    await _pending(session_factory, gw.key)
    fake.reply("GET", f"/v1/payments/{PROVIDER_ID}", (200, _tab_obj("SUCCESS", amountKopecks=100)))
    raw, headers = _tab_signed({"id": PROVIDER_ID, "orderId": PAYMENT_ID, "status": "SUCCESS", "test": False})
    await service.handle_webhook(session_factory, settings, gw, raw, headers, None)
    async with session_factory() as s:
        payment = await s.get(Payment, PAYMENT_ID)
        assert payment is not None and payment.status is not PaymentStatus.SUCCEEDED
    assert await _credits(session_factory) == 0


# ---------------------------------------------------------------------------------------------
# Settings and registry
# ---------------------------------------------------------------------------------------------
_ENVS: dict[str, dict[str, Any]] = {
    "cispay": {"cispay_shop_id": "s", "cispay_api_key": "k", "cispay_payment_method": "card"},
    "tabpay": {"tabpay_api_key": "k", "tabpay_webhook_secret": "s", "tabpay_payment_method": "sbp"},
    "severpay": {"severpay_mid": 5, "severpay_token": "t"},
    "cashera": {"cashera_api_key": "k", "cashera_secret": "s", "cashera_payment_method": "sbp"},
    "paritypay": {"paritypay_shop_id": "s", "paritypay_secret_key": "k", "paritypay_secret_key2": "k2"},
    "aurapay": {"aurapay_shop_id": "s", "aurapay_api_key": "k", "aurapay_secret_key2": "k2"},
}


@pytest.mark.parametrize("name", sorted(_ENVS))
async def test_registry_builds_gateway_and_validates_settings(name: str) -> None:
    env: dict[str, Any] = {
        f"{name}_enabled": True,
        **_ENVS[name],
        "public_base_url": "https://bot.example.com",
        "trusted_proxies": TRUSTED,
    }
    gateways = build_gateways(make_settings(**env))
    try:
        gw = gateways[PaymentProvider(name).value]
        assert gw.supports_polling
    finally:
        await close_gateways(gateways)
    for field in _ENVS[name]:
        if field.endswith("method"):
            continue
        with pytest.raises(ValueError):
            make_settings(**{**env, field: None})
    with pytest.raises(ValueError):
        make_settings(**{**env, "public_base_url": None})
    for method_field in (f for f in _ENVS[name] if f.endswith("method")):
        with pytest.raises(ValueError):
            make_settings(**{**env, method_field: "crypto"})


def test_constructor_validation() -> None:
    with pytest.raises(ValueError):
        CisPayGateway("", "k")
    with pytest.raises(ValueError):
        CisPayGateway("s", "k", payment_method="crypto")
    with pytest.raises(ValueError):
        TabPayGateway("k", "")
    with pytest.raises(ValueError):
        SeverPayGateway(0, "t")
    with pytest.raises(ValueError):
        CasheraGateway("k", "")
    with pytest.raises(ValueError):
        ParityPayGateway("s", "k", "")
    with pytest.raises(ValueError):
        AuraPayGateway("s", "k", "k2", service="crypto")


async def test_registry_passes_the_webhook_url_to_providers_that_take_one() -> None:
    env: dict[str, Any] = {
        "cashera_enabled": True,
        **_ENVS["cashera"],
        "public_base_url": "https://bot.example.com/",
        "trusted_proxies": TRUSTED,
    }
    gateways = build_gateways(make_settings(**env))
    try:
        gw = gateways["cashera"]
        assert isinstance(gw, CasheraGateway)
        assert gw._callback_url == "https://bot.example.com/webhooks/cashera"
    finally:
        await close_gateways(gateways)
