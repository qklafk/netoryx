"""cisPay: ruble payments (cards, SBP) through a hosted payment page.

API reference: https://api.cispay.app/docs (OpenAPI: https://api.cispay.app/openapi.json)

* Auth: ``X-Shop-ID`` (shop UUID) and ``X-Api-Key`` headers on every request.
* Create: ``POST /payments`` (amounts in kopecks, ``order_id`` = our payment id) -> ``id``, ``payment_url``.
* Status: ``GET /payments/status?id=`` or ``?order_id=``.
* Webhook: JSON body signed in the ``X-Signature`` header, hex HMAC-SHA256 of the raw body keyed with the shop's
  API key. The status is re-fetched before crediting; sandbox payments are never credited.
"""

from __future__ import annotations

import hashlib
import hmac
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

BASE_URL = "https://api.cispay.app"
SIGNATURE_HEADER = "X-Signature"
PAYMENT_METHODS = ("CARD", "SBP")  # empty = the payer chooses on the payment page

_STATUSES = {
    "PENDING": PaymentStatus.PENDING,
    "PAID": PaymentStatus.SUCCEEDED,
    "FAILED": PaymentStatus.CANCELED,
    "EXPIRED": PaymentStatus.EXPIRED,
    "REFUNDED": PaymentStatus.REFUNDED,
}


def sign(api_key: str, raw: bytes) -> str:
    """Hex HMAC-SHA256 of the raw webhook body."""
    return hmac.new(api_key.encode(), raw, hashlib.sha256).hexdigest()


def payment_status(data: Any, expected_order_id: str | None = None) -> ProviderStatus:
    """Map a status response to a ProviderStatus."""
    if not isinstance(data, dict):
        raise GatewayError("unexpected cisPay response")
    raw_status = str(require(data, "status")).upper()
    status = _STATUSES.get(raw_status)
    if status is None:
        raise GatewayError(f"unknown cisPay status {raw_status!r}")
    if expected_order_id is not None and str(data.get("order_id") or "") != expected_order_id:
        raise GatewayError("cisPay payment belongs to another order")
    provider_id = str(data["id"]) if data.get("id") else None
    paid = None
    if status is PaymentStatus.SUCCEEDED:
        if data.get("is_sandbox"):
            return ProviderStatus(PaymentStatus.PENDING, None, provider_id)  # test payments carry no money
        if str(data.get("currency") or "RUB").upper() != "RUB":
            raise GatewayError("cisPay payment in an unexpected currency")
        amount = data.get("amount")
        if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
            paid = amount
    return ProviderStatus(status, paid, provider_id)


class CisPayGateway(HttpGateway):
    key = PaymentProvider.CISPAY.value
    title = "💳 Карта / СБП (cisPay)"
    supports_polling = True
    polls_by_payment_id = True  # the status can be looked up by our order id

    def __init__(
        self,
        shop_id: str,
        api_key: str,
        *,
        payment_method: str | None = None,
        return_url: str | None = None,
        timeout: float = 15.0,
        base_url: str | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        if not shop_id or not api_key:
            raise ValueError("cisPay needs a shop id and an API key")
        method = (payment_method or "").upper() or None
        if method and method not in PAYMENT_METHODS:
            raise ValueError(f"unsupported cisPay payment method {payment_method!r}")
        self._headers = {"X-Shop-ID": shop_id, "X-Api-Key": api_key}
        self._api_key = api_key
        self._method = method
        self._return_url = return_url
        self._base_url = (base_url or BASE_URL).rstrip("/")
        # a card payment has a 50 RUB floor; the payer may pick a card when no method is fixed
        self.min_amount_kop = 100 if method == "SBP" else 5000

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        body: dict[str, Any] = {
            "amount": payment.amount_kop,
            "currency": "RUB",
            "order_id": payment.id,
            "customer_id": str(user.tg_id)[:40],  # required for SBP and when the payer picks the method
            "description": description[:512] or "VPN",
        }
        if self._method:
            body["payment_method"] = self._method
        if self._return_url:
            body["redirect_success_url"] = self._return_url
            body["redirect_fail_url"] = self._return_url
        data = await self._request_json("POST", f"{self._base_url}/payments", json=body, headers=self._headers)
        return Invoice(
            provider_payment_id=str(require(data, "id")),
            url=str(require(data, "payment_url")),
            provider_amount=kop_to_str(payment.amount_kop),
            provider_currency="RUB",
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        params = {"id": payment.provider_payment_id} if payment.provider_payment_id else {"order_id": payment.id}
        data = await self._request_json(
            "GET", f"{self._base_url}/payments/status", params=params, headers=self._headers, retries=1
        )
        return payment_status(data, payment.id)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        signature = get_header(headers, SIGNATURE_HEADER)
        if not signature:
            raise WebhookRejected("missing cisPay signature", 401)
        if not hmac.compare_digest(sign(self._api_key, raw).encode(), signature.strip().lower().encode()):
            raise WebhookRejected("bad cisPay signature", 401)
        data = json_body(raw)
        if data.get("event") or data.get("object") == "subscription":
            return WebhookResult()  # subscription notices are not payments
        order_id, payment_id = data.get("order_id"), data.get("id")
        if not order_id and not payment_id:
            raise WebhookRejected("cisPay webhook without a payment reference", 400)
        return WebhookResult(
            payment_ids=[str(order_id)] if order_id else [],
            provider_ids=[str(payment_id)] if payment_id else [],
        )
