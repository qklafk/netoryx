"""Direct Toncoin transfers to the owner's wallet, found through the toncenter indexer (API v3).

Every payment gets a unique text comment; the buyer sends at least the invoiced amount with that comment
(a ``ton://transfer`` link fills both in). The reconciler reads the wallet's incoming transactions with
``GET /api/v3/transactions`` and accepts one that

* was received by our wallet (``account`` and ``in_msg.destination``),
* carries exactly our comment (``message_content.decoded`` of type ``text_comment``),
* brings at least the invoiced nanotons and was neither bounced nor aborted,
* is finalized (``finality``) and included in a masterchain block.

The transaction hash becomes ``provider_payment_id``; ``UNIQUE(provider, provider_payment_id)`` keeps one
transfer from paying two invoices.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from app.db.models import PaymentProvider, PaymentStatus
from app.services.payments.base import GatewayError, Invoice, ProviderStatus
from app.services.payments.onchain import (
    CLOCK_SKEW_SEC,
    COINGECKO_URL,
    OnchainGateway,
    TransferInstructions,
    TransfersCache,
    ceil_units,
    payment_comment,
    rub_amount,
    str_to_units,
    units_to_str,
)

if TYPE_CHECKING:
    from app.db.models import Payment, User

log = logging.getLogger(__name__)

BASE_URL = "https://toncenter.com/api/v3"
TONKEEPER_URL = "https://app.tonkeeper.com/transfer"
DECIMALS = 9
AMOUNT_STEP_DECIMALS = 4  # invoices are rounded up to 0.0001 TON
PAGE_LIMIT = 100
MAX_PAGES = 10
_RAW_RE = re.compile(r"^(-?\d{1,3}):([0-9a-fA-F]{64})$")


# ---------------------------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------------------------
def _crc16(data: bytes) -> int:
    """CRC-16/XMODEM, the checksum of user-friendly TON addresses."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def raw_address(address: str) -> str:
    """Normalize a TON address (raw ``0:hex`` or user-friendly base64/base64url) to lower-case raw form.

    Raises ValueError for anything else, including a user-friendly address with a bad checksum.
    """
    text = (address or "").strip()
    match = _RAW_RE.match(text)
    if match:
        return f"{int(match.group(1))}:{match.group(2).lower()}"
    if len(text) != 48:
        raise ValueError("not a TON address")
    try:
        data = base64.b64decode(text.replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not a TON address") from exc
    if len(data) != 36 or int.from_bytes(data[34:], "big") != _crc16(data[:34]):
        raise ValueError("bad TON address checksum")
    workchain = int.from_bytes(data[1:2], "big", signed=True)
    return f"{workchain}:{data[2:34].hex()}"


def _same_address(value: object, raw: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return raw_address(value) == raw
    except ValueError:
        return False


# ---------------------------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------------------------
def _comment(message: dict[str, Any]) -> str | None:
    content = message.get("message_content")
    decoded = content.get("decoded") if isinstance(content, dict) else None
    if not isinstance(decoded, dict) or decoded.get("type", decoded.get("@type")) != "text_comment":
        return None
    comment = decoded.get("comment")
    return comment if isinstance(comment, str) else None


def _is_final(tx: dict[str, Any]) -> bool:
    """Included in a masterchain block and, when the indexer reports finality, finalized."""
    if not isinstance(tx.get("mc_block_seqno"), int):
        return False
    finality = tx.get("finality", "finalized")
    return finality in ("finalized", 2)


def match_transfer(
    tx: dict[str, Any], *, raw: str, comment: str, min_nanotons: int, since: int
) -> tuple[bool, bool] | None:
    """(matched, final) for a transaction that pays ``comment``; None if it does not."""
    message = tx.get("in_msg")
    description = tx.get("description")
    if not isinstance(message, dict) or not isinstance(description, dict):
        return None
    if not _same_address(tx.get("account"), raw) or not _same_address(message.get("destination"), raw):
        return None
    given = _comment(message)
    if given is None or given.strip().upper() != comment:
        return None
    if message.get("bounced") is not False or description.get("aborted") is not False:
        return None
    if not isinstance(tx.get("now"), int) or tx["now"] < since:
        return None
    try:
        value = int(str(message.get("value")))
    except ValueError:
        return None
    if value < min_nanotons:
        return None
    return True, _is_final(tx)


class TonGateway(OnchainGateway):
    key = PaymentProvider.TON.value
    title = "💎 Toncoin (TON)"
    coingecko_id = "the-open-network"

    def __init__(
        self,
        wallet_address: str,
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
        self._raw = raw_address(wallet_address)
        self._address = wallet_address.strip()
        self._api_key = api_key or None
        self._base_url = base_url.rstrip("/")
        self._cache = TransfersCache()

    # --- invoice -----------------------------------------------------------------------------------
    def _links(self, nanotons: int, comment: str) -> tuple[str, str]:
        query = f"amount={nanotons}&text={quote(comment)}"
        return f"{TONKEEPER_URL}/{self._address}?{query}", f"ton://transfer/{self._address}?{query}"

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        rate = await self.rate()
        nanotons = ceil_units(rub_amount(payment.amount_kop) / rate, DECIMALS, AMOUNT_STEP_DECIMALS)
        wallet_url, _ = self._links(nanotons, payment_comment(payment.id))
        return Invoice(None, wallet_url, units_to_str(nanotons, DECIMALS), "TON")

    def transfer_instructions(self, payment: Payment) -> TransferInstructions:
        nanotons = str_to_units(payment.provider_amount, DECIMALS)
        comment = payment_comment(payment.id)
        wallet_url, deep_link = self._links(nanotons, comment)
        return TransferInstructions(
            network="TON",
            currency="TON",
            address=self._address,
            amount=units_to_str(nanotons, DECIMALS),
            comment=comment,
            wallet_url=wallet_url,
            deep_link=deep_link,
        )

    # --- polling -----------------------------------------------------------------------------------
    async def fetch(self, payment: Payment) -> ProviderStatus:
        expected = str_to_units(payment.provider_amount, DECIMALS)
        since = int(payment.created_at.timestamp()) - CLOCK_SKEW_SEC
        comment = payment_comment(payment.id)
        found_pending = False
        for tx in await self._cache.get(since, self._load):
            result = match_transfer(tx, raw=self._raw, comment=comment, min_nanotons=expected, since=since)
            if result is None:
                continue
            if not result[1]:
                found_pending = True
                continue
            tx_hash = tx.get("hash")
            if not isinstance(tx_hash, str) or not tx_hash:
                continue
            return ProviderStatus(PaymentStatus.SUCCEEDED, None, tx_hash)
        if found_pending:
            log.info("payment %s: TON transfer seen, waiting for finality", payment.id)
        return ProviderStatus(PaymentStatus.PENDING)

    async def _load(self, since: int) -> list[dict[str, Any]]:
        headers = {"X-Api-Key": self._api_key} if self._api_key else {}
        items: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            params = {
                "account": self._address,
                "start_utime": str(since),
                "limit": str(PAGE_LIMIT),
                "offset": str(page * PAGE_LIMIT),
                "sort": "desc",
            }
            data = await self._request_json(
                "GET", f"{self._base_url}/transactions", params=params, headers=headers, retries=2
            )
            batch = data.get("transactions") if isinstance(data, dict) else None
            if not isinstance(batch, list):
                raise GatewayError("toncenter returned no transactions list")
            items.extend(tx for tx in batch if isinstance(tx, dict))
            if len(batch) < PAGE_LIMIT:
                return items
        log.warning("TON wallet has more than %s transactions in the polling window", MAX_PAGES * PAGE_LIMIT)
        return items
