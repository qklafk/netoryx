"""Direct USDT (TRC-20) transfers to the owner's TRON address, found through TronGrid.

TRC-20 transfers carry no comment, so a payment is identified by its amount: the invoiced amount is the
ruble price converted to USDT and rounded up to whole cents, plus a unique fraction of a cent
(0.000001 .. 0.009999 USDT). The fraction is reserved in the ``kv`` table under
``usdt_trc20:<whole cents>:<fraction>`` with the payment id as the value, so no two open payments, in
any process, share an amount. A reservation is reused only after its payment was paid, or a few days
after it expired or was canceled (a late transfer for an expired invoice must not pay a new one).
Short fractions (0.001, 0.0002, ...) are handed out first, so amounts are easy to type while only a few
invoices are open.

The reconciler reads ``GET /v1/accounts/{address}/transactions/trc20`` with ``only_to``,
``only_confirmed`` and the USDT contract filter and accepts a ``Transfer`` of exactly the invoiced amount
to our address made after the invoice was created and not yet recorded for another payment. The
transaction id becomes ``provider_payment_id``.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import CursorResult, select, update

from app.db.models import KeyValue, Payment, PaymentProvider, PaymentStatus
from app.db.session import read_session
from app.services import kv
from app.services.payments.base import GatewayError, Invoice, ProviderStatus
from app.services.payments.onchain import (
    CLOCK_SKEW_SEC,
    COINGECKO_URL,
    OnchainGateway,
    TransferInstructions,
    TransfersCache,
    ceil_units,
    rub_amount,
    str_to_units,
    units_to_str,
)
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.db.models import User
    from app.db.session import SessionFactory

log = logging.getLogger(__name__)

BASE_URL = "https://api.trongrid.io"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
DECIMALS = 6
CENT = 10_000  # 0.01 USDT in micro-USDT
PAGE_LIMIT = 200
MAX_PAGES = 10
# An expired or canceled invoice keeps its amount this long, so a late transfer cannot pay someone else's invoice.
SLOT_REUSE_AFTER = timedelta(days=3)
_OPEN = (PaymentStatus.CREATED, PaymentStatus.PENDING)
_PAID = (PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def is_tron_address(address: str) -> bool:
    """A base58check TRON address (0x41 prefix, valid checksum)."""
    text = (address or "").strip()
    if len(text) != 34 or not text.startswith("T"):
        return False
    number = 0
    for char in text:
        index = _B58.find(char)
        if index < 0:
            return False
        number = number * 58 + index
    try:
        data = number.to_bytes(25, "big")
    except OverflowError:
        return False
    checksum = hashlib.sha256(hashlib.sha256(data[:21]).digest()).digest()[:4]
    return data[0] == 0x41 and data[21:] == checksum


def slot_order() -> list[int]:
    """Fractions of a cent in micro-USDT, the shortest decimals first: 1000..9000, then 100-steps, ..."""
    order: list[int] = []
    for step in (1000, 100, 10, 1):
        order.extend(n for n in range(step, CENT, step) if step == 1000 or n % (step * 10))
    return order


SLOTS = slot_order()


def match_transfer(record: dict[str, Any], *, address: str, expected: int, since_ms: int) -> str | None:
    """The transaction id if ``record`` is a USDT transfer of exactly ``expected`` micro-USDT to ``address``."""
    token = record.get("token_info")
    if not isinstance(token, dict) or token.get("address") != USDT_CONTRACT:
        return None
    if record.get("to") != address or record.get("type") != "Transfer":
        return None
    if str(token.get("decimals", DECIMALS)) != str(DECIMALS):
        return None
    timestamp = record.get("block_timestamp")
    if not isinstance(timestamp, int) or timestamp < since_ms:
        return None
    try:
        value = int(str(record.get("value")))
    except ValueError:
        return None
    tx_id = record.get("transaction_id")
    if value != expected or not isinstance(tx_id, str) or not tx_id:
        return None
    return tx_id


class UsdtTrc20Gateway(OnchainGateway):
    key = PaymentProvider.USDT_TRC20.value
    title = "💵 USDT (TRC-20)"
    coingecko_id = "tether"

    def __init__(
        self,
        address: str,
        session_factory: SessionFactory,
        *,
        api_key: str | None = None,
        rate_rub: float | None = None,
        auto_rate: bool = False,
        rate_cache_sec: int = 300,
        timeout: float = 15.0,
        base_url: str = BASE_URL,
        rate_url: str = COINGECKO_URL,
    ) -> None:
        super().__init__(
            rate_rub=rate_rub, auto_rate=auto_rate, rate_cache_sec=rate_cache_sec, rate_url=rate_url, timeout=timeout
        )
        if not is_tron_address(address):
            raise ValueError("invalid TRON address")
        self._address = address.strip()
        self._session_factory = session_factory
        self._api_key = api_key or None
        self._base_url = base_url.rstrip("/")
        self._cache = TransfersCache()

    # --- invoice -----------------------------------------------------------------------------------
    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        rate = await self.rate()
        cents = ceil_units(rub_amount(payment.amount_kop) / rate, DECIMALS, 2) // CENT
        amount = await self._reserve(payment.id, cents)
        return Invoice(None, None, units_to_str(amount, DECIMALS), "USDT")

    async def _reserve(self, payment_id: str, cents: int) -> int:
        """Reserve a unique amount (micro-USDT) for ``payment_id``. Raises GatewayError when all are taken."""
        prefix = f"{self.key}:{cents}:"
        now = utcnow()
        async with self._session_factory.begin() as s:
            rows = await s.execute(select(KeyValue.key, KeyValue.value).where(KeyValue.key.like(f"{prefix}%")))
            held: dict[str, str] = {key: value for key, value in rows}
            holders: dict[str, Payment] = {}
            if held:
                found = await s.scalars(select(Payment).where(Payment.id.in_(set(held.values()))))
                holders = {p.id: p for p in found}
            for slot in SLOTS:
                key = f"{prefix}{slot:04d}"
                holder = held.get(key)
                if holder is None:
                    if await kv.add(s, key, payment_id):
                        return cents * CENT + slot
                    continue  # taken concurrently
                if not _slot_free(holders.get(holder), now):
                    continue
                result = await s.execute(
                    update(KeyValue)
                    .where(KeyValue.key == key, KeyValue.value == holder)
                    .values(value=payment_id)
                    .execution_options(synchronize_session=False)
                )
                if isinstance(result, CursorResult) and result.rowcount == 1:
                    return cents * CENT + slot
        raise GatewayError("no free USDT amount for this price, too many open invoices")

    def transfer_instructions(self, payment: Payment) -> TransferInstructions:
        return TransferInstructions(
            network="TRON (TRC-20)",
            currency="USDT",
            address=self._address,
            amount=units_to_str(str_to_units(payment.provider_amount, DECIMALS), DECIMALS),
        )

    # --- polling -----------------------------------------------------------------------------------
    async def fetch(self, payment: Payment) -> ProviderStatus:
        expected = str_to_units(payment.provider_amount, DECIMALS)
        since_ms = (int(payment.created_at.timestamp()) - CLOCK_SKEW_SEC) * 1000
        matches: list[str] = []
        for record in await self._cache.get(since_ms, self._load):
            tx_id = match_transfer(record, address=self._address, expected=expected, since_ms=since_ms)
            if tx_id is not None and tx_id not in matches:
                matches.append(tx_id)
        if not matches:
            return ProviderStatus(PaymentStatus.PENDING)
        async with read_session(self._session_factory) as s:
            used = set(
                await s.scalars(
                    select(Payment.provider_payment_id).where(
                        Payment.provider == self.key,
                        Payment.provider_payment_id.in_(matches),
                        Payment.id != payment.id,
                    )
                )
            )
        for tx_id in matches:
            if tx_id not in used:
                return ProviderStatus(PaymentStatus.SUCCEEDED, None, tx_id)
        return ProviderStatus(PaymentStatus.PENDING)

    async def _load(self, since_ms: int) -> list[dict[str, Any]]:
        headers = {"TRON-PRO-API-KEY": self._api_key} if self._api_key else {}
        params = {
            "only_to": "true",
            "only_confirmed": "true",
            "contract_address": USDT_CONTRACT,
            "min_timestamp": str(since_ms),
            "order_by": "block_timestamp,asc",
            "limit": str(PAGE_LIMIT),
        }
        url = f"{self._base_url}/v1/accounts/{self._address}/transactions/trc20"
        items: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            data = await self._request_json("GET", url, params=params, headers=headers, retries=2)
            if not isinstance(data, dict) or data.get("success") is False or not isinstance(data.get("data"), list):
                raise GatewayError("TronGrid returned an unexpected response")
            items.extend(r for r in data["data"] if isinstance(r, dict))
            meta = data.get("meta")
            fingerprint = meta.get("fingerprint") if isinstance(meta, dict) else None
            if not fingerprint or len(data["data"]) < PAGE_LIMIT:
                return items
            params = {**params, "fingerprint": str(fingerprint)}
        log.warning("TRON address has more than %s USDT transfers in the polling window", MAX_PAGES * PAGE_LIMIT)
        return items


def _slot_free(holder: Payment | None, now: datetime) -> bool:
    """Whether an amount reserved by ``holder`` may be given to a new invoice."""
    if holder is None:
        return True
    if holder.status in _OPEN:
        return False
    if holder.status in _PAID:
        return True
    return holder.updated_at is None or holder.updated_at < now - SLOT_REUSE_AFTER
