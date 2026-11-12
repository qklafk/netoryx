"""Platega.io gateway: SBP, cards and SberPay in RUB.

* Auth: ``X-MerchantId`` + ``X-Secret`` headers on every request.
* Create: ``POST /v2/transaction/process`` (the user picks the method) or ``POST /transaction/process``
  when a fixed ``paymentMethod`` is configured. ``paymentDetails.amount`` is a number in rubles.
* Status: ``GET /transaction/{id}`` (``PENDING`` / ``CONFIRMED`` / ``CANCELED`` / ``CHARGEBACKED``).
* Callbacks carry the same ``X-MerchantId`` / ``X-Secret`` headers (no HMAC); the body is never trusted,
  the status is re-fetched.
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
    amount_to_kop,
    get_header,
    json_body,
    kop_to_number,
    kop_to_str,
    require,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

BASE_URL = "https://app.platega.io"
# `return` and `failedUrl` are required by /transaction/process and /v2/transaction/process.
DEFAULT_RETURN_URL = "https://t.me"

_STATUSES = {
    "PENDING": PaymentStatus.PENDING,
    "CONFIRMED": PaymentStatus.SUCCEEDED,
    "CANCELED": PaymentStatus.CANCELED,
    "CANCELLED": PaymentStatus.CANCELED,
    "EXPIRED": PaymentStatus.EXPIRED,
    "CHARGEBACKED": PaymentStatus.REFUNDED,
    "REFUNDED": PaymentStatus.REFUNDED,
}


def user_email(tg_id: int) -> str:
    """Stable per-user e-mail Platega's antifraud requires (``metadata.user_email``)."""
    return f"{tg_id}@telegram.org"


class PlategaGateway(HttpGateway):
    key = PaymentProvider.PLATEGA.value
    title = "💳 Карта / СБП (Platega)"
    supports_polling = True
    min_amount_kop = 100

    def __init__(
        self,
        merchant_id: str,
        secret: str,
        *,
        payment_method: int | None = None,
        return_url: str | None = None,
        timeout: float = 15.0,
        base_url: str = BASE_URL,
    ) -> None:
        super().__init__(timeout=timeout)
        self._merchant_id = merchant_id
        self._secret = secret
        self._payment_method = payment_method
        self._return_url = return_url or DEFAULT_RETURN_URL
        self._base_url = base_url.rstrip("/")

    @property
    def _auth(self) -> dict[str, str]:
        return {"X-MerchantId": self._merchant_id, "X-Secret": self._secret}

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        body: dict[str, Any] = {
            "paymentDetails": {"amount": kop_to_number(payment.amount_kop), "currency": "RUB"},
            "description": description[:255],
            "payload": payment.id,
            "metadata": {"user_email": user_email(user.tg_id), "userId": str(user.tg_id)},
            "return": self._return_url,
            "failedUrl": self._return_url,
        }
        if self._payment_method is None:
            path = "/v2/transaction/process"
        else:
            path = "/transaction/process"
            body["paymentMethod"] = self._payment_method
        # Not idempotent at the provider: never retried.
        data = await self._request_json("POST", self._base_url + path, json=body, headers=self._auth)
        if not isinstance(data, dict):
            raise GatewayError("unexpected Platega response")
        url = data.get("url") or data.get("redirect")  # v2 / v1
        if not url:
            raise GatewayError("Platega response has no payment URL")
        return Invoice(
            provider_payment_id=str(require(data, "transactionId")),
            url=str(url),
            provider_amount=kop_to_str(payment.amount_kop),
            provider_currency="RUB",
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if not payment.provider_payment_id:
            raise GatewayError("payment has no Platega transaction id")
        data = await self._request_json(
            "GET", f"{self._base_url}/transaction/{payment.provider_payment_id}", headers=self._auth, retries=2
        )
        return transaction_status(data, payment.provider_payment_id)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if not (
            _same(get_header(headers, "X-MerchantId"), self._merchant_id)
            and _same(get_header(headers, "X-Secret"), self._secret)
        ):
            raise WebhookRejected("bad Platega credentials", 401)
        data = json_body(raw)
        our_id = data.get("payload")
        provider_id = data.get("id")
        return WebhookResult(
            payment_ids=[our_id] if isinstance(our_id, str) and our_id else [],
            provider_ids=[str(provider_id)] if provider_id else [],
        )


def _same(given: str | None, expected: str) -> bool:
    # Header values may carry lone surrogates (undecodable bytes); a strict encode would raise.
    return given is not None and hmac.compare_digest(given.strip().encode("utf-8", "replace"), expected.encode("utf-8"))


def transaction_status(data: Any, provider_id: str) -> ProviderStatus:
    """Map ``GET /transaction/{id}`` to a ProviderStatus; RUB amount only when the currency is RUB."""
    if not isinstance(data, dict):
        raise GatewayError("unexpected Platega response")
    raw_status = str(require(data, "status")).upper()
    status = _STATUSES.get(raw_status)
    if status is None:
        raise GatewayError(f"unknown Platega status {raw_status!r}")
    paid: int | None = None
    details = data.get("paymentDetails")
    if status is PaymentStatus.SUCCEEDED and isinstance(details, dict) and details.get("currency", "RUB") == "RUB":
        try:
            paid = amount_to_kop(details.get("amount", ""))
        except ValueError:
            paid = None
    return ProviderStatus(status, paid, provider_id)
