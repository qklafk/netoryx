"""Payment gateway interface and helpers shared by every provider.

A gateway only talks to its provider: it creates invoices, polls their status and verifies webhooks.
It never touches the database; persisting, crediting and idempotency live in
:mod:`app.services.payments.service`.

Money crosses this boundary as integer kopecks. Each provider has its own wire format for amounts
(``"199.00"``, ``199``, ``"199.5"``), so the conversions are kept here, with ``Decimal`` arithmetic only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import aiohttp

from app.db.models import PaymentStatus

if TYPE_CHECKING:
    from app.db.models import Payment, User

log = logging.getLogger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


# ---------------------------------------------------------------------------------------------
# Errors and value objects
# ---------------------------------------------------------------------------------------------
class GatewayError(Exception):
    """The provider could not be reached or rejected the request."""


class WebhookRejected(Exception):
    """A webhook failed verification (signature, source IP, malformed body).

    ``status_code`` is what the HTTP layer should answer with.
    """

    def __init__(self, reason: str, status_code: int = 403) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class Invoice:
    """What the provider returned for a newly created payment."""

    provider_payment_id: str | None
    url: str | None  # where to send the user; None for Telegram Stars (the bot sends an invoice)
    provider_amount: str | None  # amount in the provider's unit, informational ("199.00", "150" Stars)
    provider_currency: str | None


@dataclass(frozen=True, slots=True)
class SavedMethod:
    """A reusable payment method the provider reported for a paid payment (card token, never a card number)."""

    token: str
    title: str  # what the user sees, e.g. "Visa •• 4242"

    def __repr__(self) -> str:  # keep the token out of logs and tracebacks
        return f"SavedMethod(title={self.title!r})"


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """Status of a payment as reported by the provider."""

    status: PaymentStatus
    paid_amount_kop: int | None = None  # what the user actually paid, if the provider reports it
    provider_payment_id: str | None = None
    # Set by providers that can charge a card again later, from a verified status only.
    saved_method: SavedMethod | None = None


@dataclass(frozen=True, slots=True)
class ChargeResult:
    """Outcome of charging a saved payment method.

    ``method_invalid`` means the provider will never accept this method again (revoked, expired, lost card).
    """

    status: ProviderStatus
    method_invalid: bool = False
    reason: str | None = None


@runtime_checkable
class RecurringGateway(Protocol):
    """A gateway that can save a card on a first payment and charge it later without the user."""

    key: str
    min_amount_kop: int

    async def charge_saved(self, payment: Payment, user: User, token: str, description: str) -> ChargeResult:
        """Charge ``payment.amount_kop`` from a saved method. Raises GatewayError when the outcome is unknown."""
        ...


@dataclass(frozen=True, slots=True)
class WebhookResult:
    """A verified webhook.

    ``payment_ids`` are our ``Payment.id`` values found in the body, ``provider_ids`` the provider's ids.
    ``trusted_status`` is set only when the provider cannot be polled and the body itself is
    authenticated (signed), e.g. YooMoney or FreeKassa; otherwise the status must be fetched.
    """

    payment_ids: list[str] = field(default_factory=list)
    provider_ids: list[str] = field(default_factory=list)
    response_body: str = "OK"
    status_code: int = 200
    trusted_status: ProviderStatus | None = None


@runtime_checkable
class PaymentGateway(Protocol):
    """A payment provider.

    Optional attribute ``polls_by_payment_id: bool`` (default False): ``fetch`` works from our
    ``Payment.id`` alone, so the reconciler also polls payments without a ``provider_payment_id``.

    Optional attribute ``repeat_payments: str`` for invoice links that can be paid more than once: when
    a ``trusted_status`` reports a further transfer (another provider id) for an already credited
    payment, ``"credit"`` records and credits it as a new top-up, ``"alert"`` only reports it
    (``payment.repeat_unrecorded`` event); absent, it is ignored as a redelivery.

    Optional attribute ``webhook_query_fields: bool`` (default False): the provider may deliver the webhook
    by GET; ``handle_webhook`` then passes the request's query string as ``raw`` when the body is empty.

    A ``trusted_status`` of PENDING with a ``provider_payment_id`` reports a transfer the provider received
    but holds (YooMoney ``unaccepted``): it is recorded for an admin and not credited.
    """

    key: str  # PaymentProvider value, stored in Payment.provider
    title: str  # RU button label, e.g. "💳 Карта / СБП (Platega)"
    supports_polling: bool
    min_amount_kop: int

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        """Create the invoice at the provider. Raises GatewayError."""
        ...

    async def fetch(self, payment: Payment) -> ProviderStatus:
        """Fetch the current status from the provider. Raises GatewayError."""
        ...

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        """Verify and parse a webhook. Raises WebhookRejected."""
        ...

    async def close(self) -> None:
        """Release network resources."""
        ...


# ---------------------------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------------------------
_KOP = Decimal("0.01")


def kop_to_decimal(kop: int) -> Decimal:
    """150050 -> Decimal("1500.50")."""
    return (Decimal(kop) / 100).quantize(_KOP)


def kop_to_str(kop: int) -> str:
    """Two decimals, as YooKassa / FreeKassa / YooMoney expect: 19900 -> "199.00"."""
    return f"{kop_to_decimal(kop):.2f}"


def kop_to_plain(kop: int) -> str:
    """Shortest exact decimal string: 19900 -> "199", 19950 -> "199.5"."""
    text = f"{kop_to_decimal(kop):.2f}".rstrip("0").rstrip(".")
    return text or "0"


def kop_to_number(kop: int) -> int | float:
    """A JSON number in rubles: 19900 -> 199, 19950 -> 199.5 (exact for two decimals)."""
    return kop // 100 if kop % 100 == 0 else float(kop_to_decimal(kop))


def amount_to_kop(value: str | int | float | Decimal) -> int:
    """Parse a provider amount in rubles into kopecks, rounding fractions of a kopeck down.

    Raises ValueError for anything that is not a finite, non-negative number.
    """
    if isinstance(value, bool):
        raise ValueError("boolean is not an amount")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount: {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"invalid amount: {value!r}")
    return int((number * 100).to_integral_value(rounding=ROUND_DOWN))


# ---------------------------------------------------------------------------------------------
# Headers and source IPs
# ---------------------------------------------------------------------------------------------
def get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup that works for plain dicts and multidicts."""
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, val in headers.items():
        if key.lower() == lowered:
            return val
    return None


def parse_ip(value: str | None) -> IPAddress | None:
    """Parse an IP, accepting "1.2.3.4:80", "[::1]:80" and IPv4-mapped IPv6; None if invalid."""
    if not value:
        return None
    text = value.strip()
    if text.startswith("["):
        text = text[1 : text.find("]")] if "]" in text else text[1:]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def parse_networks(items: Iterable[str]) -> list[IPNetwork]:
    """Parse IPs/CIDRs, skipping (and logging) invalid entries."""
    networks: list[IPNetwork] = []
    for item in items:
        try:
            networks.append(ipaddress.ip_network(item.strip(), strict=False))
        except ValueError:
            log.warning("Ignoring invalid IP network %r", item)
    return networks


def ip_in(ip: IPAddress | None, networks: Iterable[IPNetwork]) -> bool:
    """True if ``ip`` belongs to any of ``networks`` (address families are matched safely)."""
    return ip is not None and any(ip.version == net.version and ip in net for net in networks)


def client_ip(
    remote_ip: str | None, headers: Mapping[str, str], trusted_proxies: Iterable[IPNetwork]
) -> IPAddress | None:
    """Resolve the real client IP.

    ``X-Forwarded-For`` and ``X-Real-IP`` are honoured only when the direct peer is a trusted proxy.
    The XFF chain is walked right to left and the first hop that is not a trusted proxy is the client,
    so a client cannot spoof its address by prepending entries.

    Proxies differ in which header they set and pass the other one through from the client unchanged
    (nginx with only ``X-Real-IP $remote_addr`` forwards a client-sent XFF; Caddy forwards a client-sent
    X-Real-IP). So when both headers name a client and they disagree, one of them is forged and the
    address is unresolvable (None) rather than trusting either. An ``X-Real-IP`` that names a trusted
    proxy (set by an inner proxy in a chain) carries no client information and is ignored.
    """
    proxies = list(trusted_proxies)
    peer = parse_ip(remote_ip)
    if peer is None or not ip_in(peer, proxies):
        return peer
    try:
        from_xff = _forwarded_for(get_header(headers, "X-Forwarded-For"), proxies)
        from_real_ip = _real_ip(get_header(headers, "X-Real-IP"), proxies)
    except ValueError:
        return None
    if from_xff is not None and from_real_ip is not None and from_xff != from_real_ip:
        return None
    return from_xff or from_real_ip or peer


def _forwarded_for(header: str | None, proxies: list[IPNetwork]) -> IPAddress | None:
    """The client named by an X-Forwarded-For chain (None if absent). Raises ValueError if malformed."""
    hops = [hop.strip() for hop in (header or "").split(",") if hop.strip()]
    if not hops:
        return None
    for hop in reversed(hops):
        ip = parse_ip(hop)
        if ip is None:
            raise ValueError(f"bad X-Forwarded-For hop {hop!r}")
        if not ip_in(ip, proxies):
            return ip
    return parse_ip(hops[0])


def _real_ip(header: str | None, proxies: list[IPNetwork]) -> IPAddress | None:
    """The client named by X-Real-IP; None if absent or a trusted proxy. Raises ValueError if malformed."""
    text = (header or "").strip()
    if not text:
        return None
    ip = parse_ip(text)
    if ip is None:
        raise ValueError(f"bad X-Real-IP {text!r}")
    return None if ip_in(ip, proxies) else ip


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------
RETRY_STATUSES = frozenset({500, 502, 503, 504})


class HttpGateway:
    """Base class owning one lazily created ``aiohttp.ClientSession``.

    The session is created on first use so gateways can be constructed outside an event loop.
    """

    def __init__(self, *, timeout: float = 15.0, retry_delay: float = 0.5) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._retry_delay = retry_delay
        self._http: aiohttp.ClientSession | None = None

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=self._timeout)
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        retries: int = 0,
        ok_statuses: Iterable[int] = (200, 201),
        **kwargs: Any,
    ) -> Any:
        """Send a request and decode a JSON response.

        Connection errors, timeouts and 5xx are retried ``retries`` times with exponential backoff;
        pass ``retries > 0`` only for idempotent requests. Raises GatewayError.
        """
        ok = set(ok_statuses)
        attempt = 0
        while True:
            try:
                return await self._request_once(method, url, ok, **kwargs)
            except _Retryable as exc:
                if attempt >= retries:
                    raise GatewayError(str(exc)) from exc.__cause__
                await asyncio.sleep(self._retry_delay * 2**attempt)
                attempt += 1

    async def _request_once(self, method: str, url: str, ok: set[int], **kwargs: Any) -> Any:
        try:
            async with self._session().request(method, url, **kwargs) as resp:
                # errors="replace": an error page in another encoding without a charset (a WAF / proxy page)
                # must become a GatewayError below, not a stray UnicodeDecodeError.
                text = await resp.text(errors="replace")
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise _Retryable(f"{method} {_path(url)}: {type(exc).__name__}") from exc
        if status in RETRY_STATUSES:
            raise _Retryable(f"{method} {_path(url)}: HTTP {status}")
        if status not in ok:
            raise GatewayError(f"{method} {_path(url)}: HTTP {status}: {text[:200]}")
        try:
            return json.loads(text) if text else None
        except ValueError as exc:
            raise GatewayError(f"{method} {_path(url)}: invalid JSON: {text[:200]}") from exc


class _Retryable(Exception):
    """Internal: a transient failure that may be retried."""


def _path(url: str) -> str:
    """URL without the query string (which may carry credentials) for error messages."""
    return url.split("?", 1)[0]


def require(data: Any, *keys: str) -> Any:
    """Walk nested dicts; raise GatewayError when a key is missing or the shape is wrong."""
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise GatewayError(f"unexpected provider response: missing {'.'.join(keys)}")
        current = current[key]
    return current


def json_body(raw: bytes) -> dict[str, Any]:
    """Decode a JSON object webhook body or raise WebhookRejected(400)."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebhookRejected("malformed JSON body", 400) from exc
    if not isinstance(data, dict):
        raise WebhookRejected("JSON body is not an object", 400)
    return data
