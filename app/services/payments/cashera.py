"""Cashera: ruble payments (SBP, cards) settled in USD, through a hosted payment page.

API reference: https://docs.cashera.cash (payments/create, payments/status, webhooks)

* Auth: ``X-Api-Key`` header (``pk_...``). Amounts are integer kopecks.
* Create: ``POST /api/v1/integration/transactions`` with ``external_id`` = our payment id -> ``uuid``,
  ``payment_url``.
* Status: ``GET /api/v1/integration/transactions/{uuid}`` or ``.../by-external-id/{external_id}``.
* Webhook ``transaction.status_updated``: authenticated by the ``X-Api-Key`` and ``X-Secret`` (``sk_...``) headers,
  which must equal the stored credentials. The status is re-fetched before crediting (a late ``paid`` after a
  failed attempt is accepted by the same path).
"""

from __future__ import annotations

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

BASE_URL = "https://api.cashera.cash/api/v1/integration"
PAYMENT_METHODS = ("sbp", "card")  # empty = the payer chooses on the payment page

_STATUSES = {
    "pending": PaymentStatus.PENDING,
    "paid": PaymentStatus.SUCCEEDED,
    "canceled": PaymentStatus.CANCELED,
    "failed": PaymentStatus.CANCELED,
    "expired": PaymentStatus.EXPIRED,
    "refunded": PaymentStatus.REFUNDED,
    "chargeback": PaymentStatus.REFUNDED,
}


def payment_status(data: Any, expected_external_id: str | None = None) -> ProviderStatus:
    """Map a transaction object to a ProviderStatus."""
    if not isinstance(data, dict):
        raise GatewayError("unexpected Cashera response")
    raw_status = str(require(data, "status")).lower()
    status = _STATUSES.get(raw_status)
    if status is None:
        raise GatewayError(f"unknown Cashera status {raw_status!r}")
    if expected_external_id is not None and str(data.get("external_id") or "") != expected_external_id:
        raise GatewayError("Cashera transaction belongs to another order")
    if data.get("type") not in (None, "deposit"):
        raise GatewayError("Cashera transaction is not a deposit")
    provider_id = str(data["uuid"]) if data.get("uuid") else None
    paid = None
    if status is PaymentStatus.SUCCEEDED:
        if str(data.get("currency") or "").upper() != "RUB":
            raise GatewayError("Cashera payment in an unexpected currency")
        amount = data.get("amount")
        if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
            paid = amount
    return ProviderStatus(status, paid, provider_id)


class CasheraGateway(HttpGateway):
    key = PaymentProvider.CASHERA.value
    title = "💳 Карта / СБП (Cashera)"
    supports_polling = True
    polls_by_payment_id = True  # transactions can be looked up by external_id

    def __init__(
        self,
        api_key: str,
        secret: str,
        *,
        payment_method: str | None = None,
        callback_url: str | None = None,
        return_url: str | None = None,
        timeout: float = 15.0,
        base_url: str | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        if not api_key or not secret:
            raise ValueError("Cashera needs an API key and a secret")
        method = (payment_method or "").lower() or None
        if method and method not in PAYMENT_METHODS:
            raise ValueError(f"unsupported Cashera payment method {payment_method!r}")
        self._api_key = api_key
        self._secret = secret
        self._headers = {"X-Api-Key": api_key}
        self._method = method
        self._callback_url = callback_url
        self._return_url = return_url
        self._base_url = (base_url or BASE_URL).rstrip("/")
        # card payments start at 100 RUB; other methods at 1 kopeck
        self.min_amount_kop = 100 if method == "sbp" else 10000

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        body: dict[str, Any] = {
            "amount": payment.amount_kop,
            "currency": "RUB",
            "external_id": payment.id,
            "description": description[:255] or "VPN",
        }
        if self._method:
            body["payment_method"] = self._method
        if self._callback_url:
            body["callback_url"] = self._callback_url
        if self._return_url:
            body["success_url"] = self._return_url
            body["fail_url"] = self._return_url
        data = await self._request_json("POST", f"{self._base_url}/transactions", json=body, headers=self._headers)
        url = str(require(data, "payment_url"))
        if "://" not in url:
            url = "https://" + url  # the API returns the host and path without a scheme
        return Invoice(
            provider_payment_id=str(require(data, "uuid")),
            url=url,
            provider_amount=kop_to_str(payment.amount_kop),
            provider_currency="RUB",
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if payment.provider_payment_id:
            url = f"{self._base_url}/transactions/{payment.provider_payment_id}"
        else:
            url = f"{self._base_url}/transactions/by-external-id/{payment.id}"
        data = await self._request_json("GET", url, headers=self._headers, retries=1)
        return payment_status(data, payment.id)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        key = get_header(headers, "X-Api-Key") or ""
        secret = get_header(headers, "X-Secret") or ""
        ok_key = hmac.compare_digest(key.encode(), self._api_key.encode())
        ok_secret = hmac.compare_digest(secret.encode(), self._secret.encode())
        if not (ok_key and ok_secret):
            raise WebhookRejected("bad Cashera credentials", 401)
        data = json_body(raw)
        if data.get("event") != "transaction.status_updated":
            return WebhookResult()  # payout and subscription events are not ours
        tx = data.get("transaction")
        if not isinstance(tx, dict):
            raise WebhookRejected("Cashera webhook without a transaction", 400)
        external_id, uuid = tx.get("external_id"), tx.get("uuid")
        if not external_id and not uuid:
            raise WebhookRejected("Cashera webhook without a payment reference", 400)
        return WebhookResult(
            payment_ids=[str(external_id)] if external_id else [],
            provider_ids=[str(uuid)] if uuid else [],
        )
