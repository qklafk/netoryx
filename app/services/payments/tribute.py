"""Tribute (tribute.tg) shop orders.

Every payment is a one-time shop order created through the Shop API (``POST /shop/orders``); the order
UUID is stored as ``provider_payment_id``. Webhooks carry the order UUID only and are signed with
HMAC-SHA256 of the raw body keyed by the API key (``trbt-signature`` header); the status is always
re-fetched from ``GET /shop/orders/{uuid}`` before crediting.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from collections.abc import Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

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
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

BASE_URL = "https://tribute.tg/api/v1"
SIGNATURE_HEADER = "trbt-signature"
TITLE_MAX = 100
DESCRIPTION_MAX = 300
CUSTOMER_ID_MAX = 256


def sign(raw: bytes, api_key: str) -> str:
    """Hex HMAC-SHA256 of the raw webhook body keyed by the API key."""
    return hmac.new(api_key.encode(), raw, hashlib.sha256).hexdigest()


def verify(raw: bytes, given: str | None, api_key: str) -> bool:
    """Check a ``trbt-signature`` value; hex (any case) and base64 encodings of the digest are accepted."""
    if not given:
        return False
    text = given.strip()
    if text.lower().startswith("sha256="):
        text = text[7:]
    expected = hmac.new(api_key.encode(), raw, hashlib.sha256).digest()
    candidates: list[bytes] = []
    with suppress(ValueError):
        candidates.append(bytes.fromhex(text))
    with suppress(binascii.Error, ValueError):
        candidates.append(base64.b64decode(text, validate=True))
    return any(len(c) == len(expected) and hmac.compare_digest(c, expected) for c in candidates)


def status(order: Mapping[str, Any]) -> ProviderStatus:
    """Map a Tribute shop order to our status. ``prepaid`` (Stars credited, charge unconfirmed) is pending."""
    value = str(order.get("status") or "").lower()
    state = (
        PaymentStatus.SUCCEEDED
        if value == "paid"
        else PaymentStatus.CANCELED
        if value == "failed"
        else PaymentStatus.PENDING
    )
    paid = None
    amount = order.get("amount")
    if (
        state is PaymentStatus.SUCCEEDED
        and str(order.get("currency") or "").lower() == "rub"
        and isinstance(amount, int)
        and not isinstance(amount, bool)
        and amount >= 0
    ):
        paid = amount
    uuid = order.get("uuid")
    return ProviderStatus(state, paid, str(uuid) if uuid else None)


class TributeGateway(HttpGateway):
    key = PaymentProvider.TRIBUTE.value
    title = "💳 Карта / СБП (Tribute)"
    supports_polling = True
    min_amount_kop = 100  # the Shop API rejects amounts below 100 minor units

    def __init__(
        self,
        api_key: str,
        *,
        shop_id: int | None = None,
        return_url: str | None = None,
        timeout: float = 15.0,
        base_url: str = BASE_URL,
    ) -> None:
        super().__init__(timeout=timeout)
        if not api_key:
            raise ValueError("Tribute needs an API key")
        self._api_key = api_key
        self._shop_id = shop_id
        # Tribute accepts only https:// redirect URLs.
        self._return_url = return_url if return_url and return_url.startswith("https://") else None
        self._base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Api-Key": self._api_key, "Accept": "application/json"}

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        text = (description or "").strip() or "VPN"
        body: dict[str, Any] = {
            "amount": payment.amount_kop,
            "currency": "rub",
            "title": text[:TITLE_MAX],
            "description": text[:DESCRIPTION_MAX],
            "period": "onetime",
            "customerId": str(user.tg_id)[:CUSTOMER_ID_MAX],
        }
        if self._shop_id:
            body["shopId"] = self._shop_id
        if self._return_url:
            body["successUrl"] = self._return_url
            body["failUrl"] = self._return_url
        if user.receipt_email:
            body["email"] = user.receipt_email
        order = await self._request_json(
            "POST", f"{self._base_url}/shop/orders", json=body, headers=self._headers(), ok_statuses=(200, 201)
        )
        if not isinstance(order, dict):
            raise GatewayError("Tribute returned an invalid order")
        uuid = order.get("uuid")
        url = order.get("paymentUrl") or order.get("webappPaymentUrl")
        if not uuid or not url:
            raise GatewayError("Tribute order has no uuid or payment URL")
        if order.get("amount") != payment.amount_kop or str(order.get("currency") or "").lower() != "rub":
            raise GatewayError("Tribute order amount or currency does not match the payment")
        return Invoice(str(uuid), str(url), kop_to_str(payment.amount_kop), "RUB")

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if not payment.provider_payment_id:
            raise GatewayError("payment has no Tribute order id")
        order_id = payment.provider_payment_id
        order = await self._request_json(
            "GET",
            f"{self._base_url}/shop/orders/{quote(order_id, safe='')}",
            headers=self._headers(),
            retries=2,
            ok_statuses=(200,),
        )
        if not isinstance(order, dict) or str(order.get("uuid") or "") != order_id:
            raise GatewayError("Tribute returned another order")
        return status(order)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if not verify(raw, get_header(headers, SIGNATURE_HEADER), self._api_key):
            raise WebhookRejected("bad Tribute signature", 401)
        data = json_body(raw)
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise WebhookRejected("Tribute webhook has no payload", 400)
        # Only shop order events refer to orders created here; others (subscriptions, donations) are acknowledged.
        name = str(data.get("name") or "")
        uuid = payload.get("uuid")
        if not name.startswith("shop_order") or not isinstance(uuid, str) or not uuid:
            return WebhookResult()
        return WebhookResult(provider_ids=[uuid])
