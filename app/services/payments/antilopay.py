"""Antilopay acquiring: cards, SBP and SberPay through a hosted payment form.

* Requests: compact JSON bodies signed with SHA256withRSA by the merchant's secret (private) key;
  headers ``X-Apay-Secret-Id``, ``X-Apay-Sign`` (Base64) and ``X-Apay-Sign-Version: 1``.
* Create: ``POST payment/create`` with ``order_id`` = our payment id; answers ``payment_id`` + ``payment_url``.
* Status: ``POST payment/check`` by ``order_id`` (``PENDING`` / ``SUCCESS`` / ``FAIL`` / ``CANCEL`` /
  ``EXPIRED`` / ``CHARGEBACK`` / ``REVERSED``); ``original_amount`` is what the buyer paid.
* Callbacks: SHA256withRSA over the raw body with the project's callback public key, Base64 in
  ``X-Apay-Callback``; sent from fixed addresses. The status is re-fetched before crediting.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal

from cryptography.hazmat.primitives import hashes

from app.db.models import PaymentProvider, PaymentStatus
from app.services.payments import rsa_keys
from app.services.payments.base import (
    GatewayError,
    HttpGateway,
    Invoice,
    IPNetwork,
    ProviderStatus,
    WebhookRejected,
    WebhookResult,
    amount_to_kop,
    client_ip,
    get_header,
    ip_in,
    json_body,
    kop_to_number,
    kop_to_str,
    parse_networks,
    require,
)
from app.services.payments.platega import user_email

if TYPE_CHECKING:
    from app.db.models import Payment, User

BASE_URL = "https://lk.antilopay.com/api/v1"
SIGN_VERSION = "1"
CALLBACK_IPS = ("81.177.221.226", "87.228.9.243")
NOT_FOUND = 8

_STATUSES = {
    "PENDING": PaymentStatus.PENDING,
    "SUCCESS": PaymentStatus.SUCCEEDED,
    "FAIL": PaymentStatus.CANCELED,
    "CANCEL": PaymentStatus.CANCELED,
    "EXPIRED": PaymentStatus.EXPIRED,
    "CHARGEBACK": PaymentStatus.REFUNDED,
    "REVERSED": PaymentStatus.REFUNDED,
}


def canonical(body: Mapping[str, Any]) -> bytes:
    """The exact bytes that are signed and sent: compact JSON without spaces."""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def payment_status(data: Mapping[str, Any]) -> ProviderStatus:
    """Map a ``payment/check`` answer."""
    state = _STATUSES.get(str(data.get("status") or "").upper(), PaymentStatus.PENDING)
    paid = None
    if state is PaymentStatus.SUCCEEDED:
        if str(data.get("currency") or "RUB").upper() != "RUB":
            raise GatewayError(f"Antilopay payment in unexpected currency {data.get('currency')!r}")
        with suppress(ValueError):
            paid = amount_to_kop(data["original_amount"] if data.get("original_amount") is not None else "")
    identifier = data.get("payment_id")
    return ProviderStatus(state, paid, str(identifier) if identifier else None)


class AntilopayGateway(HttpGateway):
    key = PaymentProvider.ANTILOPAY.value
    title = "💳 Карта / СБП / SberPay (Antilopay)"
    supports_polling = True
    polls_by_payment_id = True
    min_amount_kop = 100

    def __init__(
        self,
        project_id: str,
        secret_id: str,
        secret_key: str,
        callback_key: str,
        *,
        product_type: Literal["goods", "services"] = "services",
        vat: int | None = None,
        prefer_methods: Iterable[str] = (),
        customer_email: str | None = None,
        return_url: str | None = None,
        check_ip: bool = True,
        trusted_proxies: Iterable[str] = (),
        timeout: float = 15.0,
        base_url: str = BASE_URL,
    ) -> None:
        super().__init__(timeout=timeout)
        if not (project_id and secret_id and secret_key and callback_key):
            raise ValueError("Antilopay needs a project id, secret id, secret key and callback public key")
        self._project_id, self._secret_id = project_id, secret_id
        self._private_key = rsa_keys.load_private_key(secret_key)
        self._callback_key = rsa_keys.load_public_key(callback_key)
        self._product_type, self._vat = product_type, vat
        self._prefer_methods = [m.strip().upper() for m in prefer_methods if m.strip()]
        self._customer_email = customer_email
        self._return_url = return_url
        self._check_ip = check_ip
        self._allowed: list[IPNetwork] = parse_networks(CALLBACK_IPS)
        self._proxies: list[IPNetwork] = parse_networks(trusted_proxies)
        self._base_url = base_url.rstrip("/")

    async def _post(self, path: str, body: dict[str, Any], *, retries: int = 0) -> dict[str, Any]:
        data = canonical(body)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Apay-Secret-Id": self._secret_id,
            "X-Apay-Sign": rsa_keys.sign(self._private_key, data, hashes.SHA256()),
            "X-Apay-Sign-Version": SIGN_VERSION,
        }
        result = await self._request_json(
            "POST", f"{self._base_url}/{path}", data=data, headers=headers, retries=retries
        )
        if not isinstance(result, dict) or "code" not in result:
            raise GatewayError(f"Antilopay {path}: unexpected response")
        return result

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        text = description[:255] or "VPN"
        body: dict[str, Any] = {
            "project_identificator": self._project_id,
            "amount": kop_to_number(payment.amount_kop),
            "order_id": payment.id,
            "currency": "RUB",
            "product_name": text,
            "product_type": self._product_type,
            "description": text,
            "customer": {"email": self._customer_email or user.receipt_email or user_email(user.tg_id)},
        }
        if self._vat is not None:
            body["vat"] = self._vat
        if self._return_url:
            body["success_url"] = self._return_url
            body["fail_url"] = self._return_url
        if self._prefer_methods:
            body["prefer_methods"] = self._prefer_methods
        # Not retried: the provider asks to check for an existing payment before repeating a create.
        result = await self._post("payment/create", body)
        if result.get("code") != 0:
            raise GatewayError(f"Antilopay payment/create failed: code {result.get('code')}: {result.get('error')}")
        return Invoice(
            provider_payment_id=str(require(result, "payment_id")),
            url=str(require(result, "payment_url")),
            provider_amount=kop_to_str(payment.amount_kop),
            provider_currency="RUB",
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        result = await self._post(
            "payment/check", {"project_identificator": self._project_id, "order_id": payment.id}, retries=2
        )
        code = result.get("code")
        if code == NOT_FOUND:
            return ProviderStatus(PaymentStatus.PENDING)  # never reached the provider; expires locally
        if code != 0:
            raise GatewayError(f"Antilopay payment/check failed: code {code}: {result.get('error')}")
        if str(result.get("order_id") or payment.id) != payment.id:
            raise GatewayError("Antilopay payment/check answered for another order")
        return payment_status(result)

    def is_allowed_source(self, remote_ip: str | None, headers: Mapping[str, str]) -> bool:
        """True if the request comes from an Antilopay callback address (through trusted proxies only)."""
        return ip_in(client_ip(remote_ip, headers, self._proxies), self._allowed)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if self._check_ip and not self.is_allowed_source(remote_ip, headers):
            raise WebhookRejected("source IP is not an Antilopay address", 403)
        version = get_header(headers, "X-Apay-Callback-Version")
        if version is not None and version.strip() != SIGN_VERSION:
            raise WebhookRejected("unsupported Antilopay signature version", 401)
        if not rsa_keys.verify(self._callback_key, raw, get_header(headers, "X-Apay-Callback"), hashes.SHA256()):
            raise WebhookRejected("bad Antilopay signature", 401)
        data = json_body(raw)
        if str(data.get("type") or "payment") != "payment":
            return WebhookResult()  # withdrawals, refunds, Steam top-ups are not ours
        order_id, payment_id = data.get("order_id"), data.get("payment_id")
        return WebhookResult(
            payment_ids=[str(order_id)] if order_id else [],
            provider_ids=[str(payment_id)] if payment_id else [],
        )
