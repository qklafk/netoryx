"""Payment providers and the payment lifecycle.

* :mod:`.base`     - ``PaymentGateway`` protocol, value objects, money/IP helpers.
* :mod:`.service`  - create / credit (exactly once) / webhooks / reconciler.
* :mod:`.registry` - enabled gateways from settings.
* one module per provider: :mod:`.stars`, :mod:`.cryptobot`, :mod:`.platega`, :mod:`.yookassa`,
  :mod:`.yoomoney`, :mod:`.freekassa`, :mod:`.robokassa`.
"""

from app.services.payments.base import (
    GatewayError,
    Invoice,
    PaymentGateway,
    ProviderStatus,
    WebhookRejected,
    WebhookResult,
)

__all__ = [
    "GatewayError",
    "Invoice",
    "PaymentGateway",
    "ProviderStatus",
    "WebhookRejected",
    "WebhookResult",
]
