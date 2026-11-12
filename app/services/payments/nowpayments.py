"""NOWPayments API gateway with authenticated IPN callbacks."""

from __future__ import annotations

import hashlib
import hmac
import json
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
    kop_to_plain,
    require,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

BASE_URL = "https://api.nowpayments.io/v1"
SUCCESS = {"finished", "confirmed", "sending"}
CANCELED = {"failed", "expired", "refunded"}


def _js_numbers(node: Any) -> Any:
    """Whole floats print without a fraction in JavaScript (1500.0 -> 1500), which is what the sender signed."""
    if isinstance(node, float) and node.is_integer():
        return int(node)
    if isinstance(node, dict):
        return {key: _js_numbers(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_js_numbers(value) for value in node]
    return node


def canonical(data: Mapping[str, Any]) -> bytes:
    """Key-sorted compact JSON with literal Unicode, like ``JSON.stringify`` over sorted keys."""
    return json.dumps(_js_numbers(data), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def signature(data: Mapping[str, Any], secret: str) -> str:
    return hmac.new(secret.encode(), canonical(data), hashlib.sha512).hexdigest()


def payment_status(data: Mapping[str, Any]) -> ProviderStatus:
    value = str(data.get("payment_status") or "")
    state = (
        PaymentStatus.SUCCEEDED
        if value in SUCCESS
        else PaymentStatus.CANCELED
        if value in CANCELED
        else PaymentStatus.PENDING
    )
    paid = None
    if state is PaymentStatus.SUCCEEDED:
        with suppress(ValueError):
            paid = amount_to_kop(data.get("price_amount") or "")
    identifier = data.get("payment_id")
    return ProviderStatus(state, paid, str(identifier) if identifier is not None else None)


class NowPaymentsGateway(HttpGateway):
    key = PaymentProvider.NOWPAYMENTS.value
    title = "💠 Криптовалюта (NOWPayments)"
    supports_polling = True
    min_amount_kop = 100

    def __init__(
        self,
        api_key: str,
        ipn_secret: str,
        *,
        pay_currency: str = "usdttrc20",
        return_url: str | None = None,
        callback_url: str | None = None,
        timeout: float = 15,
        base_url: str = BASE_URL,
    ) -> None:
        super().__init__(timeout=timeout)
        if not api_key or not ipn_secret:
            raise ValueError("NOWPayments needs API and IPN keys")
        self._api_key, self._ipn_secret, self._pay_currency = api_key, ipn_secret, pay_currency.lower()
        self._return_url, self._callback_url, self._base_url = return_url, callback_url, base_url.rstrip("/")

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        body: dict[str, Any] = {
            "price_amount": kop_to_plain(payment.amount_kop),
            "price_currency": "rub",
            "pay_currency": self._pay_currency,
            "order_id": payment.id,
            "order_description": description[:255],
        }
        if self._return_url:
            body["success_url"] = self._return_url
        if self._callback_url:
            body["ipn_callback_url"] = self._callback_url
        result = await self._request_json(
            "POST", f"{self._base_url}/payment", json=body, headers={"x-api-key": self._api_key}, retries=0
        )
        if not isinstance(result, dict):
            raise GatewayError("NOWPayments returned invalid response")
        return Invoice(
            str(require(result, "payment_id")),
            str(require(result, "pay_address")),
            str(result.get("pay_amount") or body["price_amount"]),
            str(result.get("pay_currency") or self._pay_currency).upper(),
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if not payment.provider_payment_id:
            raise GatewayError("payment has no NOWPayments id")
        result = await self._request_json(
            "GET",
            f"{self._base_url}/payment/{payment.provider_payment_id}",
            headers={"x-api-key": self._api_key},
            retries=2,
        )
        if not isinstance(result, dict):
            raise GatewayError("NOWPayments returned invalid response")
        return payment_status(result)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        data = json_body(raw)
        if not hmac.compare_digest(
            signature(data, self._ipn_secret), (get_header(headers, "x-nowpayments-sig") or "").lower()
        ):
            raise WebhookRejected("bad NOWPayments signature", 401)
        return WebhookResult(
            payment_ids=[str(data["order_id"])] if data.get("order_id") else [],
            provider_ids=[str(data["payment_id"])] if data.get("payment_id") is not None else [],
        )
