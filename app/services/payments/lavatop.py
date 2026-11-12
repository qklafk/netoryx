"""lava.top invoices.

The owner creates a digital product with a dynamic ("pay what you want") price on lava.top and configures
its offer id; every payment is a separate contract created with ``POST /api/v3/invoice`` for that offer
and the payment amount. The contract id is stored as ``provider_payment_id``.

Webhooks are authenticated by the key the owner sets in the lava.top webhook settings (``X-Api-Key``
header, or HTTP Basic credentials ``login:password``); the status is always re-fetched with
``GET /api/v1/invoices/{id}`` before crediting.
"""

from __future__ import annotations

import base64
import binascii
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
    amount_to_kop,
    get_header,
    json_body,
    kop_to_number,
    kop_to_str,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

BASE_URL = "https://gate.lava.top"
SUCCESS = {"completed", "subscription-active"}
CANCELED = {"failed", "cancelled", "subscription-failed"}


def status(invoice: Mapping[str, Any]) -> ProviderStatus:
    """Map a lava.top contract (``InvoiceResponseV2``) to our status."""
    value = str(invoice.get("status") or "").lower().replace("_", "-")
    state = (
        PaymentStatus.SUCCEEDED
        if value in SUCCESS
        else PaymentStatus.CANCELED
        if value in CANCELED
        else PaymentStatus.PENDING
    )
    paid = None
    receipt = invoice.get("receipt")
    if (
        state is PaymentStatus.SUCCEEDED
        and isinstance(receipt, dict)
        and str(receipt.get("currency") or "").upper() == "RUB"
        and receipt.get("amount") is not None
    ):
        with suppress(ValueError):
            paid = amount_to_kop(receipt["amount"])
    identifier = invoice.get("id")
    return ProviderStatus(state, paid, str(identifier) if identifier else None)


def webhook_authorized(headers: Mapping[str, str], secret: str) -> bool:
    """True if the request carries the configured key in ``X-Api-Key`` or as Basic ``login:password``."""
    given = get_header(headers, "X-Api-Key")
    if given is not None:
        return hmac.compare_digest(given.strip().encode(), secret.encode())
    auth = get_header(headers, "Authorization") or ""
    scheme, _, token = auth.strip().partition(" ")
    if scheme.lower() != "basic" or not token:
        return False
    try:
        decoded = base64.b64decode(token.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    return hmac.compare_digest(decoded, secret.encode())


class LavaTopGateway(HttpGateway):
    key = PaymentProvider.LAVATOP.value
    title = "💳 Карта (lava.top)"
    supports_polling = True
    min_amount_kop = 100

    def __init__(
        self,
        api_key: str,
        offer_id: str,
        webhook_secret: str,
        *,
        email_domain: str | None = None,
        payment_method: str | None = None,
        return_url: str | None = None,
        timeout: float = 15.0,
        base_url: str = BASE_URL,
    ) -> None:
        super().__init__(timeout=timeout)
        if not (api_key and offer_id and webhook_secret):
            raise ValueError("lava.top needs an API key, an offer id and a webhook key")
        self._api_key, self._offer_id, self._webhook_secret = api_key, offer_id, webhook_secret
        self._email_domain = (email_domain or "").strip().lstrip("@") or None
        self._payment_method = (payment_method or "card").strip().upper()
        self._return_url = return_url
        self._base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._api_key, "Accept": "application/json"}

    def _email(self, user: User) -> str:
        """lava.top requires a buyer email; users without one get a stable per-user address."""
        if user.receipt_email:
            return user.receipt_email
        if not self._email_domain:
            raise GatewayError("lava.top needs a buyer email: set LAVATOP_EMAIL_DOMAIN")
        return f"tg{user.tg_id}@{self._email_domain}"

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        body: dict[str, Any] = {
            "email": self._email(user),
            "offerId": self._offer_id,
            "currency": "RUB",
            "amount": kop_to_number(payment.amount_kop),
            "buyerLanguage": "RU",
        }
        if self._payment_method == "SBP":
            # SBP is offered only through the PAY2ME provider; cards use the default provider.
            body["paymentProvider"] = "PAY2ME"
            body["paymentMethod"] = "SBP"
        if self._return_url:
            for field in ("successful_return_url", "failure_return_url", "cancel_return_url"):
                body[field] = self._return_url
        result = await self._request_json(
            "POST", f"{self._base_url}/api/v3/invoice", json=body, headers=self._headers(), ok_statuses=(200, 201)
        )
        if not isinstance(result, dict):
            raise GatewayError("lava.top returned an invalid invoice")
        contract_id, url = result.get("id"), result.get("paymentUrl")
        if not contract_id or not url:
            raise GatewayError("lava.top invoice has no id or payment URL")
        return Invoice(str(contract_id), str(url), kop_to_str(payment.amount_kop), "RUB")

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if not payment.provider_payment_id:
            raise GatewayError("payment has no lava.top contract id")
        contract_id = payment.provider_payment_id
        invoice = await self._request_json(
            "GET",
            f"{self._base_url}/api/v1/invoices/{quote(contract_id, safe='')}",
            headers=self._headers(),
            retries=2,
            ok_statuses=(200,),
        )
        if not isinstance(invoice, dict) or str(invoice.get("id") or "") != contract_id:
            raise GatewayError("lava.top returned another contract")
        return status(invoice)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if not webhook_authorized(headers, self._webhook_secret):
            raise WebhookRejected("bad lava.top webhook key", 401)
        data = json_body(raw)
        contract_id = data.get("contractId")
        if not isinstance(contract_id, str) or not contract_id:
            raise WebhookRejected("lava.top webhook has no contractId", 400)
        return WebhookResult(provider_ids=[contract_id])
