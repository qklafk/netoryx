"""YooMoney quickpay gateway: card or wallet payments to a personal YooMoney wallet.

* Create: a ``https://yoomoney.ru/quickpay/confirm`` link built locally (no API call); ``label`` = our
  payment id.
* Notifications: ``application/x-www-form-urlencoded`` POST signed with ``sign`` =
  hex(HMAC-SHA256(secret, s)), where ``s`` joins every other field sorted by name as ``k=v`` with
  ``&``, values percent-encoded per RFC 3986. The deprecated ``sha1_hash`` is not accepted.
* YooMoney cannot be polled without OAuth, so a verified notification is trusted as is.
* The link is not single-use: a second transfer through it (same ``label``, new ``operation_id``) is
  recorded and credited as a separate top-up by the payment service (``repeat_payments``).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, quote, urlencode

from app.db.models import PaymentProvider, PaymentStatus
from app.services.payments.base import (
    GatewayError,
    Invoice,
    ProviderStatus,
    WebhookRejected,
    WebhookResult,
    amount_to_kop,
    kop_to_str,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

QUICKPAY_URL = "https://yoomoney.ru/quickpay/confirm"
RUB_CODE = "643"
MAX_LABEL = 64


def notification_sign(fields: Mapping[str, str], secret: str) -> str:
    """Compute the ``sign`` of a notification from all its fields except ``sign``."""
    message = "&".join(f"{k}={quote(v, safe='')}" for k, v in sorted(fields.items()) if k != "sign")
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def verify_notification(fields: Mapping[str, str], secret: str) -> bool:
    """Constant-time check of the ``sign`` field."""
    given = fields.get("sign", "").strip().lower()
    # bytes, not str: compare_digest raises TypeError on non-ASCII str (an attacker-controlled field)
    return bool(given) and hmac.compare_digest(
        notification_sign(fields, secret).encode(), given.encode("utf-8", "replace")
    )


class YooMoneyGateway:
    key = PaymentProvider.YOOMONEY.value
    supports_polling = False
    # The quickpay link can be paid again and again: each transfer has its own operation_id.
    repeat_payments = "credit"
    min_amount_kop = 200  # [unverified] quickpay rejects sums below 2 RUB

    def __init__(
        self,
        wallet: str,
        notification_secret: str,
        *,
        payment_type: str = "AC",
        return_url: str | None = None,
        quickpay_url: str = QUICKPAY_URL,
    ) -> None:
        self._wallet = wallet
        self._secret = notification_secret
        self._payment_type = payment_type
        self._return_url = return_url
        self._quickpay_url = quickpay_url
        self.title = "👛 Кошелёк ЮMoney" if payment_type == "PC" else "💳 Банковская карта (ЮMoney)"

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        if len(payment.id) > MAX_LABEL:
            raise GatewayError("payment id is too long for a YooMoney label")
        amount = kop_to_str(payment.amount_kop)
        params = {
            "receiver": self._wallet,
            "quickpay-form": "button",
            "paymentType": self._payment_type,
            "sum": amount,
            "label": payment.id,
            "targets": description[:150],
        }
        if self._return_url:
            params["successURL"] = self._return_url
        return Invoice(None, f"{self._quickpay_url}?{urlencode(params)}", amount, "RUB")

    async def fetch(self, payment: Payment) -> ProviderStatus:
        raise GatewayError("YooMoney quickpay cannot be polled")

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        try:
            fields = dict(parse_qsl(raw.decode(), keep_blank_values=True, strict_parsing=bool(raw)))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WebhookRejected("malformed form body", 400) from exc
        if not verify_notification(fields, self._secret):
            raise WebhookRejected("bad YooMoney signature", 401)
        label = fields.get("label", "")
        if not label or fields.get("currency") != RUB_CODE:
            return WebhookResult()  # dashboard test notifications have no label
        operation_id = fields.get("operation_id") or None
        # Held by the receiver's balance limit, or protected by a code the sender can still take back: not credited
        # yet; the payment service records it (``payment.held``) and keeps the payment from expiring until an
        # administrator credits it.
        if fields.get("unaccepted") == "true" or fields.get("codepro") == "true":
            status = ProviderStatus(PaymentStatus.PENDING, None, operation_id)
        else:
            status = ProviderStatus(PaymentStatus.SUCCEEDED, _paid_kop(fields), operation_id)
        return WebhookResult(
            payment_ids=[label],
            provider_ids=[operation_id] if operation_id else [],
            trusted_status=status,
        )

    async def close(self) -> None:
        return None


def _paid_kop(fields: Mapping[str, str]) -> int | None:
    """What the payer paid: ``withdraw_amount`` (fees are the receiver's), else the credited ``amount``."""
    for name in ("withdraw_amount", "amount"):
        value = fields.get(name)
        if value:
            try:
                return amount_to_kop(value)
            except ValueError:
                continue
    return None
