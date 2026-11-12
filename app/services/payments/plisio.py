"""Plisio cryptocurrency invoice gateway.

The API deliberately uses GET query parameters.  Callbacks request ``json=true``
and are verified using the provider's documented HMAC-SHA1 JSON serialization.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
from app.utils.security import const_eq

if TYPE_CHECKING:
    from app.db.models import Payment, User

BASE_URL = "https://api.plisio.net/api/v1"
SUCCESS = {"completed", "mismatch"}
CANCELED = {"cancelled", "cancelled duplicate", "expired", "error"}


def json_callback_url(url: str) -> str:
    """Ask Plisio for JSON callbacks without destroying any existing query string."""
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["json"] = "true"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def canonical(data: Mapping[str, Any]) -> bytes:
    """Equivalent to JSON.stringify(parsed JSON): preserve callback key order, compact JSON and literal Unicode."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


def signature(data: Mapping[str, Any], api_key: str) -> str:
    return hmac.new(api_key.encode(), canonical(data), hashlib.sha1).hexdigest()


def _underpaid(data: Mapping[str, Any]) -> bool:
    """True if the callback reports a received crypto amount (``actual_sum``) below the invoiced one (``amount``)."""
    try:
        received, expected = Decimal(str(data["actual_sum"])), Decimal(str(data["amount"]))
    except (KeyError, InvalidOperation):
        return False
    return received.is_finite() and expected.is_finite() and received < expected


def status(data: Mapping[str, Any]) -> ProviderStatus:
    value = str(data.get("status") or "").lower()
    state = (
        PaymentStatus.SUCCEEDED
        if value in SUCCESS
        else PaymentStatus.CANCELED
        if value in CANCELED
        else PaymentStatus.PENDING
    )
    paid = None
    if value == "mismatch" and _underpaid(data):
        # The buyer sent a different amount than invoiced; an underpayment stays open.
        state = PaymentStatus.PENDING
    if state is PaymentStatus.SUCCEEDED:
        with suppress(ValueError):
            paid = amount_to_kop(data.get("source_amount") or "")
    identifier = data.get("id") or data.get("txn_id")
    return ProviderStatus(state, paid, str(identifier) if identifier is not None else None)


class PlisioGateway(HttpGateway):
    key = PaymentProvider.PLISIO.value
    title = "💠 Криптовалюта (Plisio)"
    supports_polling = True
    min_amount_kop = 100

    def __init__(
        self,
        api_key: str,
        *,
        currency: str | None = None,
        return_url: str | None = None,
        callback_url: str | None = None,
        timeout: float = 15,
        base_url: str = BASE_URL,
    ) -> None:
        super().__init__(timeout=timeout)
        if not api_key:
            raise ValueError("Plisio needs an API key")
        self._api_key, self._currency, self._return_url = api_key, (currency or "").upper(), return_url
        self._callback_url = json_callback_url(callback_url) if callback_url else None
        self._base_url = base_url.rstrip("/")

    async def _call(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        result = await self._request_json(
            "GET", f"{self._base_url}{path}", params={**params, "api_key": self._api_key}, retries=2
        )
        if not isinstance(result, dict) or result.get("status") != "success":
            raise GatewayError(f"Plisio {path} failed: {result!r}")
        data = result.get("data", result)
        if not isinstance(data, dict):
            raise GatewayError("Plisio returned invalid response")
        return data

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        params = {
            "source_currency": "RUB",
            "source_amount": kop_to_plain(payment.amount_kop),
            "order_number": payment.id,
            "order_name": description[:255] or "VPN",
        }
        if self._currency:
            params["currency"] = self._currency
        if self._return_url:
            params["success_invoice_url"] = self._return_url
        if self._callback_url:
            params["callback_url"] = self._callback_url
        result = await self._call("/invoices/new", params)
        return Invoice(
            str(require(result, "txn_id")),
            str(require(result, "invoice_url")),
            str(result.get("source_amount") or params["source_amount"]),
            "RUB",
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if not payment.provider_payment_id:
            raise GatewayError("payment has no Plisio id")
        return status(await self._call(f"/operations/{payment.provider_payment_id}", {}))

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        data = json_body(raw)
        given = data.pop("verify_hash", None)
        if not isinstance(given, str) or not const_eq(signature(data, self._api_key), given.lower()):
            raise WebhookRejected("bad Plisio signature", 401)
        return WebhookResult(
            payment_ids=[str(data["order_number"])] if data.get("order_number") else [],
            provider_ids=[str(data["txn_id"])] if data.get("txn_id") is not None else [],
        )
