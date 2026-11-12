"""Owner-configured HTTP gateway: request templates, response mapping, status polling and webhooks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qsl

import pytest

from app.db.models import PaymentProvider, PaymentStatus
from app.services.payments.base import GatewayError, PaymentGateway, WebhookRejected
from app.services.payments.custom_http import (
    CustomGatewayConfigError,
    CustomHttpConfig,
    CustomHttpGateway,
    config_errors,
)
from app.services.payments.registry import build_gateways
from tests.conftest import make_settings
from tests.test_payments_gateways import (  # noqa: F401  (fixtures)
    PAYMENT_ID,
    FakeProvider,
    closing,
    fake,
    make_payment,
    make_user,
)

SECRET = "hook-secret"
TRICKY = 'Top-up "VIP" & more\\ {payment_id} =x'
STATUS_VALUES = json.dumps({"paid": "succeeded", "failed": "canceled", "new": "pending"})


def base_config(base_url: str = "https://pay.example", **kw: Any) -> CustomHttpConfig:
    values: dict[str, Any] = {
        "title": "💳 Карта (MyPay)",
        "api_key": "api-key-1",
        "create_url": f"{base_url}/invoices",
        "headers": json.dumps({"Authorization": "Bearer {api_key}", "Idempotency-Key": "{payment_id}"}),
        "create_body": json.dumps(
            {
                "order": "{payment_id}",
                "amount": "{amount}",
                "amount_kop": "{amount_kop}",
                "sum": "{amount_number}",
                "text": "Payment: {description}",
                "meta": {"user": "{user_id}", "back": "{return_url}", "hook": "{callback_url}"},
                "currency": "{currency}",
            }
        ),
        "response_url_path": "data.links.0.href",
        "response_id_path": "data.id",
        "status_url": f"{base_url}/invoices/{{provider_id}}",
        "status_path": "data.state",
        "status_values": STATUS_VALUES,
        "status_amount_path": "data.paid",
        "webhook_secret": SECRET,
        "webhook_signature_header": "X-Signature",
        "webhook_payment_id_path": "order",
        "webhook_provider_id_path": "invoice.id",
        "webhook_status_path": "state",
    }
    values.update(kw)
    return CustomHttpConfig(**values)


def make_gateway(config: CustomHttpConfig, **kw: Any) -> CustomHttpGateway:
    gw = CustomHttpGateway(
        config,
        return_url="https://t.me/TestBot",
        callback_url="https://bot.example/webhooks/custom",
        trusted_proxies=["127.0.0.1", "10.0.0.0/8"],
        **kw,
    )
    gw._retry_delay = 0
    return gw


def sign(raw: bytes, algo: str = "sha256", encoding: str = "hex", secret: str = SECRET) -> str:
    mac = hmac.new(secret.encode(), raw, getattr(hashlib, algo)).digest()
    return base64.b64encode(mac).decode() if encoding == "base64" else mac.hex()


INVOICE_REPLY = {"data": {"id": "inv-42", "links": [{"href": "https://pay.example/checkout/inv-42"}]}}


# ---------------------------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------------------------
async def test_create_json_template_escapes_values(fake: FakeProvider, closing: list[PaymentGateway]) -> None:  # noqa: F811
    fake.reply("POST", "/invoices", (201, INVOICE_REPLY))
    gw = make_gateway(base_config(fake.base_url))
    closing.append(gw)

    invoice = await gw.create(make_payment(19950), make_user(), TRICKY)

    assert invoice.provider_payment_id == "inv-42"
    assert invoice.url == "https://pay.example/checkout/inv-42"
    assert invoice.provider_amount == "199.50" and invoice.provider_currency == "RUB"
    sent = fake.requests[-1]
    assert sent.headers["Authorization"] == "Bearer api-key-1"
    assert sent.headers["Idempotency-Key"] == PAYMENT_ID
    assert sent.headers["Content-Type"] == "application/json"
    body = sent.json()
    assert body == {
        "order": PAYMENT_ID,
        "amount": "199.50",
        "amount_kop": 19950,
        "sum": 199.5,
        "text": f"Payment: {TRICKY}",  # quotes, backslash and braces stay inside the string
        "meta": {"user": 5512345678, "back": "https://t.me/TestBot", "hook": "https://bot.example/webhooks/custom"},
        "currency": "RUB",
    }


async def test_create_form_template_escapes_ampersands(fake: FakeProvider, closing: list[PaymentGateway]) -> None:  # noqa: F811
    config = base_config(
        fake.base_url,
        create_url=f"{fake.base_url}/pay/{{payment_id}}?d={{description}}",
        body_format="form",
        create_body="order={payment_id}&sum={amount}&desc={description}&key={api_key}",
        response_url_path="url",
        response_id_path="id",
        status_url=None,
        status_path=None,
    )
    gw = make_gateway(config)
    closing.append(gw)
    fake.reply("POST", f"/pay/{PAYMENT_ID}", (200, {"url": "https://pay.example/c/1", "id": 7}))

    invoice = await gw.create(make_payment(), make_user(), TRICKY)

    assert invoice.provider_payment_id == "7" and invoice.url == "https://pay.example/c/1"
    sent = fake.requests[-1]
    assert sent.query == {"d": TRICKY}
    assert sent.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert dict(parse_qsl(sent.body.decode(), strict_parsing=True)) == {
        "order": PAYMENT_ID,
        "sum": "199.00",
        "desc": TRICKY,
        "key": "api-key-1",
    }


async def test_create_rejects_missing_or_unsafe_payment_url(fake: FakeProvider, closing: list[PaymentGateway]) -> None:  # noqa: F811
    fake.reply(
        "POST",
        "/invoices",
        (200, {"data": {"id": "1", "links": [{"href": "javascript:alert(1)"}]}}),
        (200, {"data": {"id": "1"}}),
        (200, {"data": {"links": [{"href": "https://pay.example/x"}]}}),
        (400, {"error": "bad"}),
    )
    gw = make_gateway(base_config(fake.base_url))
    closing.append(gw)
    for _ in range(4):
        with pytest.raises(GatewayError):
            await gw.create(make_payment(), make_user(), "x")


# ---------------------------------------------------------------------------------------------
# Status polling
# ---------------------------------------------------------------------------------------------
async def test_fetch_maps_status_and_amount(fake: FakeProvider, closing: list[PaymentGateway]) -> None:  # noqa: F811
    fake.reply(
        "GET",
        "/invoices/inv-42",
        (200, {"data": {"state": "PAID", "paid": "199.5"}}),
        (200, {"data": {"state": "failed"}}),
        (200, {"data": {"state": "something-new"}}),
        (200, {"data": {}}),
        (200, {"data": {"state": "paid", "paid": "lots"}}),
    )
    gw = make_gateway(base_config(fake.base_url))
    closing.append(gw)
    assert gw.supports_polling and not gw.polls_by_payment_id
    payment = make_payment(provider_payment_id="inv-42")

    paid = await gw.fetch(payment)
    assert paid.status is PaymentStatus.SUCCEEDED and paid.paid_amount_kop == 19950
    assert paid.provider_payment_id == "inv-42"
    assert fake.requests[-1].headers["Authorization"] == "Bearer api-key-1"
    assert (await gw.fetch(payment)).status is PaymentStatus.CANCELED
    assert (await gw.fetch(payment)).status is PaymentStatus.PENDING
    with pytest.raises(GatewayError):
        await gw.fetch(payment)
    with pytest.raises(GatewayError):
        await gw.fetch(payment)
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment())  # no provider id to put into the URL


async def test_fetch_by_our_id_with_post_body(fake: FakeProvider, closing: list[PaymentGateway]) -> None:  # noqa: F811
    fake.reply("POST", "/status", (200, {"result": {"ok": True}}))
    config = base_config(
        fake.base_url,
        status_method="POST",
        status_url=f"{fake.base_url}/status",
        status_body=json.dumps({"order": "{payment_id}"}),
        status_path="result.ok",
        status_values=json.dumps({"true": "succeeded", "false": "pending"}),
        status_amount_path=None,
    )
    gw = make_gateway(config)
    closing.append(gw)
    assert gw.polls_by_payment_id
    status = await gw.fetch(make_payment())
    assert status.status is PaymentStatus.SUCCEEDED and status.paid_amount_kop is None
    assert fake.requests[-1].json() == {"order": PAYMENT_ID}


# ---------------------------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------------------------
async def test_webhook_with_status_request_only_identifies_payment() -> None:
    gw = make_gateway(base_config())
    raw = json.dumps({"order": PAYMENT_ID, "invoice": {"id": "inv-42"}, "state": "paid"}).encode()
    result = await gw.parse_webhook(raw, {"X-Signature": sign(raw)}, "203.0.113.5")
    assert result.payment_ids == [PAYMENT_ID] and result.provider_ids == ["inv-42"]
    assert result.trusted_status is None  # the status is re-fetched from the provider
    # A "sha256=" prefix and upper-case hex are accepted.
    result = await gw.parse_webhook(raw, {"x-signature": "sha256=" + sign(raw).upper()}, None)
    assert result.payment_ids == [PAYMENT_ID]


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Signature": ""}, {"X-Signature": "00" * 32}, {"X-Signature": sign(b"other body")}],
)
async def test_webhook_rejects_bad_signature(headers: dict[str, str]) -> None:
    gw = make_gateway(base_config())
    raw = json.dumps({"order": PAYMENT_ID, "state": "paid"}).encode()
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(raw, headers, None)
    assert exc.value.status_code == 401


async def test_webhook_rejects_tampered_body() -> None:
    gw = make_gateway(base_config(status_url=None, status_path=None))
    raw = json.dumps({"order": PAYMENT_ID, "state": "failed"}).encode()
    signature = sign(raw)
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw.replace(b"failed", b"paid"), {"X-Signature": signature}, None)


async def test_webhook_trusted_status_without_status_request() -> None:
    gw = make_gateway(
        base_config(
            status_url=None,
            status_path=None,
            webhook_amount_path="amount",
            webhook_hash="sha512",
            webhook_signature_encoding="base64",
        )
    )
    assert not gw.supports_polling
    raw = json.dumps({"order": PAYMENT_ID, "state": "paid", "amount": "150.75"}).encode()
    result = await gw.parse_webhook(raw, {"X-Signature": sign(raw, "sha512", "base64")}, None)
    assert result.payment_ids == [PAYMENT_ID]
    assert result.trusted_status is not None
    assert result.trusted_status.status is PaymentStatus.SUCCEEDED
    assert result.trusted_status.paid_amount_kop == 15075
    assert result.trusted_status.provider_payment_id is None
    with pytest.raises(WebhookRejected):  # hex signature while base64 is configured
        await gw.parse_webhook(raw, {"X-Signature": sign(raw, "sha512")}, None)

    canceled = json.dumps({"order": PAYMENT_ID, "state": "failed"}).encode()
    result = await gw.parse_webhook(canceled, {"X-Signature": sign(canceled, "sha512", "base64")}, None)
    assert result.trusted_status is not None and result.trusted_status.status is PaymentStatus.CANCELED

    for body in ({"order": PAYMENT_ID}, {"order": PAYMENT_ID, "state": "paid", "amount": "n/a"}, {"state": "paid"}):
        raw = json.dumps(body).encode()
        with pytest.raises(WebhookRejected) as exc:
            await gw.parse_webhook(raw, {"X-Signature": sign(raw, "sha512", "base64")}, None)
        assert exc.value.status_code == 400


async def test_webhook_form_body_and_malformed_body() -> None:
    gw = make_gateway(base_config(status_url=None, status_path=None, webhook_provider_id_path=None))
    raw = f"order={PAYMENT_ID}&state=paid".encode()
    result = await gw.parse_webhook(raw, {"X-Signature": sign(raw)}, None)
    assert result.trusted_status is not None and result.trusted_status.status is PaymentStatus.SUCCEEDED
    for raw in (b"", b"\xff\xfe", b"not a form"):
        with pytest.raises(WebhookRejected) as exc:
            await gw.parse_webhook(raw, {"X-Signature": sign(raw)}, None)
        assert exc.value.status_code == 400


async def test_webhook_ip_allow_list() -> None:
    gw = make_gateway(base_config(webhook_allowed_ips=("203.0.113.0/24",)))
    raw = json.dumps({"order": PAYMENT_ID}).encode()
    headers = {"X-Signature": sign(raw)}
    assert (await gw.parse_webhook(raw, headers, "203.0.113.9")).payment_ids == [PAYMENT_ID]
    # Behind a trusted reverse proxy the forwarded client address is checked.
    proxied = {**headers, "X-Forwarded-For": "203.0.113.9"}
    assert (await gw.parse_webhook(raw, proxied, "127.0.0.1")).payment_ids == [PAYMENT_ID]
    for remote, extra in (("198.51.100.1", {}), (None, {}), ("198.51.100.1", {"X-Forwarded-For": "203.0.113.9"})):
        with pytest.raises(WebhookRejected) as exc:
            await gw.parse_webhook(raw, {**headers, **extra}, remote)
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------------
def test_valid_config_has_no_errors() -> None:
    assert config_errors(base_config()) == []
    assert config_errors(base_config(status_url=None, status_path=None)) == []


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"create_url": None}, "create URL is required"),
        ({"create_url": "ftp://pay.example/x"}, "http(s)"),
        ({"create_url": "file:///etc/passwd"}, "http(s)"),
        ({"create_url": "https://{description}.example/x"}, "host"),
        ({"create_url": "https://pay.example/x?key={api_key}"}, "must not be placed in a URL"),
        ({"create_url": "https://pay.example/{oops}"}, "unknown placeholder"),
        ({"create_body": "{not json"}, "not valid JSON"),
        ({"create_body": json.dumps({"x": "{provider_id}"})}, "unknown placeholder"),
        ({"body_format": "form", "create_body": "a=1&&b"}, "form template"),
        ({"create_method": "GET"}, "not allowed with GET"),
        ({"headers": "[1]"}, "JSON object"),
        ({"headers": json.dumps({"Bad Header": "x"})}, "invalid header name"),
        ({"headers": json.dumps({"X-A": "a\r\nInjected: 1"})}, "single line"),
        ({"response_url_path": None}, "payment URL path is required"),
        ({"response_url_path": "data..url"}, "invalid path"),
        ({"status_values": None}, "mapping is required"),
        ({"status_values": json.dumps({"paid": "done"})}, "succeeded, canceled or pending"),
        ({"status_values": json.dumps({"x": "pending"})}, "at least one"),
        ({"status_path": None}, "status field path is required"),
        ({"response_id_path": None}, "{provider_id}"),
        ({"status_url": None, "status_path": None, "webhook_status_path": None}, "webhook status path"),
        ({"webhook_secret": ""}, "HMAC secret is required"),
        ({"webhook_signature_header": None}, "signature header is required"),
        ({"webhook_hash": "md5"}, "sha256 or sha512"),
        ({"webhook_allowed_ips": ("not-an-ip",)}, "invalid entry"),
        ({"webhook_payment_id_path": None, "webhook_provider_id_path": None}, "webhook payment id path"),
        ({"title": " "}, "button title"),
    ],
)
def test_bad_config_is_rejected(changes: dict[str, Any], fragment: str) -> None:
    config = replace(base_config(), **changes)
    errors = config_errors(config)
    assert any(fragment in error for error in errors), errors
    with pytest.raises(CustomGatewayConfigError):
        CustomHttpGateway(config)


def test_config_errors_never_echo_secrets() -> None:
    config = base_config(headers=json.dumps({"Bad Header": "super-secret-token"}), webhook_hash="md5")
    assert "super-secret-token" not in "; ".join(config_errors(config))


SETTINGS = {
    "custom_enabled": True,
    "custom_title": "💳 MyPay",
    "custom_api_key": "k",
    "custom_create_url": "https://pay.example/invoices",
    "custom_create_body": json.dumps({"order": "{payment_id}", "amount": "{amount}"}),
    "custom_response_url_path": "url",
    "custom_status_values": STATUS_VALUES,
    "custom_webhook_secret": SECRET,
    "custom_webhook_signature_header": "X-Signature",
    "custom_webhook_payment_id_path": "order",
    "custom_webhook_status_path": "state",
    "custom_webhook_allowed_ips": "203.0.113.0/24, 198.51.100.7",
}


async def test_settings_validation_and_registry() -> None:
    settings = make_settings(**SETTINGS)
    assert settings.custom_webhook_allowed_ips == ["203.0.113.0/24", "198.51.100.7"]
    gateways = build_gateways(settings, 5, return_url="https://t.me/TestBot")
    gw = gateways[PaymentProvider.CUSTOM.value]
    assert isinstance(gw, CustomHttpGateway) and isinstance(gw, PaymentGateway)
    assert gw.key == "custom" and gw.title == "💳 MyPay" and not gw.supports_polling
    assert gw._callback_url == "https://bot.example.com/webhooks/custom"
    await gw.close()

    with pytest.raises(ValueError, match="webhook HMAC secret is required"):
        make_settings(**{**SETTINGS, "custom_webhook_secret": None})
    with pytest.raises(ValueError, match="http"):
        make_settings(**{**SETTINGS, "custom_create_url": "javascript:alert(1)"})
    make_settings(**{**SETTINGS, "custom_enabled": False, "custom_webhook_secret": None})  # not validated when off
