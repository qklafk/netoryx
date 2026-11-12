"""Manual card/SBP transfer placeholder gateway.

It creates a regular pending payment but never claims it paid: a staff member
reviews the Telegram receipt and uses the normal idempotent admin credit action.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from app.db.models import PaymentProvider
from app.services.payments.base import (
    GatewayError,
    Invoice,
    ProviderStatus,
    WebhookRejected,
    WebhookResult,
    kop_to_plain,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User


class ManualTransferGateway:
    key = PaymentProvider.MANUAL.value
    title = "💳 Карта / СБП (ручной перевод)"
    supports_polling = False
    min_amount_kop = 100

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        return Invoice(payment.id, "", kop_to_plain(payment.amount_kop), "RUB")

    async def fetch(self, payment: Payment) -> ProviderStatus:
        raise GatewayError("manual transfers are confirmed by an administrator")

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        raise WebhookRejected("manual transfers have no webhooks", 404)

    async def close(self) -> None:
        return None
