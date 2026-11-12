"""Stripe Checkout gateway (hosted payment page, one-time payments).

Amounts are converted from rubles into the configured charge currency at a fixed rate. A webhook is
only a hint: its ``Stripe-Signature`` is verified and then the Checkout Session is fetched again.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.db.models import PaymentProvider, PaymentStatus
from app.services.payments import fx
from app.services.payments.base import (
    GatewayError,
    HttpGateway,
    Invoice,
    ProviderStatus,
    WebhookRejected,
    WebhookResult,
    get_header,
    json_body,
    require,
)
from app.utils.security import const_eq

if TYPE_CHECKING:
    from app.db.models import Payment, User

log = logging.getLogger(__name__)

BASE_URL = "https://api.stripe.com"
SIGNATURE_TOLERANCE_SEC = 300

# Currencies whose API amount has no minor unit.
ZERO_DECIMAL = frozenset(
    {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "VND", "VUV", "XAF", "XOF", "XPF"}
)
# Represented with two decimals on the wire, but only whole units can be charged.
WHOLE_UNITS = frozenset({"ISK", "UGX"})
THREE_DECIMAL = frozenset({"BHD", "JOD", "KWD", "OMR", "TND"})
# Minimum charge per currency (in units), from Stripe's published limits.
MIN_CHARGE = {
    "USD": Decimal("0.50"),
    "EUR": Decimal("0.50"),
    "GBP": Decimal("0.30"),
    "AUD": Decimal("0.50"),
    "CAD": Decimal("0.50"),
    "CHF": Decimal("0.50"),
    "AED": Decimal("2.00"),
    "CZK": Decimal("15.00"),
    "DKK": Decimal("2.50"),
    "HKD": Decimal("4.00"),
    "HUF": Decimal("175.00"),
    "JPY": Decimal("50"),
    "KRW": Decimal("50"),
    "MXN": Decimal("10"),
    "NOK": Decimal("3.00"),
    "PLN": Decimal("2.00"),
    "RON": Decimal("2.00"),
    "SEK": Decimal("3.00"),
    "SGD": Decimal("0.50"),
    "THB": Decimal("10"),
    "RUB": Decimal("0.50"),
}

HANDLED_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
        "checkout.session.expired",
    }
)


def exponent(currency: str) -> int:
    return 0 if currency in ZERO_DECIMAL else 2


def parse_signature_header(header: str) -> tuple[int, list[str]]:
    """``t=...,v1=...,v1=...`` -> (timestamp, v1 signatures). Raises ValueError if malformed."""
    timestamp: int | None = None
    signatures: list[str] = []
    for item in header.split(","):
        key, sep, value = item.strip().partition("=")
        if not sep:
            continue
        if key == "t":
            timestamp = int(value)
        elif key == "v1":
            signatures.append(value.strip())
    if timestamp is None or not signatures:
        raise ValueError("no timestamp or v1 signature")
    return timestamp, signatures


def compute_signature(secret: str, timestamp: int, raw: bytes) -> str:
    """HMAC-SHA256 of ``"{t}.{body}"`` keyed with the endpoint secret, hex encoded."""
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()


def verify_signature(
    raw: bytes, header: str | None, secret: str, *, now: float, tolerance: int = SIGNATURE_TOLERANCE_SEC
) -> None:
    """Raise WebhookRejected unless one of the ``v1`` signatures matches and the timestamp is fresh."""
    if not header:
        raise WebhookRejected("missing Stripe-Signature", 400)
    try:
        timestamp, signatures = parse_signature_header(header)
    except ValueError as exc:
        raise WebhookRejected("malformed Stripe-Signature", 400) from exc
    expected = compute_signature(secret, timestamp, raw)
    if not any(const_eq(expected, given) for given in signatures):
        raise WebhookRejected("bad Stripe signature", 401)
    if timestamp < now - tolerance:
        raise WebhookRejected("stale Stripe signature", 401)


class StripeGateway(HttpGateway):
    key = PaymentProvider.STRIPE.value
    title = "💳 Банковская карта (Stripe)"
    supports_polling = True

    def __init__(
        self,
        secret_key: str,
        *,
        webhook_secret: str | None = None,
        currency: str = "USD",
        rub_per_unit: str | Decimal | None,
        return_url: str | None = None,
        timeout: float = 15,
        base_url: str = BASE_URL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(timeout=timeout)
        if not secret_key:
            raise ValueError("Stripe needs a secret key")
        self._currency = fx.normalize_currency(currency)
        if self._currency in THREE_DECIMAL:
            raise ValueError(f"Stripe currency {self._currency} is not supported")
        self._rate = fx.parse_rate(rub_per_unit)
        self._exp = exponent(self._currency)
        self._whole = self._currency in WHOLE_UNITS
        self._secret_key, self._webhook_secret = secret_key, webhook_secret or ""
        self._return_url, self._base_url, self._clock = return_url, base_url.rstrip("/"), clock
        self.min_amount_kop = max(100, fx.min_amount_kop(MIN_CHARGE.get(self._currency, Decimal(0)), self._rate))

    def to_minor(self, amount_kop: int) -> int:
        """Charge amount in the currency's API units (rounded up)."""
        return fx.kop_to_minor(amount_kop, self._rate, self._exp, whole_units=self._whole)

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._secret_key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        minor = self.to_minor(payment.amount_kop)
        form: list[tuple[str, str]] = [
            ("mode", "payment"),
            ("client_reference_id", payment.id),
            ("metadata[payment_id]", payment.id),
            ("payment_intent_data[metadata][payment_id]", payment.id),
            ("line_items[0][quantity]", "1"),
            ("line_items[0][price_data][currency]", self._currency.lower()),
            ("line_items[0][price_data][unit_amount]", str(minor)),
            ("line_items[0][price_data][product_data][name]", (description or "VPN")[:250]),
        ]
        if self._return_url:
            form += [("success_url", self._return_url), ("cancel_url", self._return_url)]
        data = await self._request_json(
            "POST",
            f"{self._base_url}/v1/checkout/sessions",
            data=form,
            headers=self._headers(idempotency_key=f"checkout-{payment.id}"),
            retries=2,  # safe: the Idempotency-Key makes retries return the same session
        )
        session_id, url = require(data, "id"), require(data, "url")
        if not isinstance(url, str) or not url:
            raise GatewayError("Stripe returned no checkout URL")
        return Invoice(str(session_id), url, fx.minor_to_str(minor, self._exp), self._currency)

    def _expected_minor(self, payment: Payment) -> int:
        """The invoiced charge: what was stored at creation, recomputed only when it is missing."""
        if payment.provider_amount and (payment.provider_currency or "").upper() == self._currency:
            try:
                return fx.str_to_minor(payment.provider_amount, self._exp)
            except ValueError:
                pass
        return self.to_minor(payment.amount_kop)

    def status(self, payment: Payment, session: Mapping[str, Any]) -> ProviderStatus:
        """Map a Checkout Session to our status; a paid amount below the invoice is never credited."""
        reference = session.get("client_reference_id")
        if reference is not None and str(reference) != payment.id:
            raise GatewayError("Stripe session belongs to another payment")
        session_id = str(session["id"]) if session.get("id") else None
        state = str(session.get("status") or "")
        if session.get("payment_status") == "paid":
            currency = str(session.get("currency") or "").upper()
            total = session.get("amount_total")
            expected = self._expected_minor(payment)
            if currency != self._currency or not isinstance(total, int) or isinstance(total, bool) or total < expected:
                log.warning(
                    "payment %s: Stripe paid %s %s, expected %s %s; not credited",
                    payment.id,
                    total,
                    currency,
                    expected,
                    self._currency,
                )
                return ProviderStatus(PaymentStatus.PENDING, provider_payment_id=session_id)
            return ProviderStatus(PaymentStatus.SUCCEEDED, provider_payment_id=session_id)
        if state == "expired":
            return ProviderStatus(PaymentStatus.EXPIRED, provider_payment_id=session_id)
        return ProviderStatus(PaymentStatus.PENDING, provider_payment_id=session_id)

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if not payment.provider_payment_id:
            raise GatewayError("payment has no Stripe session id")
        data = await self._request_json(
            "GET",
            f"{self._base_url}/v1/checkout/sessions/{payment.provider_payment_id}",
            headers=self._headers(),
            retries=2,
        )
        if not isinstance(data, dict):
            raise GatewayError("Stripe returned invalid response")
        return self.status(payment, data)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if not self._webhook_secret:
            raise WebhookRejected("Stripe webhook secret is not configured", 403)
        verify_signature(raw, get_header(headers, "Stripe-Signature"), self._webhook_secret, now=self._clock())
        event = json_body(raw)
        if event.get("type") not in HANDLED_EVENTS:
            return WebhookResult()
        obj = (event.get("data") or {}).get("object") if isinstance(event.get("data"), dict) else None
        if not isinstance(obj, dict) or obj.get("object") != "checkout.session":
            raise WebhookRejected("Stripe event carries no checkout session", 400)
        payment_ids: list[str] = []
        metadata = obj.get("metadata")
        for value in (
            obj.get("client_reference_id"),
            metadata.get("payment_id") if isinstance(metadata, dict) else None,
        ):
            if isinstance(value, str) and value and value not in payment_ids:
                payment_ids.append(value)
        return WebhookResult(
            payment_ids=payment_ids,
            provider_ids=[str(obj["id"])] if obj.get("id") else [],
        )
