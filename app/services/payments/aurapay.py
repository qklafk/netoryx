"""Aurapay: ruble payments (cards, SBP) through a hosted payment page.

API reference: https://docs.aurapay.tech (OpenAPI: https://docs.aurapay.tech/openapi.yaml)

* Auth: ``X-ApiKey`` and ``X-ShopId`` headers.
* Create: ``POST /invoice/create`` with ``order_id`` = our payment id -> ``id``, ``payment_data.url``.
* Status: ``POST /invoice/status`` with ``order_id`` (or ``id``).
* Webhook: ``X-SIGNATURE`` is the HMAC-SHA256, keyed with the shop's secret key no. 2, of the body values sorted by
  key and concatenated (see :mod:`app.services.payments.concat_signature`). The status is re-fetched before
  crediting.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.db.models import PaymentProvider, PaymentStatus
from app.services.payments import concat_signature
from app.services.payments.base import (
    GatewayError,
    HttpGateway,
    Invoice,
    ProviderStatus,
    WebhookRejected,
    WebhookResult,
    amount_to_kop,
    get_header,
    kop_to_number,
    kop_to_str,
    require,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

BASE_URL = "https://app.aurapay.tech"
SERVICES = ("sbp", "card")  # empty = the payer chooses on the payment page

_STATUSES = {
    "PENDING": PaymentStatus.PENDING,
    "PAID": PaymentStatus.SUCCEEDED,
    "EXPIRED": PaymentStatus.EXPIRED,
    "REFUNDED": PaymentStatus.REFUNDED,
}


def payment_status(data: Any, expected_order_id: str | None = None) -> ProviderStatus:
    """Map an invoice object to a ProviderStatus."""
    if not isinstance(data, dict):
        raise GatewayError("unexpected Aurapay response")
    raw_status = str(require(data, "status")).upper()
    status = _STATUSES.get(raw_status)
    if status is None:
        raise GatewayError(f"unknown Aurapay status {raw_status!r}")
    if expected_order_id is not None and str(data.get("order_id") or "") != expected_order_id:
        raise GatewayError("Aurapay invoice belongs to another order")
    provider_id = str(data["id"]) if data.get("id") else None
    paid = None
    if status is PaymentStatus.SUCCEEDED:
        try:
            paid = amount_to_kop(Decimal(str(data.get("amount"))))
        except (ValueError, ArithmeticError):
            paid = None
    return ProviderStatus(status, paid, provider_id)


class AuraPayGateway(HttpGateway):
    key = PaymentProvider.AURAPAY.value
    title = "💳 Карта / СБП (Aurapay)"
    supports_polling = True
    polls_by_payment_id = True  # invoices can be looked up by order_id
    min_amount_kop = 100

    def __init__(
        self,
        shop_id: str,
        api_key: str,
        webhook_key: str,
        *,
        service: str | None = None,
        callback_url: str | None = None,
        return_url: str | None = None,
        timeout: float = 15.0,
        base_url: str | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        if not shop_id or not api_key or not webhook_key:
            raise ValueError("Aurapay needs a shop id, an API key and the second secret key")
        service = (service or "").lower() or None
        if service and service not in SERVICES:
            raise ValueError(f"unsupported Aurapay service {service!r}")
        self._headers = {"X-ApiKey": api_key, "X-ShopId": shop_id}
        self._webhook_key = webhook_key
        self._service = service
        self._callback_url = callback_url
        self._return_url = return_url
        self._base_url = (base_url or BASE_URL).rstrip("/")

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        body: dict[str, Any] = {
            "order_id": payment.id,
            "amount": kop_to_number(payment.amount_kop),
            "comment": description[:255] or "VPN",
        }
        if self._service:
            body["service"] = self._service
        if self._callback_url:
            body["callback_url"] = self._callback_url
        if self._return_url:
            body["success_url"] = self._return_url
            body["fail_url"] = self._return_url
        data = await self._request_json("POST", f"{self._base_url}/invoice/create", json=body, headers=self._headers)
        return Invoice(
            provider_payment_id=str(require(data, "id")),
            url=str(require(data, "payment_data", "url")),
            provider_amount=kop_to_str(payment.amount_kop),
            provider_currency="RUB",
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        data = await self._request_json(
            "POST",
            f"{self._base_url}/invoice/status",
            json={"order_id": payment.id},
            headers=self._headers,
            retries=1,
        )
        return payment_status(data, payment.id)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        signature = get_header(headers, concat_signature.SIGNATURE_HEADER)
        if not signature:
            raise WebhookRejected("missing Aurapay signature", 401)
        try:
            body = concat_signature.parse_body(raw)
        except ValueError as exc:
            raise WebhookRejected("malformed Aurapay webhook", 400) from exc
        if not concat_signature.verify(self._webhook_key, body, signature):
            raise WebhookRejected("bad Aurapay signature", 401)
        if "event" in body:
            return WebhookResult()  # subscription notices are not invoices
        order_id, invoice_id = body.get("order_id"), body.get("id")
        if not order_id and not invoice_id:
            raise WebhookRejected("Aurapay webhook without an invoice reference", 400)
        return WebhookResult(
            payment_ids=[str(order_id)] if order_id else [],
            provider_ids=[str(invoice_id)] if invoice_id else [],
        )
