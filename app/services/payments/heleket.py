"""Heleket cryptocurrency invoice gateway."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
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
    json_body,
    kop_to_plain,
    require,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

BASE_URL = "https://api.heleket.com/v1"
_SUCCESS = {"paid", "paid_over"}
_CANCELED = {"cancel", "fail", "wrong_amount", "system_fail", "locked", "refund_paid", "refund_fail"}


def canonical_json(data: Mapping[str, Any]) -> bytes:
    """PHP-compatible JSON: no spaces, Unicode literal, and escaped slashes."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("/", "\\/").encode()


def sign(data: Mapping[str, Any], api_key: str) -> str:
    return hashlib.md5(base64.b64encode(canonical_json(data)) + api_key.encode()).hexdigest()


def status(data: Mapping[str, Any]) -> ProviderStatus:
    value = str(data.get("payment_status") or data.get("status") or "")
    result = (
        PaymentStatus.SUCCEEDED
        if value in _SUCCESS
        else PaymentStatus.CANCELED
        if value in _CANCELED
        else PaymentStatus.PENDING
    )
    paid = None
    if result is PaymentStatus.SUCCEEDED:
        with contextlib.suppress(ValueError):
            paid = amount_to_kop(data.get("amount") or data.get("payment_amount") or "")
    value_id = data.get("uuid")
    return ProviderStatus(result, paid, str(value_id) if value_id else None)


class HeleketGateway(HttpGateway):
    key = PaymentProvider.HELEKET.value
    title = "💠 Криптовалюта (Heleket)"
    supports_polling = True
    min_amount_kop = 100

    def __init__(
        self,
        merchant_id: str,
        api_key: str,
        *,
        currency: str = "RUB",
        to_currency: str | None = None,
        return_url: str | None = None,
        callback_url: str | None = None,
        timeout: float = 15,
        base_url: str = BASE_URL,
    ) -> None:
        super().__init__(timeout=timeout)
        if not merchant_id or not api_key:
            raise ValueError("Heleket needs merchant ID and API key")
        self._merchant_id, self._api_key, self._currency = merchant_id, api_key, currency.upper()
        self._to_currency, self._return_url, self._callback_url, self._base_url = (
            to_currency,
            return_url,
            callback_url,
            base_url.rstrip("/"),
        )

    async def _call(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = canonical_json(payload)
        data = await self._request_json(
            "POST",
            f"{self._base_url}{path}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "merchant": self._merchant_id,
                "sign": hashlib.md5(base64.b64encode(body) + self._api_key.encode()).hexdigest(),
            },
            retries=2,
        )
        if not isinstance(data, dict) or data.get("state") not in (0, None):
            raise GatewayError(f"Heleket {path} failed: {data!r}")
        result = data.get("result", data)
        if not isinstance(result, dict):
            raise GatewayError("Heleket returned invalid response")
        return result

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        payload: dict[str, Any] = {
            "amount": kop_to_plain(payment.amount_kop),
            "currency": self._currency,
            "order_id": payment.id,
            "additional_data": description[:255],
            "is_payment_multiple": False,
        }
        if self._to_currency:
            payload["to_currency"] = self._to_currency
        if self._return_url:
            payload["url_return"] = self._return_url
        if self._callback_url:
            payload["url_callback"] = self._callback_url
        result = await self._call("/payment", payload)
        return Invoice(
            str(require(result, "uuid")),
            str(require(result, "url")),
            str(result.get("amount") or payload["amount"]),
            self._currency,
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        result = await self._call("/payment/info", {"order_id": payment.id})
        return status(result)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        data = json_body(raw)
        given = data.pop("sign", None)
        if not isinstance(given, str) or not hmac.compare_digest(sign(data, self._api_key), given):
            raise WebhookRejected("bad Heleket signature", 401)
        return WebhookResult(
            payment_ids=[str(data["order_id"])] if data.get("order_id") else [],
            provider_ids=[str(data["uuid"])] if data.get("uuid") else [],
        )
