"""YooKassa (API v3) gateway: cards, SBP, SberPay, YooMoney.

* Auth: HTTP Basic ``shopId:secretKey``; ``Idempotence-Key`` (our payment id) on every POST, so creating
  a payment can be retried safely.
* Status: ``GET /v3/payments/{id}`` (``pending`` / ``waiting_for_capture`` / ``succeeded`` / ``canceled``).
* Webhooks are unsigned: the source IP is checked against YooKassa's allowlist and the payment is
  always re-fetched from the API.
* Autopayments: with the payer's consent the first payment asks to keep the method
  (``save_payment_method``); a paid payment then reports ``payment_method.saved`` and its ``id``, which
  is charged later with ``payment_method_id`` and no confirmation. The shop must have autopayments enabled.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from app.db.models import PaymentProvider, PaymentStatus
from app.services.payments.base import (
    ChargeResult,
    GatewayError,
    HttpGateway,
    Invoice,
    IPNetwork,
    ProviderStatus,
    SavedMethod,
    WebhookRejected,
    WebhookResult,
    amount_to_kop,
    client_ip,
    ip_in,
    json_body,
    kop_to_str,
    parse_networks,
    require,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

BASE_URL = "https://api.yookassa.ru/v3"
DEFAULT_RETURN_URL = "https://t.me"

# https://yookassa.ru/developers/using-api/webhooks#ip
WEBHOOK_NETWORKS: tuple[str, ...] = (
    "185.71.76.0/27",
    "185.71.77.0/27",
    "77.75.153.0/25",
    "77.75.156.11/32",
    "77.75.156.35/32",
    "77.75.154.128/25",
    "2a02:5180::/32",
)

_STATUSES = {
    "pending": PaymentStatus.PENDING,
    "waiting_for_capture": PaymentStatus.PENDING,  # we always create with capture=true
    "succeeded": PaymentStatus.SUCCEEDED,
    "canceled": PaymentStatus.CANCELED,
}
# cancellation_details.reason values after which a saved method can never be charged again.
INVALID_METHOD_REASONS = frozenset({"permission_revoked", "card_expired", "payment_method_restricted"})


class YooKassaGateway(HttpGateway):
    key = PaymentProvider.YOOKASSA.value
    title = "💳 Карта / СБП (ЮKassa)"
    supports_polling = True
    min_amount_kop = 100

    def __init__(
        self,
        shop_id: str,
        secret_key: str,
        *,
        return_url: str | None = None,
        receipt_enabled: bool = False,
        receipt_email: str | None = None,
        vat_code: int = 1,
        check_ip: bool = True,
        trusted_proxies: Iterable[str] = (),
        timeout: float = 15.0,
        base_url: str = BASE_URL,
    ) -> None:
        super().__init__(timeout=timeout)
        token = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()
        self._auth = {"Authorization": f"Basic {token}"}
        self._return_url = return_url or DEFAULT_RETURN_URL
        self._receipt_enabled = receipt_enabled
        self._receipt_email = receipt_email
        self._vat_code = vat_code
        self._check_ip = check_ip
        self._allowed: list[IPNetwork] = parse_networks(WEBHOOK_NETWORKS)
        self._proxies: list[IPNetwork] = parse_networks(trusted_proxies)
        self._base_url = base_url.rstrip("/")

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        amount = {"value": kop_to_str(payment.amount_kop), "currency": "RUB"}
        body: dict[str, Any] = {
            "amount": amount,
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": self._return_url},
            "description": description[:128],
            "metadata": {"payment_id": payment.id, "user_id": str(user.tg_id)},
        }
        if self._receipt_enabled:
            body["receipt"] = self._receipt(amount, user, description)
        if getattr(payment, "save_method", False):
            body["save_payment_method"] = True
        data = await self._request_json(
            "POST",
            f"{self._base_url}/payments",
            json=body,
            headers={**self._auth, "Idempotence-Key": payment.id},
            retries=2,  # safe: the Idempotence-Key makes repeats return the same payment
        )
        return Invoice(
            provider_payment_id=str(require(data, "id")),
            url=str(require(data, "confirmation", "confirmation_url")),
            provider_amount=amount["value"],
            provider_currency="RUB",
        )

    def _receipt(self, amount: dict[str, str], user: User, description: str) -> dict[str, Any]:
        """54-FZ receipt with a single "service" item; needs an e-mail (fixed or the user's)."""
        email = self._receipt_email or user.receipt_email
        if not email:
            raise GatewayError("YooKassa receipt requires an e-mail")
        return {
            "customer": {"email": email},
            "items": [
                {
                    "description": description[:128],
                    "quantity": "1.00",
                    "amount": amount,
                    "vat_code": self._vat_code,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }
            ],
        }

    async def charge_saved(self, payment: Payment, user: User, token: str, description: str) -> ChargeResult:
        """Autopayment from a saved method: no confirmation, the result usually comes back at once."""
        amount = {"value": kop_to_str(payment.amount_kop), "currency": "RUB"}
        body: dict[str, Any] = {
            "amount": amount,
            "capture": True,
            "payment_method_id": token,
            "description": description[:128],
            "metadata": {"payment_id": payment.id, "user_id": str(user.tg_id)},
        }
        if self._receipt_enabled:
            body["receipt"] = self._receipt(amount, user, description)
        data = await self._request_json(
            "POST",
            f"{self._base_url}/payments",
            json=body,
            headers={**self._auth, "Idempotence-Key": payment.id},
            retries=2,
        )
        status = payment_status(data)
        reason: str | None = None
        details = data.get("cancellation_details") if isinstance(data, dict) else None
        if status.status is PaymentStatus.CANCELED and isinstance(details, dict):
            reason = str(details.get("reason") or "") or None
        return ChargeResult(status, method_invalid=reason in INVALID_METHOD_REASONS, reason=reason)

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if not payment.provider_payment_id:
            raise GatewayError("payment has no YooKassa id")
        data = await self._request_json(
            "GET", f"{self._base_url}/payments/{payment.provider_payment_id}", headers=self._auth, retries=2
        )
        return payment_status(data)

    def is_allowed_source(self, remote_ip: str | None, headers: Mapping[str, str]) -> bool:
        """True if the request comes from YooKassa (through trusted proxies only)."""
        return ip_in(client_ip(remote_ip, headers, self._proxies), self._allowed)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if self._check_ip and not self.is_allowed_source(remote_ip, headers):
            raise WebhookRejected("source IP is not a YooKassa address", 403)
        data = json_body(raw)
        event = str(data.get("event", ""))
        obj = data.get("object")
        if not event.startswith("payment.") or not isinstance(obj, dict) or not obj.get("id"):
            return WebhookResult()  # refunds, payouts, ... are not ours to handle here
        metadata = obj.get("metadata")
        our_id = metadata.get("payment_id") if isinstance(metadata, dict) else None
        return WebhookResult(
            payment_ids=[our_id] if isinstance(our_id, str) and our_id else [],
            provider_ids=[str(obj["id"])],
        )


def payment_status(data: Any) -> ProviderStatus:
    """Map a YooKassa payment object to a ProviderStatus (paid amount only for RUB)."""
    raw_status = str(require(data, "status"))
    status = _STATUSES.get(raw_status)
    if status is None:
        raise GatewayError(f"unknown YooKassa status {raw_status!r}")
    paid: int | None = None
    amount = data.get("amount")
    if status is PaymentStatus.SUCCEEDED and isinstance(amount, dict) and amount.get("currency") == "RUB":
        try:
            paid = amount_to_kop(amount.get("value", ""))
        except ValueError:
            paid = None
    saved = _saved_method(data) if status is PaymentStatus.SUCCEEDED else None
    return ProviderStatus(status, paid, str(data.get("id")) if data.get("id") else None, saved)


def _saved_method(data: dict[str, Any]) -> SavedMethod | None:
    """The reusable method of a paid payment, if YooKassa saved it."""
    method = data.get("payment_method")
    if not isinstance(method, dict) or method.get("saved") is not True or not method.get("id"):
        return None
    card = method.get("card")
    if isinstance(card, dict) and card.get("last4"):
        title = f"{card.get('card_type') or 'Card'} •• {card['last4']}"
    else:
        title = str(method.get("title") or method.get("type") or "YooKassa")
    return SavedMethod(str(method["id"]), title[:64])
