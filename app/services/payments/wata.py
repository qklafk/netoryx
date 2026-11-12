"""WATA internet acquiring: cards and SBP through a hosted payment link.

* Auth: ``Authorization: Bearer <terminal access token>``.
* Create: ``POST /links`` with ``orderId`` = our payment id; the answer carries the link ``id`` and ``url``.
* Status: ``GET /v2/transactions?OrderId=...``; a transaction in ``Paid`` means the order is paid
  (``Created`` / ``Pending`` / ``Declined`` leave it open while the link lives).
* Webhooks are signed with SHA512withRSA over the raw body, Base64 in ``X-Signature``; the public key
  comes from ``GET /public-key`` (or is pinned in the settings). The status is re-fetched before crediting.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import hashes

from app.db.models import PaymentProvider, PaymentStatus
from app.services.payments import rsa_keys
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
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

    from app.db.models import Payment, User

BASE_URL = "https://api.wata.pro/api/h2h"
SANDBOX_URL = "https://api-sandbox.wata.pro/api/h2h"
SIGNATURE_HEADER = "X-Signature"
MIN_LINK_TTL = timedelta(minutes=10)
MAX_LINK_TTL = timedelta(days=30)
KEY_REFRESH_SEC = 300.0  # a signature that fails with an older cached key triggers one re-download


def transactions_status(data: Any, order_id: str) -> ProviderStatus:
    """Status of an order from a ``/v2/transactions`` page: paid once any payment transaction is ``Paid``."""
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise GatewayError("unexpected WATA response: missing items")
    link_id: str | None = None
    for item in items:
        if not isinstance(item, dict) or str(item.get("orderId") or "") != order_id:
            continue
        if str(item.get("kind") or "Payment") != "Payment":
            continue
        if item.get("paymentLinkId"):
            link_id = str(item["paymentLinkId"])
        if str(item.get("status") or "") != "Paid":
            continue
        if str(item.get("currency") or "RUB").upper() != "RUB":
            raise GatewayError(f"WATA transaction in unexpected currency {item.get('currency')!r}")
        paid = None
        with suppress(ValueError):
            amount = item.get("amount")
            paid = amount_to_kop(amount if amount is not None else "")
        return ProviderStatus(PaymentStatus.SUCCEEDED, paid, link_id or (str(item["id"]) if item.get("id") else None))
    return ProviderStatus(PaymentStatus.PENDING, None, link_id)


class WataGateway(HttpGateway):
    key = PaymentProvider.WATA.value
    title = "💳 Карта / СБП (WATA)"
    supports_polling = True
    polls_by_payment_id = True
    min_amount_kop = 1000  # 10 RUB, the provider's minimum for a link

    def __init__(
        self,
        token: str,
        *,
        sandbox: bool = False,
        public_key: str | None = None,
        return_url: str | None = None,
        link_ttl: timedelta | None = None,
        timeout: float = 15.0,
        base_url: str | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        if not token:
            raise ValueError("WATA needs an access token")
        self._auth = {"Authorization": f"Bearer {token}"}
        self._base_url = (base_url or (SANDBOX_URL if sandbox else BASE_URL)).rstrip("/")
        self._return_url = return_url
        self._link_ttl = min(max(link_ttl, MIN_LINK_TTL), MAX_LINK_TTL) if link_ttl is not None else None
        self._pinned_key = rsa_keys.load_public_key(public_key) if public_key else None
        self._key: RSAPublicKey | None = None
        self._key_loaded_at = 0.0
        self._key_lock = asyncio.Lock()

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        body: dict[str, Any] = {
            "amount": kop_to_number(payment.amount_kop),
            "currency": "RUB",
            "description": description[:255] or "VPN",
            "orderId": payment.id,
        }
        if self._return_url:
            body["successRedirectUrl"] = self._return_url
            body["failRedirectUrl"] = self._return_url
        if self._link_ttl is not None:
            expires = datetime.now(UTC) + self._link_ttl
            body["expirationDateTime"] = expires.isoformat(timespec="seconds").replace("+00:00", "Z")
        data = await self._request_json("POST", f"{self._base_url}/links", json=body, headers=self._auth)
        return Invoice(
            provider_payment_id=str(require(data, "id")),
            url=str(require(data, "url")),
            provider_amount=kop_to_str(payment.amount_kop),
            provider_currency="RUB",
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        data = await self._request_json(
            "GET",
            f"{self._base_url}/v2/transactions",
            params={"OrderId": payment.id, "MaxResultCount": "100"},
            headers=self._auth,
            retries=1,
        )
        return transactions_status(data, payment.id)

    async def _public_key(self, *, refresh: bool = False) -> RSAPublicKey:
        """The webhook verification key: pinned, or downloaded and cached."""
        if self._pinned_key is not None:
            return self._pinned_key
        async with self._key_lock:
            stale = time.monotonic() - self._key_loaded_at > KEY_REFRESH_SEC
            if self._key is None or (refresh and stale):
                data = await self._request_json(
                    "GET", f"{self._base_url}/public-key", headers={"Content-Type": "application/json"}, retries=1
                )
                value = require(data, "value")
                try:
                    self._key = rsa_keys.load_public_key(str(value))
                except ValueError as exc:
                    raise GatewayError("WATA returned an unusable public key") from exc
                self._key_loaded_at = time.monotonic()
            return self._key

    async def verify_signature(self, raw: bytes, signature: str | None) -> bool:
        if not signature:
            return False
        try:
            key = await self._public_key()
            if rsa_keys.verify(key, raw, signature, hashes.SHA512()):
                return True
            if self._pinned_key is not None:
                return False
            # The key may have been rotated: re-download it (at most every KEY_REFRESH_SEC) and retry once.
            fresh = await self._public_key(refresh=True)
        except GatewayError as exc:
            raise WebhookRejected("WATA public key is unavailable", 503) from exc
        return fresh is not key and rsa_keys.verify(fresh, raw, signature, hashes.SHA512())

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if not await self.verify_signature(raw, get_header(headers, SIGNATURE_HEADER)):
            raise WebhookRejected("bad WATA signature", 401)
        data = json_body(raw)
        if str(data.get("kind") or "Payment") != "Payment" or data.get("transactionStatus") != "Paid":
            # Refunds, declines and the pre-payment check (``Created``) are acknowledged without action;
            # the pre-payment check must be answered quickly or the transaction is declined.
            return WebhookResult()
        order_id = data.get("orderId")
        link_id = data.get("paymentLinkId")
        return WebhookResult(
            payment_ids=[str(order_id)] if order_id else [],
            provider_ids=[str(link_id)] if link_id else [],
        )
