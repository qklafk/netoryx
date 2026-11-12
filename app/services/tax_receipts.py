"""Receipts in "Мой налог" (self-employed tax, NPD) for credited payments.

* :func:`enqueue` runs inside the crediting transaction and adds a ``PENDING`` row for an eligible payment
  (receipts switched on, provider in ``NPD_PROVIDERS``); :func:`on_refund` runs inside the refund transaction.
* :func:`process_due` (background job) registers pending incomes and annuls refunded ones, outside of any
  transaction, with exponential backoff and a cap of ``NPD_MAX_ATTEMPTS``.

Never twice for one payment: a row exists once per payment (UNIQUE), a registered row keeps the receipt uuid,
and once an income request may have reached the service (``sent``), the next attempt first looks the income up in
the cabinet's list (same name, amount and operation time) and adopts it instead of sending again.

Tokens are kept encrypted in ``kv`` (``npd:session``); the refresh token rotates on every refresh.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, Plan, TaxReceipt, TaxReceiptStatus
from app.services import kv
from app.services.npd import NpdAuthError, NpdClient, NpdError, NpdSession, moscow_iso, new_device_id, receipt_url
from app.utils.crypto import DecryptionError
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.config import Settings
    from app.db.session import SessionFactory
    from app.utils.crypto import SecretBox

log = logging.getLogger(__name__)

SESSION_KEY = "npd:session"
DEVICE_KEY = "npd:device"
BATCH = 50
BACKOFF_BASE = timedelta(minutes=1)
BACKOFF_CAP = timedelta(hours=6)
LOOKUP_WINDOW = timedelta(days=2)
ERROR_MAX = 500

ClientFactory = Callable[[str], NpdClient]


class NpdNotConfiguredError(NpdAuthError):
    """No session, no refresh token and no INN + password."""


def _secret(value: object) -> str:
    getter = getattr(value, "get_secret_value", None)
    return str(getter()) if getter else ""


def eligible(settings: Settings, provider: str) -> bool:
    return bool(settings.npd_enabled) and provider in set(settings.npd_providers)


def _service_name(settings: Settings, plan_title: str | None) -> str:
    if plan_title:
        try:
            name = settings.npd_service_name.format(plan=plan_title)
        except (KeyError, IndexError, ValueError):
            name = settings.npd_service_name
    else:
        name = settings.npd_topup_service_name
    return (name.strip() or "Услуга")[:256]


# ---------------------------------------------------------------------------------------------------------------
# In the caller's transaction
# ---------------------------------------------------------------------------------------------------------------
async def enqueue(session: AsyncSession, settings: Settings, payment: Payment, amount_kop: int) -> TaxReceipt | None:
    """Queue a receipt for a credited payment (no-op when not eligible or already queued)."""
    if amount_kop <= 0 or not eligible(settings, payment.provider):
        return None
    plan_title = None
    if payment.plan_id is not None and not payment.is_donation:
        plan_title = await session.scalar(select(Plan.title).where(Plan.id == payment.plan_id))
    now = utcnow()
    row = TaxReceipt(
        payment_id=payment.id,
        user_id=payment.user_id,
        provider=payment.provider,
        status=TaxReceiptStatus.PENDING.value,
        amount_kop=amount_kop,
        service_name=_service_name(settings, plan_title),
        operation_time=payment.credited_at or now,
        attempts=0,
        next_attempt_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        return None
    return row


async def on_refund(session: AsyncSession, payment_id: str) -> TaxReceipt | None:
    """The payment was refunded: annul its receipt (or drop it if it was never registered)."""
    row = await session.scalar(select(TaxReceipt).where(TaxReceipt.payment_id == payment_id).with_for_update())
    if row is None:
        return None
    _cancel_row(row, utcnow())
    return row


def _cancel_row(row: TaxReceipt, now: datetime) -> None:
    if row.status == TaxReceiptStatus.REGISTERED.value:
        row.status = TaxReceiptStatus.CANCEL_PENDING.value
        row.attempts = 0
        row.next_attempt_at = now
    elif row.status in (TaxReceiptStatus.PENDING.value, TaxReceiptStatus.FAILED.value) and not row.sent:
        row.status = TaxReceiptStatus.CANCELLED.value
        row.cancelled_at = now
        row.next_attempt_at = None
    elif row.status in (TaxReceiptStatus.PENDING.value, TaxReceiptStatus.FAILED.value):
        # A request may have registered it: find it first, then annul it.
        row.status = TaxReceiptStatus.CANCEL_PENDING.value
        row.attempts = 0
        row.next_attempt_at = now
    row.updated_at = now


async def request_cancel(session: AsyncSession, receipt_id: int) -> TaxReceipt | None:
    """Admin action: annul a registered receipt or drop a pending one."""
    row = await session.get(TaxReceipt, receipt_id, with_for_update=True)
    if row is not None:
        _cancel_row(row, utcnow())
    return row


async def request_retry(session: AsyncSession, receipt_id: int) -> TaxReceipt | None:
    """Admin action: try a failed receipt (or a stuck annulment) again now."""
    row = await session.get(TaxReceipt, receipt_id, with_for_update=True)
    if row is None:
        return None
    now = utcnow()
    if row.status == TaxReceiptStatus.FAILED.value:
        row.status = TaxReceiptStatus.PENDING.value
    if row.status in (TaxReceiptStatus.PENDING.value, TaxReceiptStatus.CANCEL_PENDING.value):
        row.attempts = 0
        row.next_attempt_at = now
        row.updated_at = now
    return row


# ---------------------------------------------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------------------------------------------
async def device_id(session_factory: SessionFactory) -> str:
    async with session_factory.begin() as session:
        value = await kv.get(session, DEVICE_KEY)
        if value:
            return value
        value = new_device_id()
        if await kv.add(session, DEVICE_KEY, value):
            return value
        return await kv.get(session, DEVICE_KEY, fresh=True) or value


async def load_session(session_factory: SessionFactory, box: SecretBox) -> NpdSession | None:
    async with session_factory.begin() as session:
        raw = await kv.get(session, SESSION_KEY)
    if not raw:
        return None
    try:
        return NpdSession.loads(box.decrypt(raw))
    except (DecryptionError, ValueError, KeyError, TypeError):
        log.warning("stored Мой налог session is unreadable; logging in again")
        return None


async def save_session(session_factory: SessionFactory, box: SecretBox, npd: NpdSession | None) -> None:
    async with session_factory.begin() as session:
        if npd is None:
            await kv.delete(session, SESSION_KEY)
        else:
            await kv.set(session, SESSION_KEY, box.encrypt(npd.dumps()))


async def _login(client: NpdClient, settings: Settings) -> NpdSession:
    inn, password = (settings.npd_inn or "").strip(), _secret(settings.npd_password)
    if not inn or not password:
        raise NpdNotConfiguredError("log in to Мой налог: set the INN and password or use the SMS login")
    return await client.login_password(inn, password)


async def _fresh_session(client: NpdClient, settings: Settings, current: NpdSession | None) -> NpdSession:
    """A new session: refresh the stored one, else the configured refresh token, else INN + password."""
    inn = (settings.npd_inn or "").strip() or None
    tokens = [current.refresh_token] if current is not None else []
    configured = _secret(settings.npd_refresh_token)
    if configured and configured not in tokens:
        tokens.append(configured)
    for token in tokens:
        try:
            return await client.refresh(token, (current.inn if current else None) or inn)
        except NpdAuthError:
            continue
    return await _login(client, settings)


# ---------------------------------------------------------------------------------------------------------------
# Background processing (no transaction is open during network calls)
# ---------------------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class ProcessResult:
    registered: list[int] = field(default_factory=list)  # receipt ids
    cancelled: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)  # gave up on this run
    auth_error: str | None = None


def backoff(attempts: int) -> timedelta:
    return min(BACKOFF_BASE * (2 ** max(0, attempts - 1)), BACKOFF_CAP)


async def _due(session_factory: SessionFactory, now: datetime, limit: int) -> list[TaxReceipt]:
    async with session_factory.begin() as session:
        rows = await session.scalars(
            select(TaxReceipt)
            .where(
                TaxReceipt.status.in_((TaxReceiptStatus.PENDING.value, TaxReceiptStatus.CANCEL_PENDING.value)),
                or_(TaxReceipt.next_attempt_at.is_(None), TaxReceipt.next_attempt_at <= now),
            )
            .order_by(TaxReceipt.id)
            .limit(limit)
        )
        result = list(rows)
        session.expunge_all()
    return result


class _Auth:
    """Holds the current tokens for one run; refreshes once on a 401."""

    def __init__(self, client: NpdClient, settings: Settings, session_factory: SessionFactory, box: SecretBox) -> None:
        self.client, self.settings, self.factory, self.box = client, settings, session_factory, box
        self.session: NpdSession | None = None

    async def start(self) -> NpdSession:
        self.session = await load_session(self.factory, self.box)
        if self.session is None:
            await self.renew()
        assert self.session is not None
        return self.session

    async def renew(self) -> NpdSession:
        self.session = await _fresh_session(self.client, self.settings, self.session)
        await save_session(self.factory, self.box, self.session)
        return self.session

    @property
    def inn(self) -> str:
        return (self.session.inn if self.session else None) or (self.settings.npd_inn or "").strip()


async def process_due(
    session_factory: SessionFactory,
    settings: Settings,
    box: SecretBox,
    client_factory: ClientFactory | None = None,
    *,
    now: datetime | None = None,
    limit: int = BATCH,
) -> ProcessResult:
    """Register / annul the receipts that are due. Returns what changed (for notifications)."""
    result = ProcessResult()
    now = now or utcnow()
    rows = await _due(session_factory, now, limit)
    if not rows:
        return result
    dev = await device_id(session_factory)
    client = client_factory(dev) if client_factory is not None else NpdClient(dev)
    try:
        auth = _Auth(client, settings, session_factory, box)
        try:
            await auth.start()
        except NpdAuthError as exc:
            result.auth_error = str(exc)
            return result
        except NpdError as exc:
            log.warning("Мой налог is unreachable: %s", exc)
            return result
        for row in rows:
            try:
                await _process_one(auth, session_factory, settings, row, result)
            except NpdAuthError as exc:
                result.auth_error = str(exc)
                break
    finally:
        await client.close()
    return result


async def _with_auth[T](auth: _Auth, call: Callable[[str], Awaitable[T]]) -> T:
    assert auth.session is not None
    try:
        return await call(auth.session.token)
    except NpdAuthError:
        await auth.renew()
        assert auth.session is not None
        return await call(auth.session.token)


async def _find_existing(auth: _Auth, session_factory: SessionFactory, row: TaxReceipt) -> str | None:
    """The uuid of this receipt's income if an earlier request did register it."""
    since, until = row.operation_time - timedelta(minutes=5), max(utcnow(), row.operation_time) + timedelta(hours=1)
    since = min(since, until - LOOKUP_WINDOW)
    incomes = await _with_auth(auth, lambda t: auth.client.list_incomes(t, since, until))
    wanted_time = moscow_iso(row.operation_time)
    candidates = [
        i.receipt_uuid
        for i in incomes
        if not i.cancelled
        and i.name == row.service_name
        and i.total_kop == row.amount_kop
        and i.operation_time[:19] == wanted_time[:19]
    ]
    if not candidates:
        return None
    async with session_factory.begin() as session:
        taken = set(
            await session.scalars(select(TaxReceipt.receipt_uuid).where(TaxReceipt.receipt_uuid.in_(candidates)))
        )
    free = [c for c in candidates if c not in taken]
    return free[0] if free else None


async def _process_one(
    auth: _Auth, session_factory: SessionFactory, settings: Settings, row: TaxReceipt, result: ProcessResult
) -> None:
    try:
        if row.status == TaxReceiptStatus.PENDING.value:
            await _register(auth, session_factory, row, result)
        else:
            await _cancel(auth, session_factory, row, result)
    except NpdAuthError:
        raise
    except NpdError as exc:
        await _record_failure(session_factory, settings, row.id, str(exc), result)


async def _register(auth: _Auth, session_factory: SessionFactory, row: TaxReceipt, result: ProcessResult) -> None:
    uuid = await _find_existing(auth, session_factory, row) if row.sent else None
    if uuid is None:
        # Remember that a request is about to go out before sending it (crash / timeout safety).
        async with session_factory.begin() as session:
            fresh = await session.get(TaxReceipt, row.id, with_for_update=True)
            if fresh is None or fresh.status != TaxReceiptStatus.PENDING.value:
                return
            if fresh.attempts != row.attempts:
                return  # another run took this attempt meanwhile
            fresh.sent = True
            fresh.attempts += 1
            fresh.updated_at = utcnow()
        uuid = await _with_auth(
            auth, lambda t: auth.client.create_income(t, row.service_name, row.amount_kop, row.operation_time, utcnow())
        )
    now = utcnow()
    async with session_factory.begin() as session:
        fresh = await session.get(TaxReceipt, row.id, with_for_update=True)
        if fresh is None:
            return
        fresh.receipt_uuid = uuid
        fresh.receipt_url = receipt_url(auth.inn, uuid, auth.client.base_url)[:512] if auth.inn else None
        fresh.registered_at = now
        fresh.last_error = None
        fresh.updated_at = now
        if fresh.status == TaxReceiptStatus.CANCEL_PENDING.value:
            fresh.next_attempt_at = now  # refunded meanwhile: annul it on the next pass
        else:
            fresh.status = TaxReceiptStatus.REGISTERED.value
            fresh.next_attempt_at = None
            result.registered.append(row.id)
    log.info("tax receipt %s registered for payment %s", row.id, row.payment_id)


async def _cancel(auth: _Auth, session_factory: SessionFactory, row: TaxReceipt, result: ProcessResult) -> None:
    uuid = row.receipt_uuid
    if uuid is None and row.sent:
        uuid = await _find_existing(auth, session_factory, row)
    if uuid is not None:
        await _with_auth(auth, lambda t: auth.client.cancel_income(t, uuid, utcnow()))
    now = utcnow()
    async with session_factory.begin() as session:
        fresh = await session.get(TaxReceipt, row.id, with_for_update=True)
        if fresh is None:
            return
        fresh.receipt_uuid = fresh.receipt_uuid or uuid
        fresh.status = TaxReceiptStatus.CANCELLED.value
        fresh.cancelled_at = now
        fresh.next_attempt_at = None
        fresh.last_error = None
        fresh.updated_at = now
    result.cancelled.append(row.id)
    log.info("tax receipt %s for payment %s annulled", row.id, row.payment_id)


async def _record_failure(
    session_factory: SessionFactory, settings: Settings, receipt_id: int, error: str, result: ProcessResult
) -> None:
    now = utcnow()
    async with session_factory.begin() as session:
        row = await session.get(TaxReceipt, receipt_id, with_for_update=True)
        if row is None:
            return
        if row.status == TaxReceiptStatus.CANCEL_PENDING.value:
            row.attempts += 1
        attempts = max(row.attempts, 1)
        row.last_error = error[:ERROR_MAX]
        row.updated_at = now
        if attempts >= settings.npd_max_attempts and row.status == TaxReceiptStatus.PENDING.value:
            row.status = TaxReceiptStatus.FAILED.value
            row.next_attempt_at = None
            result.failed.append(receipt_id)
        else:
            row.next_attempt_at = now + backoff(attempts)
    log.warning("tax receipt %s: %s", receipt_id, error[:ERROR_MAX])


async def receipts_to_announce(session_factory: SessionFactory, ids: list[int]) -> list[TaxReceipt]:
    """Registered receipts among ``ids`` whose buyer was not told yet."""
    if not ids:
        return []
    async with session_factory.begin() as session:
        rows = list(
            await session.scalars(
                select(TaxReceipt).where(
                    TaxReceipt.id.in_(ids),
                    TaxReceipt.status == TaxReceiptStatus.REGISTERED.value,
                    TaxReceipt.user_notified.is_(False),
                    TaxReceipt.receipt_url.is_not(None),
                )
            )
        )
        session.expunge_all()
    return rows


async def mark_notified(session_factory: SessionFactory, receipt_id: int) -> None:
    async with session_factory.begin() as session:
        row = await session.get(TaxReceipt, receipt_id)
        if row is not None:
            row.user_notified = True
