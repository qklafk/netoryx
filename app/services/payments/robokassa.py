"""Robokassa hosted payment form and ResultURL notifications.

The payment link is signed with Password #1.  A ResultURL delivery is trusted
only after its Password #2 signature (and, by default, its source address) is
verified.  Robokassa does not offer an order-status endpoint for this flow, so
the verified delivery carries the terminal payment status.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qsl, urlencode

from app.db.models import PaymentProvider, PaymentStatus
from app.services.payments.base import (
    GatewayError,
    Invoice,
    IPNetwork,
    ProviderStatus,
    WebhookRejected,
    WebhookResult,
    amount_to_kop,
    client_ip,
    ip_in,
    kop_to_str,
    parse_networks,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

PAYMENT_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"
NOTIFICATION_IPS: tuple[str, ...] = ("185.59.216.65", "185.59.217.65")
HashAlgorithm = Literal["md5", "sha256"]
_PAYMENT_KEY = "Shp_payment_id"


def _hash(value: str, algorithm: HashAlgorithm) -> str:
    data = value.encode()
    if algorithm == "md5":
        return hashlib.md5(data, usedforsecurity=False).hexdigest()
    return hashlib.sha256(data).hexdigest()


def _custom_parts(fields: Mapping[str, str]) -> list[str]:
    """Robokassa custom fields in the documented case-insensitive name order."""
    keys = sorted((key for key in fields if key.lower().startswith("shp_")), key=str.lower)
    return [f"{key}={fields[key]}" for key in keys]


def payment_signature(
    merchant_login: str,
    amount: str,
    invoice_id: str,
    password1: str,
    custom: Mapping[str, str],
    algorithm: HashAlgorithm,
) -> str:
    return _hash(":".join([merchant_login, amount, invoice_id, password1, *_custom_parts(custom)]), algorithm)


def result_signature(
    amount: str, invoice_id: str, password2: str, custom: Mapping[str, str], algorithm: HashAlgorithm
) -> str:
    return _hash(":".join([amount, invoice_id, password2, *_custom_parts(custom)]), algorithm)


def _invoice_id(payment_id: str) -> str:
    """A compact numeric ID accepted by Robokassa, while ``Shp_payment_id`` remains authoritative."""
    try:
        return str(int(payment_id.replace("-", "")[:15], 16))
    except ValueError as exc:
        raise GatewayError("payment id cannot be represented as a Robokassa invoice id") from exc


def _form_fields(raw: bytes) -> dict[str, str]:
    try:
        pairs = parse_qsl(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=bool(raw))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WebhookRejected("malformed Robokassa form body", 400) from exc
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            raise WebhookRejected("duplicate Robokassa form field", 400)
        fields[key] = value
    return fields


class RobokassaGateway:
    key = PaymentProvider.ROBOKASSA.value
    title = "💳 Карта / СБП (Робокасса)"
    supports_polling = False
    min_amount_kop = 100
    # The ResultURL method can be configured as GET in the Robokassa cabinet.
    webhook_query_fields = True

    def __init__(
        self,
        merchant_login: str,
        password1: str,
        password2: str,
        *,
        hash_algorithm: HashAlgorithm = "md5",
        test_mode: bool = False,
        check_ip: bool = True,
        trusted_proxies: Iterable[str] = (),
        payment_url: str = PAYMENT_URL,
    ) -> None:
        if not merchant_login or not password1 or not password2:
            raise ValueError("Robokassa needs a merchant login and both passwords")
        self._merchant_login = merchant_login
        self._password1 = password1
        self._password2 = password2
        self._algorithm = hash_algorithm
        self._test_mode = test_mode
        self._check_ip = check_ip
        self._allowed: list[IPNetwork] = parse_networks(NOTIFICATION_IPS)
        self._proxies: list[IPNetwork] = parse_networks(trusted_proxies)
        self._payment_url = payment_url

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        amount = kop_to_str(payment.amount_kop)
        invoice_id = _invoice_id(payment.id)
        custom = {_PAYMENT_KEY: payment.id}
        params = {
            "MerchantLogin": self._merchant_login,
            "OutSum": amount,
            "InvId": invoice_id,
            "Description": description[:100] or "VPN",
            **custom,
            "SignatureValue": payment_signature(
                self._merchant_login, amount, invoice_id, self._password1, custom, self._algorithm
            ),
        }
        if self._test_mode:
            params["IsTest"] = "1"
        return Invoice(invoice_id, f"{self._payment_url}?{urlencode(params)}", amount, "RUB")

    async def fetch(self, payment: Payment) -> ProviderStatus:
        raise GatewayError("Robokassa payments are confirmed by ResultURL")

    def is_allowed_source(self, remote_ip: str | None, headers: Mapping[str, str]) -> bool:
        return ip_in(client_ip(remote_ip, headers, self._proxies), self._allowed)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if self._check_ip and not self.is_allowed_source(remote_ip, headers):
            raise WebhookRejected("source IP is not a Robokassa address", 403)
        fields = _form_fields(raw)
        amount = fields.get("OutSum", "")
        invoice_id = fields.get("InvId", "")
        payment_id = fields.get(_PAYMENT_KEY, "")
        given = fields.get("SignatureValue", "").strip().lower().encode("utf-8", "replace")
        expected = result_signature(amount, invoice_id, self._password2, fields, self._algorithm).encode()
        if not amount or not invoice_id or not payment_id or not hmac.compare_digest(expected, given):
            raise WebhookRejected("bad Robokassa notification signature", 401)
        try:
            paid = amount_to_kop(amount)
        except ValueError as exc:
            raise WebhookRejected("bad Robokassa OutSum", 400) from exc
        return WebhookResult(
            payment_ids=[payment_id],
            provider_ids=[invoice_id],
            response_body=f"OK{invoice_id}",
            trusted_status=ProviderStatus(PaymentStatus.SUCCEEDED, paid, invoice_id),
        )

    async def close(self) -> None:
        return None
