"""CryptoBot (Crypto Pay API) gateway: crypto payments for invoices denominated in RUB.

* Auth: ``Crypto-Pay-API-Token`` header.
* Invoices: ``createInvoice`` with ``currency_type=fiat``, ``fiat=RUB`` and a decimal ``amount`` string;
  the user pays any of ``accepted_assets`` at the current rate.
* Status: ``getInvoices?invoice_ids=...`` (``active`` / ``paid`` / ``expired``).
* Webhooks: ``crypto-pay-api-signature`` = hex(HMAC-SHA256(key=SHA256(token), msg=raw body)).
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
    amount_to_kop,
    get_header,
    json_body,
    kop_to_plain,
    require,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

MAINNET_URL = "https://pay.crypt.bot/api"
TESTNET_URL = "https://testnet-pay.crypt.bot/api"
MAX_EXPIRES_IN = 2_678_400  # 31 days, the API maximum
SIGNATURE_HEADER = "crypto-pay-api-signature"

_STATUSES = {
    "active": PaymentStatus.PENDING,
    "paid": PaymentStatus.SUCCEEDED,
    "expired": PaymentStatus.EXPIRED,
}


def sign_body(raw: bytes, token: str) -> str:
    """The signature CryptoBot puts into ``crypto-pay-api-signature``."""
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, raw, hashlib.sha256).hexdigest()


def verify_signature(raw: bytes, signature: str | None, token: str) -> bool:
    """Constant-time check of a webhook signature."""
    given = (signature or "").strip().lower().encode("utf-8", "replace")  # bytes: the header may be non-ASCII
    return bool(signature) and hmac.compare_digest(sign_body(raw, token).encode(), given)


class CryptoBotGateway(HttpGateway):
    key = PaymentProvider.CRYPTOBOT.value
    title = "🪙 Криптовалюта (CryptoBot)"
    supports_polling = True
    min_amount_kop = 100

    def __init__(
        self,
        token: str,
        *,
        testnet: bool = False,
        accepted_assets: str = "USDT,TON,BTC,ETH,LTC,TRX,USDC",
        expires_in_sec: int = 24 * 3600,
        timeout: float = 15.0,
        base_url: str | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self._token = token
        self._base_url = (base_url or (TESTNET_URL if testnet else MAINNET_URL)).rstrip("/")
        self._accepted_assets = ",".join(a.strip().upper() for a in accepted_assets.split(",") if a.strip())
        self._expires_in = max(1, min(expires_in_sec, MAX_EXPIRES_IN))

    async def _call(self, method: str, *, retries: int = 0, **payload: Any) -> Any:
        data = await self._request_json(
            "POST",
            f"{self._base_url}/{method}",
            json=payload,
            headers={"Crypto-Pay-API-Token": self._token},
            retries=retries,
            ok_statuses=(200, 400, 401, 404),  # API errors come as {"ok": false, "error": {...}}
        )
        if not isinstance(data, dict) or data.get("ok") is not True:
            error = data.get("error") if isinstance(data, dict) else None
            raise GatewayError(f"CryptoBot {method} failed: {error!r}")
        return data.get("result")

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        payload: dict[str, Any] = {
            "currency_type": "fiat",
            "fiat": "RUB",
            "amount": kop_to_plain(payment.amount_kop),
            "description": description[:1024],
            "payload": payment.id,
            "expires_in": self._expires_in,
            "allow_comments": False,
        }
        if self._accepted_assets:
            payload["accepted_assets"] = self._accepted_assets
        # createInvoice is not idempotent: never retried.
        result = await self._call("createInvoice", **payload)
        return Invoice(
            provider_payment_id=str(require(result, "invoice_id")),
            url=result.get("bot_invoice_url") or result.get("mini_app_invoice_url"),
            provider_amount=str(result.get("amount") or payload["amount"]),
            provider_currency="RUB",
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if not payment.provider_payment_id:
            raise GatewayError("payment has no CryptoBot invoice id")
        result = await self._call("getInvoices", retries=2, invoice_ids=payment.provider_payment_id)
        items = require(result, "items")
        invoice = next(
            (i for i in items if isinstance(i, dict) and str(i.get("invoice_id")) == payment.provider_payment_id),
            None,
        )
        if invoice is None:
            raise GatewayError(f"CryptoBot invoice {payment.provider_payment_id} not found")
        return invoice_status(invoice)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if not verify_signature(raw, get_header(headers, SIGNATURE_HEADER), self._token):
            raise WebhookRejected("bad CryptoBot signature", 401)
        update = json_body(raw)
        invoice = update.get("payload")
        if update.get("update_type") != "invoice_paid" or not isinstance(invoice, dict):
            return WebhookResult()
        our_id = invoice.get("payload")
        provider_id = invoice.get("invoice_id")
        return WebhookResult(
            payment_ids=[our_id] if isinstance(our_id, str) and our_id else [],
            provider_ids=[str(provider_id)] if provider_id is not None else [],
        )


def invoice_status(invoice: dict[str, Any]) -> ProviderStatus:
    """Map a Crypto Pay ``Invoice`` object to a ProviderStatus.

    The paid amount is known only for fiat RUB invoices (``amount`` is then in rubles).
    """
    status = _STATUSES.get(str(invoice.get("status")), PaymentStatus.PENDING)
    paid: int | None = None
    if status is PaymentStatus.SUCCEEDED and invoice.get("fiat") == "RUB":
        try:
            paid = amount_to_kop(invoice.get("amount", ""))
        except ValueError:
            paid = None
    invoice_id = invoice.get("invoice_id")
    return ProviderStatus(status, paid, str(invoice_id) if invoice_id is not None else None)
