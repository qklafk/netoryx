"""CloudPayments gateway: hosted payment page (order link) and saved-card charges.

* Auth: HTTP Basic ``Public ID:API secret``; ``X-Request-ID`` (our payment id) makes a repeated request return
  the first answer instead of charging again (kept by CloudPayments for one hour).
* Create: ``POST /orders/create`` with ``InvoiceId`` = our payment id and ``AccountId`` = the Telegram id (a card
  token is issued only for payments with an AccountId); the user pays at ``Model.Url``.
* Status: ``POST /v2/payments/find`` by ``InvoiceId`` (the latest operation of the invoice). A declined attempt
  on the hosted page is not final - the payer may try another card - so it stays pending until the invoice expires.
* Notifications (Check / Pay / Fail) are authenticated by ``Content-HMAC`` (HMAC-SHA256 of the raw body with the
  API secret, base64) or ``X-Content-HMAC`` (the same over the URL-decoded body); the payment is always re-fetched.
  Every notification is answered with ``{"code":0}``.
* Saved cards: ``Token`` of a completed payment, charged later by ``POST /payments/tokens/charge`` as a
  merchant-initiated scheduled payment (``TrInitiatorCode=0``, ``PaymentScheduled=1``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, unquote_plus

from app.db.models import PaymentProvider, PaymentStatus
from app.services.payments.base import (
    ChargeResult,
    GatewayError,
    HttpGateway,
    Invoice,
    ProviderStatus,
    SavedMethod,
    WebhookRejected,
    WebhookResult,
    amount_to_kop,
    get_header,
    kop_to_number,
    kop_to_str,
    require,
)
from app.utils.security import const_eq

if TYPE_CHECKING:
    from app.db.models import Payment, User

BASE_URL = "https://api.cloudpayments.ru"
NOTIFICATION_ANSWER = '{"code":0}'

# Decline codes after which a saved card will not be accepted again (lost / stolen / expired / restricted card,
# unknown issuer, and the codes CloudPayments lists as unconditionally blocking further recurring charges).
INVALID_CARD_CODES = frozenset(
    {5001, 5003, 5004, 5007, 5012, 5014, 5015, 5017, 5031, 5033, 5034, 5036, 5041, 5043, 5054, 5057, 5062}
)


def body_signature(data: bytes, secret: str) -> str:
    """Base64 HMAC-SHA256 as sent in ``Content-HMAC``."""
    return base64.b64encode(hmac.new(secret.encode(), data, hashlib.sha256).digest()).decode()


def parse_fields(raw: bytes) -> dict[str, Any]:
    """Notification fields from a form-encoded (default) or JSON body; raises WebhookRejected(400)."""
    try:
        text = raw.decode()
    except UnicodeDecodeError as exc:
        raise WebhookRejected("body is not UTF-8", 400) from exc
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError as exc:
            raise WebhookRejected("malformed JSON body", 400) from exc
        if not isinstance(data, dict):
            raise WebhookRejected("JSON body is not an object", 400)
        return data
    try:
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=bool(text))
    except ValueError as exc:
        raise WebhookRejected("malformed form body", 400) from exc
    if not pairs:
        raise WebhookRejected("empty notification", 400)
    return dict(pairs)


def card_title(model: Mapping[str, Any]) -> str:
    last4 = str(model.get("CardLastFour") or "").strip()
    kind = str(model.get("CardType") or "").strip() or "Card"
    return (f"{kind} •• {last4}" if last4 else kind)[:64]


def transaction_status(model: Mapping[str, Any], *, final_decline: bool) -> ProviderStatus:
    """Map a transaction model. ``final_decline``: a declined charge is canceled, not left for another try."""
    raw = str(model.get("Status") or "")
    transaction = model.get("TransactionId")
    transaction_id = str(transaction) if transaction not in (None, "", 0) else None
    operation = str(model.get("OperationType") or "Payment")
    if raw == "Completed" and operation == "Refund":
        return ProviderStatus(PaymentStatus.REFUNDED, provider_payment_id=transaction_id)
    if raw == "Completed":
        paid: int | None = None
        if str(model.get("Currency") or "RUB") == "RUB":
            try:
                paid = amount_to_kop(model.get("Amount", ""))
            except ValueError:
                paid = None
        token = str(model.get("Token") or "").strip()
        saved = SavedMethod(token, card_title(model)) if token else None
        return ProviderStatus(PaymentStatus.SUCCEEDED, paid, transaction_id, saved)
    if raw == "Cancelled" or (raw == "Declined" and final_decline):
        return ProviderStatus(PaymentStatus.CANCELED, provider_payment_id=transaction_id)
    if raw in ("Declined", "Authorized", "AwaitingAuthentication", "Pending", "Created", ""):
        return ProviderStatus(PaymentStatus.PENDING)
    raise GatewayError(f"unknown CloudPayments status {raw!r}")


def _reason_code(model: Mapping[str, Any]) -> int | None:
    try:
        return int(model.get("ReasonCode"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class CloudPaymentsGateway(HttpGateway):
    key = PaymentProvider.CLOUDPAYMENTS.value
    title = "💳 Карта (CloudPayments)"
    supports_polling = True
    polls_by_payment_id = True  # payments/find works with our InvoiceId
    min_amount_kop = 100

    def __init__(
        self,
        public_id: str,
        api_secret: str,
        *,
        return_url: str | None = None,
        timeout: float = 15.0,
        base_url: str = BASE_URL,
    ) -> None:
        super().__init__(timeout=timeout)
        token = base64.b64encode(f"{public_id}:{api_secret}".encode()).decode()
        self._auth = {"Authorization": f"Basic {token}"}
        self._secret = api_secret
        self._return_url = return_url
        self._base_url = base_url.rstrip("/")

    async def _post(self, path: str, body: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
        headers = dict(self._auth)
        if request_id:
            headers["X-Request-ID"] = request_id
        data = await self._request_json(
            "POST", f"{self._base_url}{path}", json=body, headers=headers, retries=2, ok_statuses=(200,)
        )
        if not isinstance(data, dict):
            raise GatewayError("unexpected CloudPayments response")
        return data

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        body: dict[str, Any] = {
            "Amount": kop_to_number(payment.amount_kop),
            "Currency": "RUB",
            "Description": description[:255],
            "InvoiceId": payment.id,
            "AccountId": str(user.tg_id),
        }
        if user.receipt_email:
            body["Email"] = user.receipt_email
        if self._return_url:
            body["SuccessRedirectUrl"] = self._return_url
            body["FailRedirectUrl"] = self._return_url
        data = await self._post("/orders/create", body, request_id=f"order-{payment.id}")
        if data.get("Success") is not True:
            raise GatewayError(f"CloudPayments order rejected: {str(data.get('Message') or '')[:200]}")
        return Invoice(
            provider_payment_id=None,  # the transaction id is learned when the order is paid
            url=str(require(data, "Model", "Url")),
            provider_amount=kop_to_str(payment.amount_kop),
            provider_currency="RUB",
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        data = await self._post("/v2/payments/find", {"InvoiceId": payment.id})
        model = data.get("Model")
        if not isinstance(model, dict):
            if data.get("Success") is False:
                return ProviderStatus(PaymentStatus.PENDING)  # "Not found": nothing paid yet
            raise GatewayError("unexpected CloudPayments response: missing Model")
        return transaction_status(model, final_decline=getattr(payment, "saved_method_id", None) is not None)

    async def charge_saved(self, payment: Payment, user: User, token: str, description: str) -> ChargeResult:
        body: dict[str, Any] = {
            "Amount": kop_to_number(payment.amount_kop),
            "Currency": "RUB",
            "AccountId": str(user.tg_id),
            "Token": token,
            "InvoiceId": payment.id,
            "Description": description[:255],
            "TrInitiatorCode": 0,
            "PaymentScheduled": 1,
        }
        if user.receipt_email:
            body["Email"] = user.receipt_email
        data = await self._post("/payments/tokens/charge", body, request_id=payment.id)
        model = data.get("Model")
        if not isinstance(model, dict):
            raise GatewayError(f"CloudPayments charge rejected: {str(data.get('Message') or '')[:200]}")
        if data.get("Success") is not True and model.get("AcsUrl"):
            # 3-D Secure needs the cardholder; it cannot be completed in the background.
            return ChargeResult(ProviderStatus(PaymentStatus.CANCELED), reason="3ds_required")
        status = transaction_status(model, final_decline=True)
        code = _reason_code(model)
        if data.get("Success") is not True and status.status is not PaymentStatus.CANCELED:
            status = ProviderStatus(PaymentStatus.CANCELED, provider_payment_id=status.provider_payment_id)
        if status.status is not PaymentStatus.CANCELED:
            return ChargeResult(status)
        reason = str(model.get("Reason") or "") or (str(code) if code is not None else None)
        return ChargeResult(status, method_invalid=code in INVALID_CARD_CODES, reason=reason)

    def verify(self, raw: bytes, headers: Mapping[str, str]) -> bool:
        """True if ``Content-HMAC`` or ``X-Content-HMAC`` matches the body."""
        if not self._secret:
            return False
        signed = get_header(headers, "Content-HMAC")
        if signed and const_eq(signed.strip(), body_signature(raw, self._secret)):
            return True
        decoded_signed = get_header(headers, "X-Content-HMAC")
        if decoded_signed:
            try:
                decoded = unquote_plus(raw.decode()).encode()
            except UnicodeDecodeError:
                return False
            return const_eq(decoded_signed.strip(), body_signature(decoded, self._secret))
        return False

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if not self.verify(raw, headers):
            raise WebhookRejected("bad CloudPayments HMAC", 403)
        fields = parse_fields(raw)
        invoice = str(fields.get("InvoiceId") or "").strip()
        return WebhookResult(
            payment_ids=[invoice] if invoice else [],
            response_body=NOTIFICATION_ANSWER,
        )
