"""SeverPay: card and SBP payments through a hosted payment page.

API reference: https://docs.severpay.io/en/ (payin: /en/payin/create, /en/payin/get; /en/webhook/)

* Every request body carries ``mid`` (merchant id), ``salt`` and ``sign``: the keys of the body are sorted, the
  compact JSON is signed with HMAC-SHA256 under the merchant's secret token, and ``sign`` is added to the body.
* Create: ``POST /payin/create`` with ``order_id`` = our payment id -> ``data.id``, ``data.url``.
* Status: ``POST /payin/get`` with the SeverPay payment ``id``.
* Webhook ``{"type": "payin", "data": {...}, "salt", "sign"}``: ``sign`` is the HMAC-SHA256 of the JSON encoding of
  the body without ``sign`` (PHP ``json_encode`` rules). The status is re-fetched before crediting. The
  acknowledgement must be the JSON object ``{"status": true}``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from decimal import Decimal
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
    json_body,
    kop_to_number,
    kop_to_str,
    require,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

BASE_URL = "https://severpay.io/api/merchant"
ACK_BODY = '{"status": true}'

_STATUSES = {
    "new": PaymentStatus.PENDING,
    "process": PaymentStatus.PENDING,
    "success": PaymentStatus.SUCCEEDED,
    "decline": PaymentStatus.CANCELED,
    "fail": PaymentStatus.CANCELED,
}


def _mac(token: str, text: str) -> str:
    return hmac.new(token.encode(), text.encode(), hashlib.sha256).hexdigest()


def sign_request(token: str, body: dict[str, Any]) -> dict[str, Any]:
    """Return ``body`` sorted by key with its ``sign`` appended."""
    ordered = dict(sorted(body.items()))
    text = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    ordered["sign"] = _mac(token, text)
    return ordered


def _php_json(data: dict[str, Any], *, escaped: bool) -> str:
    """JSON the way PHP encodes it; ``escaped`` is the default (``\\/`` and ``\\uXXXX``)."""
    if not escaped:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace("/", "\\/")


def webhook_signature_valid(token: str, body: dict[str, Any]) -> bool:
    given = body.get("sign")
    if not isinstance(given, str) or not given:
        return False
    rest = {k: v for k, v in body.items() if k != "sign"}
    candidates = {_mac(token, _php_json(rest, escaped=True)), _mac(token, _php_json(rest, escaped=False))}
    return any(hmac.compare_digest(c.encode(), given.strip().lower().encode()) for c in candidates)


def payment_status(data: Any, expected_order_id: str | None = None) -> ProviderStatus:
    """Map a ``data`` object (status response or webhook) to a ProviderStatus."""
    if not isinstance(data, dict):
        raise GatewayError("unexpected SeverPay response")
    raw_status = str(require(data, "status")).lower()
    status = _STATUSES.get(raw_status)
    if status is None:
        raise GatewayError(f"unknown SeverPay status {raw_status!r}")
    if expected_order_id is not None and str(data.get("order_id") or "") != expected_order_id:
        raise GatewayError("SeverPay payment belongs to another order")
    provider_id = str(data["id"]) if data.get("id") is not None else None
    paid = None
    if status is PaymentStatus.SUCCEEDED:
        if str(data.get("currency") or "").upper() != "RUB":
            raise GatewayError("SeverPay payment in an unexpected currency")
        try:
            paid = amount_to_kop(Decimal(str(data.get("amount"))))
        except (ValueError, ArithmeticError):
            paid = None
    return ProviderStatus(status, paid, provider_id)


class SeverPayGateway(HttpGateway):
    key = PaymentProvider.SEVERPAY.value
    title = "💳 Карта / СБП (SeverPay)"
    supports_polling = True
    min_amount_kop = 1000  # the terminal limits are in the cabinet; the provider rejects amounts outside them

    def __init__(
        self,
        merchant_id: int,
        token: str,
        *,
        email_template: str = "{tg_id}@telegram.org",
        return_url: str | None = None,
        timeout: float = 15.0,
        base_url: str | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        if not merchant_id or not token:
            raise ValueError("SeverPay needs a merchant id and a secret token")
        self._mid = int(merchant_id)
        self._token = token
        self._email_template = email_template
        self._return_url = return_url
        self._base_url = (base_url or BASE_URL).rstrip("/")

    async def _call(self, path: str, body: dict[str, Any], *, retries: int = 0) -> dict[str, Any]:
        signed = sign_request(self._token, {**body, "mid": self._mid, "salt": secrets.token_hex(8)})
        payload = json.dumps(signed, ensure_ascii=False, separators=(",", ":")).encode()
        data = await self._request_json(
            "POST",
            f"{self._base_url}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            retries=retries,
        )
        if not isinstance(data, dict) or data.get("status") is not True or not isinstance(data.get("data"), dict):
            reason = str(data.get("msg") or "") if isinstance(data, dict) else ""
            raise GatewayError(f"SeverPay {path}: {reason[:200] or 'unexpected response'}")
        return dict(data["data"])

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        body: dict[str, Any] = {
            "order_id": payment.id,
            "amount": kop_to_number(payment.amount_kop),
            "currency": "RUB",
            "client_email": self._email_template.format(tg_id=user.tg_id),
            "client_id": str(user.tg_id),
        }
        if self._return_url:
            body["url_return"] = self._return_url
        data = await self._call("/payin/create", body)
        return Invoice(
            provider_payment_id=str(require(data, "id")),
            url=str(require(data, "url")),
            provider_amount=kop_to_str(payment.amount_kop),
            provider_currency="RUB",
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if not payment.provider_payment_id:
            return ProviderStatus(PaymentStatus.PENDING)
        try:
            body: dict[str, Any] = {"id": int(payment.provider_payment_id)}
        except ValueError:
            body = {"uid": payment.provider_payment_id}
        return payment_status(await self._call("/payin/get", body, retries=1), payment.id)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        body = json_body(raw)
        if not webhook_signature_valid(self._token, body):
            raise WebhookRejected("bad SeverPay signature", 401)
        if body.get("type") != "payin":
            return WebhookResult(response_body=ACK_BODY)  # the "test" ping and other notices
        data = body.get("data")
        if not isinstance(data, dict):
            raise WebhookRejected("SeverPay webhook without payment data", 400)
        order_id, payment_id = data.get("order_id"), data.get("id")
        if not order_id and payment_id is None:
            raise WebhookRejected("SeverPay webhook without a payment reference", 400)
        return WebhookResult(
            payment_ids=[str(order_id)] if order_id else [],
            provider_ids=[str(payment_id)] if payment_id is not None else [],
            response_body=ACK_BODY,
        )
