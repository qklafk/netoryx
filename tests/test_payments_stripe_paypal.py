"""Stripe Checkout and PayPal Orders gateways against local fake HTTP servers."""
# ruff: noqa: F811  (pytest fixtures imported from the shared gateway tests)

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl

import pytest

from app.db.models import PaymentStatus
from app.services.payments import fx, stripe
from app.services.payments.base import GatewayError, PaymentGateway, WebhookRejected
from app.services.payments.paypal import PayPalGateway
from app.services.payments.registry import build_gateways, close_gateways
from app.services.payments.stripe import StripeGateway
from tests.conftest import make_settings
from tests.test_payments_gateways import (  # noqa: F401  (fixtures)
    PAYMENT_ID,
    FakeProvider,
    Recorded,
    closing,
    fake,
    make_payment,
    make_user,
)

NOW = 1_700_000_000.0


# ---------------------------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------------------------
def test_conversion_rounds_up_to_minor_unit() -> None:
    rate = fx.parse_rate("95.5")
    assert fx.kop_to_minor(19900, rate, 2) == 209  # 2.0837... USD -> 2.09
    assert fx.kop_to_minor(9550, rate, 2) == 100  # exact
    assert fx.kop_to_minor(19900, fx.parse_rate("0.6"), 0) == 332  # JPY 331.67 -> 332
    assert fx.kop_to_minor(19900, fx.parse_rate("0.6"), 2, whole_units=True) == 33200  # ISK keeps "00"
    assert fx.minor_to_str(209, 2) == "2.09"
    assert fx.minor_to_str(332, 0) == "332"
    assert fx.str_to_minor("2.09", 2) == 209
    assert fx.parse_rate("95,5") == Decimal("95.5")
    for bad in ("0", "-1", "abc", "", None, "NaN", "Infinity"):
        with pytest.raises(ValueError):
            fx.parse_rate(bad)
    with pytest.raises(ValueError):
        fx.str_to_minor("2.091", 2)
    with pytest.raises(ValueError):
        fx.normalize_currency("US")


def test_settings_validation() -> None:
    with pytest.raises(ValueError, match="STRIPE_RUB_PER_UNIT"):
        make_settings(stripe_enabled=True, stripe_secret_key="sk", public_base_url="https://bot.example")
    with pytest.raises(ValueError, match="PAYPAL_ENABLED requires"):
        make_settings(paypal_enabled=True, paypal_rub_per_unit="90", public_base_url="https://bot.example")
    with pytest.raises(ValueError, match="PAYPAL_CURRENCY"):
        make_settings(
            paypal_enabled=True,
            paypal_client_id="id",
            paypal_client_secret="s",
            paypal_rub_per_unit="90",
            paypal_currency="dollars",
            public_base_url="https://bot.example",
        )


async def test_registry_builds_both() -> None:
    settings = make_settings(
        stripe_enabled=True,
        stripe_secret_key="sk_test_x",
        stripe_webhook_secret="whsec_x",
        stripe_currency="eur",
        stripe_rub_per_unit="100",
        paypal_enabled=True,
        paypal_client_id="cid",
        paypal_client_secret="csecret",
        paypal_webhook_id="WH-1",
        paypal_sandbox=True,
        paypal_rub_per_unit="90",
        public_base_url="https://bot.example",
    )
    gateways = build_gateways(settings, return_url="https://t.me/bot")
    try:
        assert isinstance(gateways["stripe"], StripeGateway)
        assert isinstance(gateways["paypal"], PayPalGateway)
        assert gateways["paypal"]._base_url == "https://api-m.sandbox.paypal.com"  # type: ignore[attr-defined]
        assert gateways["stripe"].min_amount_kop == 5000  # 0.50 EUR at 100 RUB
    finally:
        await close_gateways(gateways)


# ---------------------------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------------------------
WHSEC = "whsec_test_secret"


def _stripe(fake: FakeProvider, **kw: Any) -> StripeGateway:
    kw.setdefault("rub_per_unit", "95.5")
    return StripeGateway(
        "sk_test_123",
        webhook_secret=WHSEC,
        return_url="https://t.me/bot",
        base_url=fake.base_url,
        clock=lambda: NOW,
        **kw,
    )


def _session(**kw: Any) -> dict[str, Any]:
    return {
        "id": "cs_test_1",
        "object": "checkout.session",
        "url": "https://checkout.stripe.com/c/pay/cs_test_1",
        "client_reference_id": PAYMENT_ID,
        "metadata": {"payment_id": PAYMENT_ID},
        "currency": "usd",
        "amount_total": 209,
        "payment_status": "unpaid",
        "status": "open",
        **kw,
    }


async def test_stripe_create_request_shape(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("POST", "/v1/checkout/sessions", (200, _session()))
    gw = _stripe(fake)
    closing.append(gw)
    invoice = await gw.create(make_payment(19900), make_user(), "Пополнение")
    assert invoice.provider_payment_id == "cs_test_1"
    assert invoice.url == "https://checkout.stripe.com/c/pay/cs_test_1"
    assert (invoice.provider_amount, invoice.provider_currency) == ("2.09", "USD")
    sent = fake.requests[0]
    assert sent.headers["Authorization"] == "Bearer sk_test_123"
    assert sent.headers["Idempotency-Key"] == f"checkout-{PAYMENT_ID}"
    assert sent.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
    form = dict(parse_qsl(sent.body.decode()))
    assert form["mode"] == "payment"
    assert form["client_reference_id"] == PAYMENT_ID
    assert form["metadata[payment_id]"] == PAYMENT_ID
    assert form["line_items[0][price_data][currency]"] == "usd"
    assert form["line_items[0][price_data][unit_amount]"] == "209"
    assert form["line_items[0][price_data][product_data][name]"] == "Пополнение"
    assert form["line_items[0][quantity]"] == "1"
    assert form["success_url"] == form["cancel_url"] == "https://t.me/bot"


async def test_stripe_zero_decimal_currency(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("POST", "/v1/checkout/sessions", (200, _session(currency="jpy", amount_total=332)))
    gw = _stripe(fake, currency="JPY", rub_per_unit="0.6")
    closing.append(gw)
    invoice = await gw.create(make_payment(19900), make_user(), "x")
    assert (invoice.provider_amount, invoice.provider_currency) == ("332", "JPY")
    assert dict(parse_qsl(fake.requests[0].body.decode()))["line_items[0][price_data][unit_amount]"] == "332"
    with pytest.raises(ValueError):
        StripeGateway("sk", currency="KWD", rub_per_unit="250")


async def test_stripe_fetch_status_mapping(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _stripe(fake)
    closing.append(gw)
    payment = make_payment(19900, provider_payment_id="cs_test_1", provider_amount="2.09", provider_currency="USD")
    fake.reply(
        "GET",
        "/v1/checkout/sessions/cs_test_1",
        (200, _session()),
        (200, _session(status="complete", payment_status="unpaid")),
        (200, _session(status="complete", payment_status="paid")),
        (200, _session(status="complete", payment_status="paid", amount_total=208)),
        (200, _session(status="complete", payment_status="paid", currency="eur")),
        (200, _session(status="expired")),
        (200, _session(client_reference_id="someone-else")),
    )
    assert (await gw.fetch(payment)).status is PaymentStatus.PENDING
    assert (await gw.fetch(payment)).status is PaymentStatus.PENDING  # async payment in progress
    paid = await gw.fetch(payment)
    assert paid.status is PaymentStatus.SUCCEEDED
    assert paid.paid_amount_kop is None  # the wallet gets the original ruble amount
    assert paid.provider_payment_id == "cs_test_1"
    assert (await gw.fetch(payment)).status is PaymentStatus.PENDING  # underpaid: never credited
    assert (await gw.fetch(payment)).status is PaymentStatus.PENDING  # wrong currency
    assert (await gw.fetch(payment)).status is PaymentStatus.EXPIRED
    with pytest.raises(GatewayError):
        await gw.fetch(payment)
    assert fake.requests[0].headers["Authorization"] == "Bearer sk_test_123"
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment())


async def test_stripe_uses_stored_invoice_amount(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    # The rate was raised after the invoice: the stored amount is what the buyer owes.
    gw = _stripe(fake, rub_per_unit="50")
    closing.append(gw)
    fake.reply("GET", "/v1/checkout/sessions/cs_test_1", (200, _session(status="complete", payment_status="paid")))
    payment = make_payment(19900, provider_payment_id="cs_test_1", provider_amount="2.09", provider_currency="USD")
    assert (await gw.fetch(payment)).status is PaymentStatus.SUCCEEDED
    unstored = make_payment(19900, provider_payment_id="cs_test_1")
    assert (await gw.fetch(unstored)).status is PaymentStatus.PENDING  # 3.98 USD expected at the new rate


def _signed(event: dict[str, Any], *, t: int = int(NOW), secret: str = WHSEC) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(event).encode()
    sig = stripe.compute_signature(secret, t, raw)
    return raw, {"Stripe-Signature": f"t={t},v1={sig},v0=deadbeef"}


def _event(kind: str = "checkout.session.completed", **session: Any) -> dict[str, Any]:
    return {"id": "evt_1", "object": "event", "type": kind, "data": {"object": _session(**session)}}


async def test_stripe_webhook_signature(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _stripe(fake)
    closing.append(gw)
    raw, headers = _signed(_event())
    result = await gw.parse_webhook(raw, headers, "1.2.3.4")
    assert result.payment_ids == [PAYMENT_ID]
    assert result.provider_ids == ["cs_test_1"]
    assert result.trusted_status is None  # the session is always fetched again

    # Several v1 signatures (secret rotation): any match is enough.
    other = stripe.compute_signature("whsec_old", int(NOW), raw)
    rotated = {"Stripe-Signature": f"t={int(NOW)},v1={other},v1={headers['Stripe-Signature'].split('v1=')[1]}"}
    assert (await gw.parse_webhook(raw, rotated, None)).payment_ids == [PAYMENT_ID]

    for kind in ("checkout.session.async_payment_succeeded", "checkout.session.expired"):
        ok_raw, ok_headers = _signed(_event(kind))
        assert (await gw.parse_webhook(ok_raw, ok_headers, None)).provider_ids == ["cs_test_1"]

    ignored_raw, ignored_headers = _signed({"id": "evt_2", "type": "charge.succeeded", "data": {"object": {}}})
    assert (await gw.parse_webhook(ignored_raw, ignored_headers, None)).payment_ids == []

    bad = [
        (raw, {}),
        (raw, {"Stripe-Signature": "garbage"}),
        (raw, {"Stripe-Signature": f"t={int(NOW)},v0={stripe.compute_signature(WHSEC, int(NOW), raw)}"}),
        (raw + b" ", headers),  # body changed
        _signed(_event(), secret="whsec_wrong"),
        _signed(_event(), t=int(NOW) - 301),  # replayed
    ]
    for body, hdrs in bad:
        with pytest.raises(WebhookRejected):
            await gw.parse_webhook(body, hdrs, None)

    malformed = b"not json"
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(malformed, _signed_raw(malformed), None)
    assert exc.value.status_code == 400

    unsigned = StripeGateway("sk", rub_per_unit="90", base_url=fake.base_url)
    closing.append(unsigned)
    with pytest.raises(WebhookRejected):
        await unsigned.parse_webhook(raw, headers, None)


def _signed_raw(raw: bytes) -> dict[str, str]:
    return {"Stripe-Signature": f"t={int(NOW)},v1={stripe.compute_signature(WHSEC, int(NOW), raw)}"}


# ---------------------------------------------------------------------------------------------
# PayPal
# ---------------------------------------------------------------------------------------------
ORDER_ID = "5O190127TN364715T"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _paypal(fake: FakeProvider, clock: Clock | None = None, **kw: Any) -> PayPalGateway:
    kw.setdefault("rub_per_unit", "95.5")
    return PayPalGateway(
        "client-id",
        "client-secret",
        webhook_id="WH-123",
        return_url="https://t.me/bot",
        base_url=fake.base_url,
        clock=clock or Clock(),
        **kw,
    )


def _token(fake: FakeProvider, expires_in: int = 32400) -> None:
    fake.reply(
        "POST",
        "/v1/oauth2/token",
        (200, {"access_token": "A21AA-token", "token_type": "Bearer", "expires_in": expires_in}),
    )


def _order(status: str, captures: list[dict[str, Any]] | None = None, **unit: Any) -> dict[str, Any]:
    purchase_unit: dict[str, Any] = {
        "reference_id": "default",
        "custom_id": PAYMENT_ID,
        "invoice_id": PAYMENT_ID,
        "amount": {"currency_code": "USD", "value": "2.09"},
        **unit,
    }
    if captures is not None:
        purchase_unit["payments"] = {"captures": captures}
    return {"id": ORDER_ID, "status": status, "purchase_units": [purchase_unit]}


def _capture(status: str = "COMPLETED", value: str = "2.09", currency: str = "USD") -> dict[str, Any]:
    return {"id": "3C679366HH908993F", "status": status, "amount": {"currency_code": currency, "value": value}}


async def test_paypal_create_and_token_cache(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    clock = Clock()
    _token(fake, expires_in=600)
    fake.reply(
        "POST",
        "/v2/checkout/orders",
        (
            201,
            {
                "id": ORDER_ID,
                "status": "PAYER_ACTION_REQUIRED",
                "links": [
                    {"href": f"https://api-m.paypal.com/v2/checkout/orders/{ORDER_ID}", "rel": "self"},
                    {"href": f"https://www.paypal.com/checkoutnow?token={ORDER_ID}", "rel": "payer-action"},
                ],
            },
        ),
    )
    gw = _paypal(fake, clock)
    closing.append(gw)
    invoice = await gw.create(make_payment(19900), make_user(), "Пополнение")
    assert invoice.provider_payment_id == ORDER_ID
    assert invoice.url == f"https://www.paypal.com/checkoutnow?token={ORDER_ID}"
    assert (invoice.provider_amount, invoice.provider_currency) == ("2.09", "USD")

    token_req, order_req = fake.requests
    assert token_req.headers["Authorization"].startswith("Basic ")
    assert dict(parse_qsl(token_req.body.decode())) == {"grant_type": "client_credentials"}
    assert order_req.headers["Authorization"] == "Bearer A21AA-token"
    assert order_req.headers["PayPal-Request-Id"] == f"order-{PAYMENT_ID}"
    body = order_req.json()
    assert body["intent"] == "CAPTURE"
    unit = body["purchase_units"][0]
    assert unit["custom_id"] == unit["invoice_id"] == PAYMENT_ID
    assert unit["amount"] == {"currency_code": "USD", "value": "2.09"}
    context = body["payment_source"]["paypal"]["experience_context"]
    assert context["return_url"] == context["cancel_url"] == "https://t.me/bot"
    assert context["shipping_preference"] == "NO_SHIPPING"

    await gw.create(make_payment(19900), make_user(), "x")
    assert [r.path for r in fake.requests].count("/v1/oauth2/token") == 1  # cached
    clock.now += 600  # expired (minus the safety margin)
    await gw.create(make_payment(19900), make_user(), "x")
    assert [r.path for r in fake.requests].count("/v1/oauth2/token") == 2


async def test_paypal_approve_link_and_currency(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    _token(fake)
    fake.reply(
        "POST",
        "/v2/checkout/orders",
        (201, {"id": ORDER_ID, "status": "CREATED", "links": [{"href": "https://paypal.test/a", "rel": "approve"}]}),
        (201, {"id": ORDER_ID, "status": "CREATED", "links": []}),
    )
    gw = _paypal(fake, currency="JPY", rub_per_unit="0.6")
    closing.append(gw)
    invoice = await gw.create(make_payment(19900), make_user(), "x")
    assert invoice.url == "https://paypal.test/a"
    assert invoice.provider_amount == "332"  # JPY has no decimals
    with pytest.raises(GatewayError):
        await gw.create(make_payment(19900), make_user(), "x")
    with pytest.raises(ValueError):
        PayPalGateway("id", "secret", currency="KZT", rub_per_unit="0.2")


async def test_paypal_fetch_captures_approved_order(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    _token(fake)
    fake.reply("GET", f"/v2/checkout/orders/{ORDER_ID}", (200, _order("APPROVED")))
    fake.reply("POST", f"/v2/checkout/orders/{ORDER_ID}/capture", (201, _order("COMPLETED", [_capture()])))
    gw = _paypal(fake)
    closing.append(gw)
    payment = make_payment(19900, provider_payment_id=ORDER_ID, provider_amount="2.09", provider_currency="USD")
    status = await gw.fetch(payment)
    assert status.status is PaymentStatus.SUCCEEDED
    assert status.paid_amount_kop is None
    assert status.provider_payment_id == ORDER_ID
    capture = next(r for r in fake.requests if r.path.endswith("/capture"))
    assert capture.headers["PayPal-Request-Id"] == PAYMENT_ID
    assert capture.headers["Prefer"] == "return=representation"


async def test_paypal_capture_error_refetches(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    _token(fake)
    fake.reply(
        "GET",
        f"/v2/checkout/orders/{ORDER_ID}",
        (200, _order("APPROVED")),
        (200, _order("COMPLETED", [_capture()])),
    )
    fake.reply(
        "POST",
        f"/v2/checkout/orders/{ORDER_ID}/capture",
        (422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]}),
    )
    gw = _paypal(fake)
    closing.append(gw)
    status = await gw.fetch(make_payment(19900, provider_payment_id=ORDER_ID))
    assert status.status is PaymentStatus.SUCCEEDED


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        (_order("CREATED"), PaymentStatus.PENDING),
        (_order("PAYER_ACTION_REQUIRED"), PaymentStatus.PENDING),
        (_order("VOIDED"), PaymentStatus.CANCELED),
        (_order("COMPLETED", [_capture("PENDING")]), PaymentStatus.PENDING),
        (_order("COMPLETED", [_capture("DECLINED")]), PaymentStatus.CANCELED),
        (_order("COMPLETED", [_capture("REFUNDED")]), PaymentStatus.REFUNDED),
        (_order("COMPLETED", [_capture("PARTIALLY_REFUNDED")]), PaymentStatus.SUCCEEDED),
        (_order("COMPLETED", [_capture(value="2.08")]), PaymentStatus.PENDING),  # underpaid
        (_order("COMPLETED", [_capture(currency="EUR")]), PaymentStatus.PENDING),  # wrong currency
        (_order("COMPLETED", [_capture(value="2.10")]), PaymentStatus.SUCCEEDED),
    ],
)
def test_paypal_status_mapping(order: dict[str, Any], expected: PaymentStatus) -> None:
    gw = PayPalGateway("id", "secret", rub_per_unit="95.5")
    payment = make_payment(19900, provider_payment_id=ORDER_ID, provider_amount="2.09", provider_currency="USD")
    assert gw.status(payment, order).status is expected


async def test_paypal_refuses_foreign_order(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    _token(fake)
    fake.reply("GET", f"/v2/checkout/orders/{ORDER_ID}", (200, _order("APPROVED", custom_id="another")))
    gw = _paypal(fake)
    closing.append(gw)
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment(19900, provider_payment_id=ORDER_ID))
    assert not any(r.path.endswith("/capture") for r in fake.requests)


PP_HEADERS = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api-m.paypal.com/v1/notifications/certs/CERT-360caa42",
    "PAYPAL-TRANSMISSION-ID": "69cd13f0-d67a-11e5-baa3-778b53f4ae55",
    "PAYPAL-TRANSMISSION-SIG": "c2lnbmF0dXJlLWZyb20tcGF5cGFs",
    "PAYPAL-TRANSMISSION-TIME": "2016-02-18T20:01:35Z",
}


def _verify_reply(status: str) -> Any:
    def reply(rec: Recorded) -> tuple[int, Any]:
        return 200, {"verification_status": status}

    return reply


async def test_paypal_webhook_verification(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    _token(fake)
    fake.reply("POST", "/v1/notifications/verify-webhook-signature", _verify_reply("SUCCESS"))
    gw = _paypal(fake)
    closing.append(gw)

    capture_event = {
        "id": "WH-58D329510W468432D-8HN650336L201105X",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource_type": "capture",
        "resource": {
            **_capture(),
            "custom_id": PAYMENT_ID,
            "invoice_id": PAYMENT_ID,
            "supplementary_data": {"related_ids": {"order_id": ORDER_ID}},
        },
    }
    # Unusual spacing must reach PayPal untouched.
    raw = json.dumps(capture_event, indent=1, ensure_ascii=False).encode()
    result = await gw.parse_webhook(raw, PP_HEADERS, "1.2.3.4")
    assert result.payment_ids == [PAYMENT_ID]
    assert result.provider_ids == [ORDER_ID]
    assert result.trusted_status is None

    verify = next(r for r in fake.requests if r.path == "/v1/notifications/verify-webhook-signature")
    assert verify.headers["Authorization"] == "Bearer A21AA-token"
    sent = verify.json()
    assert sent["webhook_id"] == "WH-123"
    assert sent["auth_algo"] == "SHA256withRSA"
    assert sent["transmission_sig"] == PP_HEADERS["PAYPAL-TRANSMISSION-SIG"]
    assert sent["webhook_event"] == capture_event
    assert raw in verify.body  # spliced byte for byte

    order_event = {
        "id": "WH-2",
        "event_type": "CHECKOUT.ORDER.APPROVED",
        "resource_type": "checkout-order",
        "resource": _order("APPROVED"),
    }
    approved = await gw.parse_webhook(json.dumps(order_event).encode(), PP_HEADERS, None)
    assert approved.payment_ids == [PAYMENT_ID]
    assert approved.provider_ids == [ORDER_ID]


async def test_paypal_webhook_rejections(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    _token(fake)
    fake.reply(
        "POST",
        "/v1/notifications/verify-webhook-signature",
        _verify_reply("FAILURE"),
        (400, {"name": "VALIDATION_ERROR"}),
    )
    gw = _paypal(fake)
    closing.append(gw)
    raw = json.dumps({"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"custom_id": PAYMENT_ID}}).encode()

    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(raw, PP_HEADERS, None)
    assert exc.value.status_code == 401
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(raw, PP_HEADERS, None)  # PayPal API error: ask for a retry
    assert exc.value.status_code == 503

    calls = len(fake.requests)
    missing = {k: v for k, v in PP_HEADERS.items() if k != "PAYPAL-TRANSMISSION-SIG"}
    with pytest.raises(WebhookRejected):
        await gw.parse_webhook(raw, missing, None)
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(b"[1, 2]", PP_HEADERS, None)
    assert exc.value.status_code == 400
    assert len(fake.requests) == calls  # rejected before calling PayPal

    no_id = PayPalGateway("id", "secret", rub_per_unit="90", base_url=fake.base_url)
    closing.append(no_id)
    with pytest.raises(WebhookRejected):
        await no_id.parse_webhook(raw, PP_HEADERS, None)
