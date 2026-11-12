"""Shared pieces of the direct blockchain gateways (TON, USDT TRC-20).

There is no intermediary: the buyer sends coins straight to the owner's wallet and the reconciler looks for the
matching incoming transfer through a public blockchain indexer. The "invoice" is a set of instructions (address,
exact amount, comment) rendered by the bot, plus a wallet link where the chain has one.

Prices are converted from rubles with a fixed rate or, optionally, a rate from CoinGecko's public
``/simple/price`` endpoint (cached for a few minutes, with the fixed rate as a fallback). An invoice is never
created with an unknown rate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from app.services.payments.base import GatewayError, HttpGateway, WebhookRejected, WebhookResult

if TYPE_CHECKING:
    from app.db.models import Payment

log = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
# Transfers are searched from a little before the payment was created: the indexer's and our clocks differ.
CLOCK_SKEW_SEC = 120
# Incoming transfers fetched for one poll are reused this long by the next polls of other payments.
TRANSFERS_CACHE_SEC = 15.0


@dataclass(frozen=True, slots=True)
class TransferInstructions:
    """What the buyer needs to pay a blockchain invoice."""

    network: str  # "TON", "TRON (TRC-20)"
    currency: str  # "TON", "USDT"
    address: str
    amount: str  # exact amount in ``currency``
    comment: str | None = None  # required transfer comment, if the chain matches by comment
    wallet_url: str | None = None  # https link that opens a wallet (usable as a Telegram button)
    deep_link: str | None = None  # chain URI scheme link, e.g. ton://transfer/...


def instructions(gateway: object, payment: Payment) -> TransferInstructions | None:
    """Transfer instructions of ``payment`` if ``gateway`` is a blockchain gateway, else None."""
    getter = getattr(gateway, "transfer_instructions", None)
    if not callable(getter) or payment.provider != getattr(gateway, "key", None):
        return None
    try:
        result = getter(payment)
    except (GatewayError, ValueError):
        return None
    return result if isinstance(result, TransferInstructions) else None


def payment_comment(payment_id: str) -> str:
    """Short transfer comment that identifies a payment: the first 12 hex digits of its UUID."""
    digits = payment_id.replace("-", "").upper()
    if len(digits) < 12:
        raise ValueError("payment id is too short")
    return digits[:12]


def ceil_units(amount: Decimal, decimals: int, step_decimals: int) -> int:
    """``amount`` in the smallest unit (10**-decimals), rounded up to a multiple of 10**-step_decimals."""
    step = 10 ** (decimals - step_decimals)
    units = int((amount * (10**decimals)).to_integral_value(rounding=ROUND_CEILING))
    return -(-units // step) * step


def units_to_str(units: int, decimals: int) -> str:
    """12340000, 6 -> "12.34"; 1000000000, 9 -> "1"."""
    text = f"{Decimal(units).scaleb(-decimals):.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def str_to_units(value: str | None, decimals: int) -> int:
    """Inverse of :func:`units_to_str`; raises GatewayError for a missing or malformed amount."""
    try:
        number = Decimal(str(value or "").strip())
    except InvalidOperation as exc:
        raise GatewayError(f"invalid stored amount {value!r}") from exc
    units = number.scaleb(decimals)
    if not number.is_finite() or number <= 0 or units != units.to_integral_value():
        raise GatewayError(f"invalid stored amount {value!r}")
    return int(units)


def rub_amount(amount_kop: int) -> Decimal:
    return Decimal(amount_kop) / 100


def positive_rate(value: object) -> Decimal | None:
    """A positive finite Decimal rate, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        rate = Decimal(str(value))
    except InvalidOperation:
        return None
    return rate if rate.is_finite() and rate > 0 else None


class TransfersCache:
    """Incoming transfers since a timestamp, reused for a short time by later polls.

    The reconciler polls every open payment of a gateway; they all read the same account history, so one
    indexer request serves all payments whose window it covers.
    """

    def __init__(self, ttl: float = TRANSFERS_CACHE_SEC) -> None:
        self._ttl = ttl
        self._since: int | None = None
        self._at = 0.0
        self._items: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def get(self, since: int, load: Callable[[int], Awaitable[list[dict[str, Any]]]]) -> list[dict[str, Any]]:
        async with self._lock:
            started = time.monotonic()
            if self._since is not None and self._since <= since and started - self._at < self._ttl:
                return self._items
            items = await load(since)
            self._since, self._at, self._items = since, started, items
            return items


class OnchainGateway(HttpGateway):
    """Base of the direct blockchain gateways: rate conversion and the common protocol parts."""

    key: str
    title: str
    supports_polling = True
    polls_by_payment_id = True
    min_amount_kop = 100
    coingecko_id: str  # CoinGecko coin id used for the automatic rate

    def __init__(
        self,
        *,
        rate_rub: float | Decimal | None = None,
        auto_rate: bool = False,
        rate_cache_sec: int = 300,
        rate_url: str = COINGECKO_URL,
        timeout: float = 15.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._fixed_rate = positive_rate(rate_rub)
        if not auto_rate and self._fixed_rate is None:
            raise ValueError(f"{self.key}: set a fixed rate or enable the automatic rate")
        self._auto_rate = auto_rate
        self._rate_cache_sec = max(0, rate_cache_sec)
        self._rate_url = rate_url
        self._cached_rate: tuple[float, Decimal] | None = None

    async def rate(self) -> Decimal:
        """RUB per coin: the automatic rate when enabled and available, else the fixed one.

        Raises GatewayError when neither is known.
        """
        if self._auto_rate:
            now = time.monotonic()
            if self._cached_rate is not None and now - self._cached_rate[0] < self._rate_cache_sec:
                return self._cached_rate[1]
            try:
                rate = await self._fetch_rate()
            except GatewayError as exc:
                log.warning("%s: exchange rate unavailable: %s", self.key, exc)
            else:
                self._cached_rate = (now, rate)
                return rate
        if self._fixed_rate is None:
            raise GatewayError(f"{self.key}: exchange rate is unknown")
        return self._fixed_rate

    async def _fetch_rate(self) -> Decimal:
        data = await self._request_json(
            "GET", self._rate_url, params={"ids": self.coingecko_id, "vs_currencies": "rub"}, retries=1
        )
        coin = data.get(self.coingecko_id) if isinstance(data, dict) else None
        rate = positive_rate(coin.get("rub")) if isinstance(coin, dict) else None
        if rate is None:
            raise GatewayError(f"no RUB rate for {self.coingecko_id} in the response")
        return rate

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        raise WebhookRejected("blockchain transfers have no webhooks", 404)
