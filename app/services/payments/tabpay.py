"""TabPay: ruble payments (SBP, cards) through a hosted payment page.

API reference: https://tabpay.org/docs (OpenAPI: https://tabpay.org/openapi.json)

* Auth: ``X-Api-Key`` header (the key starts with ``tp_``). Amounts are integer kopecks.
* Create: ``POST /api/v1/payments`` with ``orderId`` = our payment id -> ``id``, ``payUrl``.
* Status: ``GET /api/v1/payments/{id}`` or ``GET /api/v1/payments?orderId=``.
* Webhook: ``X-Signature-V2`` is the hex HMAC-SHA256 of ``<X-Timestamp>.<raw body>`` (the timestamp must be fresh);
  the legacy ``X-Signature`` signs the raw body only. The key is the shop's webhook signing secret. The status is
  re-fetched before crediting; test payments are never credited.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from app.db.models import PaymentProvider, PaymentStatus
from app.services.payments.base import (
    GatewayError,
    HttpGateway,
    Invoice,
    ProviderStatus,
    WebhookRejected,
    WebhookResult,
    get_header,
    json_body,
    kop_to_str,
    require,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

BASE_URL = "https://tabpay.org/api"
TOLERANCE_SECONDS = 300
PAYMENT_METHODS = ("SBP", "CARD")  # empty = the payer chooses on the payment page

_STATUSES = {
    "CREATED": PaymentStatus.PENDING,
    "PENDING": PaymentStatus.PENDING,
    "SUCCESS": PaymentStatus.SUCCEEDED,
    "FAILED": PaymentStatus.CANCELED,
    "CANCELED": PaymentStatus.CANCELED,
    "EXPIRED": PaymentStatus.EXPIRED,
    "REFUNDED": PaymentStatus.REFUNDED,
}


def _mac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign_v2(secret: str, timestamp: str, raw: bytes) -> str:
    """Hex HMAC-SHA256 of ``timestamp + "." + body``."""
    return _mac(secret, timestamp.encode() + b"." + raw)


def sign_v1(secret: str, raw: bytes) -> str:
    """Hex HMAC-SHA256 of the raw body (legacy header)."""
    return _mac(secret, raw)


def payment_status(data: Any, expected_order_id: str | None = None) -> ProviderStatus:
    """Map a payment object to a ProviderStatus."""
    if not isinstance(data, dict):
        raise GatewayError("unexpected TabPay response")
    raw_status = str(require(data, "status")).upper()
    status = _STATUSES.get(raw_status)
    if status is None:
        raise GatewayError(f"unknown TabPay status {raw_status!r}")
    if expected_order_id is not None and str(data.get("orderId") or "") != expected_order_id:
        raise GatewayError("TabPay payment belongs to another order")
    provider_id = str(data["id"]) if data.get("id") else None
    paid = None
    if status is PaymentStatus.SUCCEEDED:
        if data.get("isTest") is True:
            return ProviderStatus(PaymentStatus.PENDING, None, provider_id)  # sandbox shops move no money
        amount = data.get("amountKopecks")
        if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
            paid = amount
    return ProviderStatus(status, paid, provider_id)


class TabPayGateway(HttpGateway):
    key = PaymentProvider.TABPAY.value
    title = "💳 Карта / СБП (TabPay)"
    supports_polling = True
    polls_by_payment_id = True  # payments can be looked up by orderId
    min_amount_kop = 100

    def __init__(
        self,
        api_key: str,
        webhook_secret: str,
        *,
        payment_method: str | None = None,
        return_url: str | None = None,
        timeout: float = 15.0,
        base_url: str | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        if not api_key or not webhook_secret:
            raise ValueError("TabPay needs an API key and a webhook signing secret")
        method = (payment_method or "").upper() or None
        if method and method not in PAYMENT_METHODS:
            raise ValueError(f"unsupported TabPay payment method {payment_method!r}")
        self._headers = {"X-Api-Key": api_key}
        self._secret = webhook_secret
        self._method = method
        self._return_url = return_url
        self._base_url = (base_url or BASE_URL).rstrip("/")

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        body: dict[str, Any] = {
            "orderId": payment.id,
            "amountKopecks": payment.amount_kop,
            "description": description[:255] or "VPN",
            "telegramId": str(user.tg_id),
        }
        if self._method:
            body["method"] = self._method
        if self._return_url:
            body["successUrl"] = self._return_url
            body["failUrl"] = self._return_url
        data = await self._request_json("POST", f"{self._base_url}/v1/payments", json=body, headers=self._headers)
        return Invoice(
            provider_payment_id=str(require(data, "id")),
            url=str(require(data, "payUrl")),
            provider_amount=kop_to_str(payment.amount_kop),
            provider_currency="RUB",
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if payment.provider_payment_id:
            url, params = f"{self._base_url}/v1/payments/{payment.provider_payment_id}", None
        else:
            url, params = f"{self._base_url}/v1/payments", {"orderId": payment.id}
        data = await self._request_json("GET", url, params=params, headers=self._headers, retries=1)
        return payment_status(data, payment.id)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        v2 = get_header(headers, "X-Signature-V2")
        timestamp = get_header(headers, "X-Timestamp")
        v1 = get_header(headers, "X-Signature")
        if v2 and timestamp:
            if not hmac.compare_digest(sign_v2(self._secret, timestamp, raw).encode(), v2.strip().lower().encode()):
                raise WebhookRejected("bad TabPay signature", 401)
            try:
                fresh = abs(time.time() - int(timestamp)) <= TOLERANCE_SECONDS
            except ValueError:
                fresh = False
            if not fresh:
                raise WebhookRejected("stale TabPay webhook", 401)
        elif v1:
            if not hmac.compare_digest(sign_v1(self._secret, raw).encode(), v1.strip().lower().encode()):
                raise WebhookRejected("bad TabPay signature", 401)
        else:
            raise WebhookRejected("missing TabPay signature", 401)
        data = json_body(raw)
        if data.get("test") is True:
            return WebhookResult()
        order_id, payment_id = data.get("orderId"), data.get("id")
        if not order_id and not payment_id:
            raise WebhookRejected("TabPay webhook without a payment reference", 400)
        return WebhookResult(
            payment_ids=[str(order_id)] if order_id else [],
            provider_ids=[str(payment_id)] if payment_id else [],
        )
