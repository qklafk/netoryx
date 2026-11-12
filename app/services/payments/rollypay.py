"""RollyPay: ruble payments (SBP, cards) through a hosted payment page.

* Auth: ``X-API-Key`` plus a unique ``X-Nonce`` header on every request (a nonce cannot be reused for 10 minutes).
* Create: ``POST /api/v1/payments`` with ``order_id`` = our payment id; the answer has ``payment_id``, ``pay_url``.
* Status: ``GET /api/v1/payments/{payment_id}``.
* Callbacks go to the cashier's ``callback_url`` (set in the RollyPay cabinet). ``X-Signature`` is the hex
  HMAC-SHA256 of ``<X-Timestamp>.<raw body>`` with the cashier's signing secret. The status is re-fetched
  before crediting; sandbox callbacks are acknowledged without action.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from app.db.models import PaymentProvider, PaymentStatus
from app.services.payments.base import (
    GatewayError,
    HttpGateway,
    Invoice,
    ProviderStatus,
    WebhookRejected,
    WebhookResult,
    amount_to_kop,
    get_header,
    json_body,
    kop_to_str,
    require,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

BASE_URL = "https://rollypay.io"
SIGNATURE_HEADER = "X-Signature"
TIMESTAMP_HEADER = "X-Timestamp"
TEST_MODE_HEADER = "X-Test-Mode"
PAYMENT_METHODS = ("sbp", "card")  # RUB methods; empty = the payer chooses on the payment page

_STATUSES = {
    "created": PaymentStatus.PENDING,
    "processing": PaymentStatus.PENDING,
    "paid": PaymentStatus.SUCCEEDED,
    "expired": PaymentStatus.EXPIRED,
    "canceled": PaymentStatus.CANCELED,
    "refunded": PaymentStatus.REFUNDED,
    "chargeback": PaymentStatus.REFUNDED,
}


def sign(secret: str, timestamp: str, raw: bytes) -> str:
    """Hex HMAC-SHA256 of ``timestamp + "." + body``, as RollyPay signs callbacks."""
    return hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()


def payment_status(data: Any, expected_order_id: str | None = None) -> ProviderStatus:
    """Map a payment object (status response) to a ProviderStatus."""
    if not isinstance(data, dict):
        raise GatewayError("unexpected RollyPay response")
    if isinstance(data.get("data"), dict) and "status" not in data:
        data = data["data"]
    raw_status = str(require(data, "status")).lower()
    if expected_order_id is not None and str(data.get("order_id") or "") != expected_order_id:
        raise GatewayError("RollyPay payment belongs to another order")
    status = _STATUSES.get(raw_status)
    if status is None:
        raise GatewayError(f"unknown RollyPay status {raw_status!r}")
    provider_id = str(data["payment_id"]) if data.get("payment_id") else None
    paid = None
    if status is PaymentStatus.SUCCEEDED:
        currency = str(data.get("payment_currency") or data.get("currency") or "RUB").upper()
        if currency != "RUB":
            raise GatewayError(f"RollyPay payment in unexpected currency {currency!r}")
        with suppress(ValueError):
            paid = amount_to_kop(data.get("amount", ""))
    return ProviderStatus(status, paid, provider_id)


class RollyPayGateway(HttpGateway):
    key = PaymentProvider.ROLLYPAY.value
    title = "💳 Карта / СБП (RollyPay)"
    supports_polling = True
    min_amount_kop = 1000  # the per-method minimum is not published; the provider rejects amounts below it

    def __init__(
        self,
        api_key: str,
        signing_secret: str,
        *,
        payment_method: str | None = None,
        return_url: str | None = None,
        timeout: float = 15.0,
        base_url: str | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        if not api_key or not signing_secret:
            raise ValueError("RollyPay needs an API key and a signing secret")
        if payment_method and payment_method not in PAYMENT_METHODS:
            raise ValueError(f"unsupported RollyPay payment method {payment_method!r}")
        self._api_key = api_key
        self._secret = signing_secret
        self._method = payment_method or None
        self._return_url = return_url
        self._base_url = (base_url or BASE_URL).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._api_key, "X-Nonce": uuid.uuid4().hex}

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        body: dict[str, Any] = {
            "amount": kop_to_str(payment.amount_kop),
            "payment_currency": "RUB",
            "order_id": payment.id,
            "description": description[:255] or "VPN",
        }
        if self._method:
            body["payment_method"] = self._method
        if self._return_url:
            body["success_redirect_url"] = self._return_url
            body["fail_redirect_url"] = self._return_url
        data = await self._request_json("POST", f"{self._base_url}/api/v1/payments", json=body, headers=self._headers())
        if isinstance(data, dict) and isinstance(data.get("data"), dict) and "pay_url" not in data:
            data = data["data"]
        return Invoice(
            provider_payment_id=str(require(data, "payment_id")),
            url=str(require(data, "pay_url")),
            provider_amount=kop_to_str(payment.amount_kop),
            provider_currency="RUB",
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if not payment.provider_payment_id:
            return ProviderStatus(PaymentStatus.PENDING)
        data = await self._request_json(
            "GET",
            f"{self._base_url}/api/v1/payments/{payment.provider_payment_id}",
            headers=self._headers(),
            retries=1,
        )
        return payment_status(data, payment.id)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        signature = get_header(headers, SIGNATURE_HEADER)
        timestamp = get_header(headers, TIMESTAMP_HEADER)
        if not signature or not timestamp:
            raise WebhookRejected("missing RollyPay signature", 401)
        if not hmac.compare_digest(sign(self._secret, timestamp, raw).encode(), signature.strip().lower().encode()):
            raise WebhookRejected("bad RollyPay signature", 401)
        data = json_body(raw)
        if data.get("test") is True or str(get_header(headers, TEST_MODE_HEADER) or "").lower() == "true":
            return WebhookResult()
        order_id = data.get("order_id")
        payment_id = data.get("payment_id")
        if not order_id and not payment_id:
            raise WebhookRejected("RollyPay callback without a payment reference", 400)
        # Every event type is answered with a re-fetch of the payment: only a ``paid`` status credits.
        return WebhookResult(
            payment_ids=[str(order_id)] if order_id else [],
            provider_ids=[str(payment_id)] if payment_id else [],
        )
