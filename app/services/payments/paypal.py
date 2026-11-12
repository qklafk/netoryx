"""PayPal Orders v2 gateway (redirect checkout, immediate capture).

Flow: an order with intent ``CAPTURE`` is created and the buyer approves it on PayPal; the order is then
captured by :meth:`PayPalGateway.fetch` (called by the webhook handler and the reconciler) with an
idempotent ``PayPal-Request-Id``. Only a COMPLETED capture of at least the invoiced amount is credited.
Webhooks are verified through PayPal's ``verify-webhook-signature`` API, then the order is fetched again.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.db.models import PaymentProvider, PaymentStatus
from app.services.payments import fx
from app.services.payments.base import (
    GatewayError,
    HttpGateway,
    Invoice,
    ProviderStatus,
    WebhookRejected,
    WebhookResult,
    get_header,
    json_body,
    require,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

log = logging.getLogger(__name__)

LIVE_URL = "https://api-m.paypal.com"
SANDBOX_URL = "https://api-m.sandbox.paypal.com"

# Currencies PayPal accepts for payments; the ones in NO_DECIMALS reject fractional amounts.
CURRENCIES = frozenset(
    {
        "AUD", "BRL", "CAD", "CNY", "CZK", "DKK", "EUR", "HKD", "HUF", "ILS", "JPY", "MYR", "MXN",
        "TWD", "NZD", "NOK", "PHP", "PLN", "GBP", "RUB", "SGD", "SEK", "CHF", "THB", "USD",
    }
)  # fmt: skip
NO_DECIMALS = frozenset({"HUF", "JPY", "TWD"})

SIGNATURE_HEADERS = {
    "auth_algo": "PAYPAL-AUTH-ALGO",
    "cert_url": "PAYPAL-CERT-URL",
    "transmission_id": "PAYPAL-TRANSMISSION-ID",
    "transmission_sig": "PAYPAL-TRANSMISSION-SIG",
    "transmission_time": "PAYPAL-TRANSMISSION-TIME",
}
TOKEN_SAFETY_MARGIN_SEC = 60


def exponent(currency: str) -> int:
    return 0 if currency in NO_DECIMALS else 2


def _captures(order: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    units = order.get("purchase_units")
    for unit in units if isinstance(units, list) else []:
        payments = unit.get("payments") if isinstance(unit, dict) else None
        captures = payments.get("captures") if isinstance(payments, dict) else None
        for capture in captures if isinstance(captures, list) else []:
            if isinstance(capture, dict):
                yield capture


class PayPalGateway(HttpGateway):
    key = PaymentProvider.PAYPAL.value
    title = "🅿️ PayPal"
    supports_polling = True
    min_amount_kop = 100

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        webhook_id: str | None = None,
        sandbox: bool = False,
        currency: str = "USD",
        rub_per_unit: str | Decimal | None,
        return_url: str | None = None,
        brand_name: str | None = None,
        timeout: float = 15,
        base_url: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(timeout=timeout)
        if not client_id or not client_secret:
            raise ValueError("PayPal needs a client ID and secret")
        self._currency = fx.normalize_currency(currency)
        if self._currency not in CURRENCIES:
            raise ValueError(f"PayPal does not support currency {self._currency}")
        self._rate = fx.parse_rate(rub_per_unit)
        self._exp = exponent(self._currency)
        self._whole = self._currency in NO_DECIMALS
        self._client_id, self._client_secret, self._webhook_id = client_id, client_secret, webhook_id or ""
        self._return_url, self._brand_name, self._clock = return_url, brand_name, clock
        self._base_url = (base_url or (SANDBOX_URL if sandbox else LIVE_URL)).rstrip("/")
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()

    def to_minor(self, amount_kop: int) -> int:
        """Charge amount in minor units of the currency (rounded up)."""
        return fx.kop_to_minor(amount_kop, self._rate, self._exp, whole_units=self._whole)

    # --- auth ------------------------------------------------------------------------------------
    async def _access_token(self) -> str:
        """OAuth2 client-credentials token, cached until shortly before it expires."""
        async with self._token_lock:
            if self._token and self._clock() < self._token_expires:
                return self._token
            credentials = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
            data = await self._request_json(
                "POST",
                f"{self._base_url}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json", "Authorization": f"Basic {credentials}"},
                retries=2,
            )
            token = require(data, "access_token")
            if not isinstance(token, str) or not token:
                raise GatewayError("PayPal returned an empty access token")
            try:
                lifetime = float(data.get("expires_in") or 0)
            except (TypeError, ValueError):
                lifetime = 0.0
            self._token = token
            self._token_expires = self._clock() + max(0.0, lifetime - TOKEN_SAFETY_MARGIN_SEC)
            return token

    async def _api(self, method: str, path: str, *, headers: dict[str, str] | None = None, **kwargs: Any) -> Any:
        token = await self._access_token()
        try:
            return await self._request_json(
                method,
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {token}", **(headers or {})},
                **kwargs,
            )
        except GatewayError as exc:
            if ": HTTP 401" in str(exc):  # revoked or expired early: get a fresh token next time
                self._token = None
            raise

    # --- invoices --------------------------------------------------------------------------------
    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        minor = self.to_minor(payment.amount_kop)
        value = fx.minor_to_str(minor, self._exp)
        context: dict[str, Any] = {"shipping_preference": "NO_SHIPPING", "user_action": "PAY_NOW"}
        if self._return_url:
            context["return_url"] = self._return_url
            context["cancel_url"] = self._return_url
        if self._brand_name:
            context["brand_name"] = self._brand_name[:127]
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "custom_id": payment.id,
                    "invoice_id": payment.id,
                    "description": (description or "VPN")[:127],
                    "amount": {"currency_code": self._currency, "value": value},
                }
            ],
            "payment_source": {"paypal": {"experience_context": context}},
        }
        data = await self._api(
            "POST",
            "/v2/checkout/orders",
            json=body,
            headers={"PayPal-Request-Id": f"order-{payment.id}"},
            retries=2,  # the request id makes a repeated create return the same order
        )
        order_id = require(data, "id")
        url = next(
            (
                link.get("href")
                for rel in ("payer-action", "approve")
                for link in (data.get("links") or [])
                if isinstance(link, dict) and link.get("rel") == rel and link.get("href")
            ),
            None,
        )
        if not isinstance(url, str):
            raise GatewayError("PayPal returned no approval link")
        return Invoice(str(order_id), url, value, self._currency)

    def _expected_minor(self, payment: Payment) -> int:
        if payment.provider_amount and (payment.provider_currency or "").upper() == self._currency:
            try:
                return fx.str_to_minor(payment.provider_amount, self._exp)
            except ValueError:
                pass
        return self.to_minor(payment.amount_kop)

    def status(self, payment: Payment, order: Mapping[str, Any]) -> ProviderStatus:
        """Map an order (with its captures) to our status."""
        units = order.get("purchase_units")
        for unit in units if isinstance(units, list) else []:
            custom_id = unit.get("custom_id") if isinstance(unit, dict) else None
            if custom_id is not None and str(custom_id) != payment.id:
                raise GatewayError("PayPal order belongs to another payment")
        order_id = str(order["id"]) if order.get("id") else None
        state = str(order.get("status") or "")
        if state == "VOIDED":
            return ProviderStatus(PaymentStatus.CANCELED, provider_payment_id=order_id)
        if state != "COMPLETED":
            return ProviderStatus(PaymentStatus.PENDING, provider_payment_id=order_id)

        captures = list(_captures(order))
        paid = 0
        for capture in captures:
            if capture.get("status") not in ("COMPLETED", "PARTIALLY_REFUNDED"):
                continue
            amount = capture.get("amount")
            if not isinstance(amount, dict) or str(amount.get("currency_code") or "").upper() != self._currency:
                continue
            try:
                paid += fx.str_to_minor(amount.get("value"), self._exp)
            except ValueError:
                continue
        if paid:
            expected = self._expected_minor(payment)
            if paid < expected:
                log.warning(
                    "payment %s: PayPal captured %s, expected %s (%s minor units); not credited",
                    payment.id,
                    paid,
                    expected,
                    self._currency,
                )
                return ProviderStatus(PaymentStatus.PENDING, provider_payment_id=order_id)
            return ProviderStatus(PaymentStatus.SUCCEEDED, provider_payment_id=order_id)
        statuses = {str(c.get("status") or "") for c in captures}
        if statuses and statuses <= {"REFUNDED"}:
            return ProviderStatus(PaymentStatus.REFUNDED, provider_payment_id=order_id)
        if statuses and statuses <= {"DECLINED", "FAILED"}:
            return ProviderStatus(PaymentStatus.CANCELED, provider_payment_id=order_id)
        return ProviderStatus(PaymentStatus.PENDING, provider_payment_id=order_id)

    async def _get_order(self, order_id: str) -> dict[str, Any]:
        data = await self._api("GET", f"/v2/checkout/orders/{order_id}", retries=2)
        if not isinstance(data, dict):
            raise GatewayError("PayPal returned invalid order")
        return data

    async def _capture(self, payment: Payment, order_id: str) -> dict[str, Any]:
        """Capture an approved order; a repeated call with the same request id is a no-op at PayPal."""
        try:
            data = await self._api(
                "POST",
                f"/v2/checkout/orders/{order_id}/capture",
                json={},
                headers={"PayPal-Request-Id": payment.id, "Prefer": "return=representation"},
                retries=2,
            )
        except GatewayError as exc:
            # Already captured, declined instrument, ...: the order itself tells what happened.
            log.info("payment %s: PayPal capture failed: %s", payment.id, exc)
            return await self._get_order(order_id)
        if not isinstance(data, dict) or data.get("status") is None:
            return await self._get_order(order_id)
        return data

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if not payment.provider_payment_id:
            raise GatewayError("payment has no PayPal order id")
        order = await self._get_order(payment.provider_payment_id)
        if order.get("status") == "APPROVED":
            self.status(payment, order)  # refuses to capture an order of another payment
            order = await self._capture(payment, payment.provider_payment_id)
        return self.status(payment, order)

    # --- webhooks --------------------------------------------------------------------------------
    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if not self._webhook_id:
            raise WebhookRejected("PayPal webhook id is not configured", 403)
        event = json_body(raw)
        fields: dict[str, str] = {}
        for name, header in SIGNATURE_HEADERS.items():
            value = get_header(headers, header)
            if not value:
                raise WebhookRejected(f"missing {header}", 400)
            fields[name] = value
        fields["webhook_id"] = self._webhook_id
        # The event must be posted back byte for byte, so it is spliced in rather than re-serialized.
        prefix = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))[:-1]
        body = f'{prefix},"webhook_event":'.encode() + raw.strip() + b"}"
        try:
            result = await self._api(
                "POST",
                "/v1/notifications/verify-webhook-signature",
                data=body,
                headers={"Content-Type": "application/json"},
                retries=2,
            )
        except GatewayError as exc:
            raise WebhookRejected(f"PayPal signature check unavailable: {exc}", 503) from exc
        if not isinstance(result, dict) or result.get("verification_status") != "SUCCESS":
            raise WebhookRejected("bad PayPal signature", 401)
        return self._ids(event)

    @staticmethod
    def _ids(event: Mapping[str, Any]) -> WebhookResult:
        resource = event.get("resource")
        if not isinstance(resource, dict):
            return WebhookResult()
        payment_ids: list[str] = []
        provider_ids: list[str] = []

        def add(target: list[str], value: object) -> None:
            if isinstance(value, str) and value and value not in target:
                target.append(value)

        resource_type = str(event.get("resource_type") or "").lower()
        if resource_type == "checkout-order" or str(event.get("event_type") or "").startswith("CHECKOUT.ORDER."):
            add(provider_ids, resource.get("id"))
            units = resource.get("purchase_units")
            for unit in units if isinstance(units, list) else []:
                if isinstance(unit, dict):
                    add(payment_ids, unit.get("custom_id"))
                    add(payment_ids, unit.get("invoice_id"))
        else:
            add(payment_ids, resource.get("custom_id"))
            add(payment_ids, resource.get("invoice_id"))
            supplementary = resource.get("supplementary_data")
            related = supplementary.get("related_ids") if isinstance(supplementary, dict) else None
            if isinstance(related, dict):
                add(provider_ids, related.get("order_id"))
        return WebhookResult(payment_ids=payment_ids, provider_ids=provider_ids)
