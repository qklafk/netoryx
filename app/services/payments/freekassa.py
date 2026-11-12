"""FreeKassa gateway (SCI payment links + notifications, optional API v1 polling).

* Create: ``https://pay.fk.money/?m=&oa=&currency=RUB&o=&s=`` with ``s = md5("m:oa:secret1:currency:o")``;
  no API call.
* Notifications: form fields ``MERCHANT_ID, AMOUNT, intid, MERCHANT_ORDER_ID, ... SIGN`` with
  ``SIGN = md5("MERCHANT_ID:AMOUNT:secret2:MERCHANT_ORDER_ID")``, sent from a fixed set of IPs; the
  answer must be ``YES``. The method (GET or POST) is a shop setting in the FreeKassa cabinet: a GET
  carries the fields in the query string (``webhook_query_fields``), a POST as a urlencoded or
  ``multipart/form-data`` body.
* Polling (only when an API key is configured): ``POST https://api.fk.life/v1/orders`` signed with
  ``HMAC-SHA256(api_key, "|".join(values sorted by key))``; status 1 = paid, 6 = refund, 8/9 = failed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from collections.abc import Iterable, Mapping
from email import policy
from email.parser import BytesParser
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode

from app.db.models import PaymentProvider, PaymentStatus
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
    kop_to_str,
    parse_networks,
    require,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

SCI_URL = "https://pay.fk.money/"
API_URL = "https://api.fk.life/v1"
NOTIFICATION_IPS: tuple[str, ...] = ("168.119.157.136", "168.119.60.227", "178.154.197.79", "51.250.54.238")

_ORDER_STATUSES = {
    0: PaymentStatus.PENDING,
    1: PaymentStatus.SUCCEEDED,
    6: PaymentStatus.REFUNDED,
    8: PaymentStatus.CANCELED,
    9: PaymentStatus.CANCELED,
}


def _md5(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


def form_sign(shop_id: str, amount: str, secret1: str, currency: str, order_id: str) -> str:
    """Signature ``s`` of an SCI payment link."""
    return _md5(f"{shop_id}:{amount}:{secret1}:{currency}:{order_id}")


def notification_sign(shop_id: str, amount: str, secret2: str, order_id: str) -> str:
    """Expected ``SIGN`` of a payment notification."""
    return _md5(f"{shop_id}:{amount}:{secret2}:{order_id}")


def api_signature(data: Mapping[str, Any], api_key: str) -> str:
    """API v1 request signature: HMAC-SHA256 over the values sorted by key, joined with "|"."""
    message = "|".join(str(data[k]) for k in sorted(data) if k != "signature")
    return hmac.new(api_key.encode(), message.encode(), hashlib.sha256).hexdigest()


class FreeKassaGateway(HttpGateway):
    key = PaymentProvider.FREEKASSA.value
    title = "💳 Карта / СБП (FreeKassa)"
    min_amount_kop = 100

    def __init__(
        self,
        shop_id: str,
        secret1: str,
        secret2: str,
        *,
        api_key: str | None = None,
        check_ip: bool = True,
        trusted_proxies: Iterable[str] = (),
        timeout: float = 15.0,
        sci_url: str = SCI_URL,
        api_url: str = API_URL,
    ) -> None:
        super().__init__(timeout=timeout)
        self._shop_id = shop_id
        self._secret1 = secret1
        self._secret2 = secret2
        self._api_key = api_key
        self._check_ip = check_ip
        self._allowed: list[IPNetwork] = parse_networks(NOTIFICATION_IPS)
        self._proxies: list[IPNetwork] = parse_networks(trusted_proxies)
        self._sci_url = sci_url
        self._api_url = api_url.rstrip("/")
        # API v1 rejects a nonce that is not greater than the last one it received: nonces are strictly
        # increasing per process and requests are sent one at a time, in nonce order.
        self._last_nonce = 0
        self._api_lock = asyncio.Lock()
        self.supports_polling = bool(api_key)
        # The SCI link creates no provider id; fetch() looks the order up by our Payment.id.
        self.polls_by_payment_id = True
        # The SCI link can be paid again. A further notification (new intid) for a credited order is only
        # reported: the order id stored by polling (fk_order_id) is not known to equal intid, so crediting
        # it could credit the same transfer twice.
        self.repeat_payments = "alert"
        # The cabinet may be set to deliver notifications by GET: the fields are then in the query string.
        self.webhook_query_fields = True

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        amount = kop_to_str(payment.amount_kop)
        params = {
            "m": self._shop_id,
            "oa": amount,
            "currency": "RUB",
            "o": payment.id,
            "s": form_sign(self._shop_id, amount, self._secret1, "RUB", payment.id),
            "lang": "ru",
        }
        return Invoice(None, f"{self._sci_url}?{urlencode(params)}", amount, "RUB")

    async def fetch(self, payment: Payment) -> ProviderStatus:
        if not self._api_key:
            raise GatewayError("FreeKassa polling needs FREEKASSA_API_KEY")
        async with self._api_lock:  # concurrent polls would reach FreeKassa out of nonce order
            data: dict[str, Any] = {
                "shopId": int(self._shop_id) if self._shop_id.isdigit() else self._shop_id,
                "nonce": self._next_nonce(),
                "paymentId": payment.id,
            }
            data["signature"] = api_signature(data, self._api_key)
            result = await self._request_json("POST", f"{self._api_url}/orders", json=data)
        orders = require(result, "orders")
        order = next((o for o in orders if isinstance(o, dict) and str(o.get("merchant_order_id")) == payment.id), None)
        if order is None:
            return ProviderStatus(PaymentStatus.PENDING)  # not paid yet: FreeKassa has no order
        return order_status(order)

    def _next_nonce(self) -> int:
        """The current time in ms, but always greater than the previous nonce (same ms, clock steps back)."""
        self._last_nonce = max(self._last_nonce + 1, time.time_ns() // 1_000_000)
        return self._last_nonce

    def is_allowed_source(self, remote_ip: str | None, headers: Mapping[str, str]) -> bool:
        """True if the request comes from a FreeKassa IP (through trusted proxies only)."""
        return ip_in(client_ip(remote_ip, headers, self._proxies), self._allowed)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if self._check_ip and not self.is_allowed_source(remote_ip, headers):
            raise WebhookRejected("source IP is not a FreeKassa address", 403)
        fields = form_fields(raw, headers)
        shop_id = fields.get("MERCHANT_ID", "")
        amount = fields.get("AMOUNT", "")
        order_id = fields.get("MERCHANT_ORDER_ID", "")
        expected = notification_sign(shop_id, amount, self._secret2, order_id)
        given = fields.get("SIGN", "").strip().lower().encode("utf-8", "replace")  # bytes: may be non-ASCII
        if not hmac.compare_digest(expected.encode(), given):
            raise WebhookRejected("bad FreeKassa signature", 401)
        if shop_id != self._shop_id or not order_id:
            raise WebhookRejected("notification for another shop", 400)
        try:
            paid = amount_to_kop(amount)
        except ValueError as exc:
            raise WebhookRejected("bad AMOUNT", 400) from exc
        intid = fields.get("intid") or None
        return WebhookResult(
            payment_ids=[order_id],
            provider_ids=[intid] if intid else [],
            response_body="YES",
            trusted_status=ProviderStatus(PaymentStatus.SUCCEEDED, paid, intid),
        )


def form_fields(raw: bytes, headers: Mapping[str, str]) -> dict[str, str]:
    """Fields of a form body: ``multipart/form-data`` by its Content-Type, else urlencoded (or a query string).

    Raises WebhookRejected(400) for a body that cannot be decoded.
    """
    content_type = get_header(headers, "Content-Type") or ""
    try:
        if content_type.split(";", 1)[0].strip().lower() == "multipart/form-data":
            return _multipart_fields(raw, content_type)
        return dict(parse_qsl(raw.decode(), keep_blank_values=True))
    except (UnicodeDecodeError, LookupError, ValueError) as exc:
        raise WebhookRejected("malformed form body", 400) from exc


def _multipart_fields(raw: bytes, content_type: str) -> dict[str, str]:
    """Plain (non-file) fields of a ``multipart/form-data`` body; ValueError if it is not one."""
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1")
    message = BytesParser(policy=policy.HTTP).parsebytes(head + raw)
    if not message.is_multipart() or not message.get_boundary() or message.defects:
        raise ValueError("not a multipart/form-data body")
    fields: dict[str, str] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str) or part.get_filename() is not None:
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise ValueError("bad multipart part")
        fields[name] = payload.decode(part.get_content_charset() or "utf-8")
    return fields


def order_status(order: Mapping[str, Any]) -> ProviderStatus:
    """Map an API v1 order to a ProviderStatus."""
    try:
        code = int(order.get("status", -1))
    except (TypeError, ValueError):
        code = -1
    status = _ORDER_STATUSES.get(code)
    if status is None:
        raise GatewayError(f"unknown FreeKassa order status {order.get('status')!r}")
    paid: int | None = None
    if status is PaymentStatus.SUCCEEDED and str(order.get("currency", "RUB")).upper() == "RUB":
        try:
            paid = amount_to_kop(order.get("amount", ""))
        except ValueError:
            paid = None
    fk_id = order.get("fk_order_id")
    return ProviderStatus(status, paid, str(fk_id) if fk_id is not None else None)
