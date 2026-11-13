"""Payment lifecycle: create -> (webhook | reconcile) -> credit, exactly once.

States: ``CREATED`` (row persisted, provider not called yet) -> ``PENDING`` (invoice exists) ->
``SUCCEEDED`` | ``CANCELED`` | ``EXPIRED``; ``SUCCEEDED`` -> ``REFUNDED`` (chargeback / refund reported by
the provider; the wallet is not touched automatically).

Rules
* No network I/O while a transaction is open: the row is committed before calling the provider and
  the provider's answer is stored in a second short transaction.
* Crediting is claimed by one conditional ``UPDATE ... WHERE status IN (...)``; only the transaction
  whose update matched the row credits the wallet, so concurrent webhooks and the reconciler can
  never credit twice. ``wallet.apply`` (ledger ``UNIQUE(kind, ref, user_id)``) is a second safety net.
* Webhook bodies are never trusted for the status unless the provider cannot be polled and the body
  is signed (``WebhookResult.trusted_status``).
* A payment reported as paid after it was expired or canceled locally is still credited: the money
  has arrived.
* Links that can be paid more than once (YooMoney quickpay, FreeKassa SCI, Telegram Stars invoices)
  may bring a second transfer for an already credited payment. It is told apart from a redelivery by
  the provider's transfer id and, depending on the gateway's ``repeat_payments``, recorded as a new
  wallet top-up (:func:`record_repeat`) or reported as a ``payment.repeat_unrecorded`` event.
* A signed notification of a transfer the provider holds (YooMoney ``unaccepted``: the receiver's balance
  limit) is not credited, but it is recorded: its transfer id is stored on the payment (or on a repeat
  row for an already paid link), a ``payment.held`` event names it, and the payment is never expired
  automatically. Once the transfer is accepted, a notification for it or an admin's :func:`credit`
  credits it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, case, func, not_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Payment, PaymentStatus, TxKind, User
from app.db.session import SessionFactory, read_session
from app.services import events, referrals, tax_receipts, wallet
from app.services.payments.base import GatewayError, PaymentGateway, ProviderStatus, WebhookResult

log = logging.getLogger(__name__)

OPEN_STATUSES = (PaymentStatus.CREATED, PaymentStatus.PENDING)
MAX_OPEN_PAYMENTS_PER_USER = 20  # a user cannot flood the provider and the reconciler with unpaid invoices
# Statuses a provider-confirmed payment may be credited from (late payments included).
CREDITABLE_STATUSES = (*OPEN_STATUSES, PaymentStatus.EXPIRED, PaymentStatus.CANCELED)
_TRANSITIONS: dict[PaymentStatus, tuple[PaymentStatus, ...]] = {
    PaymentStatus.PENDING: (PaymentStatus.CREATED,),
    PaymentStatus.CANCELED: OPEN_STATUSES,
    PaymentStatus.EXPIRED: OPEN_STATUSES,
    PaymentStatus.REFUNDED: (PaymentStatus.SUCCEEDED,),
}
RECONCILE_BATCH = 500  # payments polled per reconcile run
RECONCILE_CONCURRENCY = 8
# Share of RECONCILE_BATCH kept for the final check of overdue payments when fresh ones fill the batch.
RECONCILE_OVERDUE_SHARE = 0.25

# PaymentGateway.repeat_payments: what to do with a further, distinct transfer for an already paid payment.
REPEAT_CREDIT = "credit"  # record it as a new payment and credit it to the wallet
REPEAT_ALERT = "alert"  # only report it (its transfer id cannot reliably be told apart from a redelivery)


@dataclass(frozen=True, slots=True)
class CreditResult:
    """Outcome of a successful credit, passed to ``OnCredited`` after the commit."""

    payment_id: str
    user_id: int
    amount_kop: int  # actually credited (may be less than requested on a partial payment)
    new_balance_kop: int
    plan_id: int | None
    is_gift: bool
    is_donation: bool
    inviter: tuple[int, int] | None  # (inviter_id, bonus_kop) granted by this payment
    provider: str
    expected_amount_kop: int | None = None  # set only when the paid amount differed from the invoice
    # Telegram Stars native subscription this payment belongs to (its first charge id); None for one-offs.
    recurring_charge_id: str | None = None
    subscription_id: int | None = None

    @property
    def is_partial(self) -> bool:
        return self.expected_amount_kop is not None and self.amount_kop < self.expected_amount_kop


OnCredited = Callable[[CreditResult], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------------------------
async def create_payment(
    session_factory: SessionFactory,
    settings: Settings,
    gateway: PaymentGateway,
    user_id: int,
    amount_kop: int,
    *,
    plan_id: int | None = None,
    plan_price_kop: int | None = None,
    is_gift: bool = False,
    is_donation: bool = False,
    is_recurring: bool = False,
    save_method: bool = False,
    provider_amount: str | None = None,
    description: str | None = None,
    subscription_id: int | None = None,
    new_subscription: bool = False,
    subscription_label: str | None = None,
) -> Payment:
    """Persist a payment, create the invoice at the provider and store its id/URL.

    ``provider_amount`` pre-sets the amount in the provider's unit (Telegram Stars for a plan with an
    explicit Stars price). Raises ValueError for an invalid amount, LookupError for an unknown user and
    GatewayError if the provider fails; the payment is then marked CANCELED.
    """
    if isinstance(amount_kop, bool) or not isinstance(amount_kop, int) or amount_kop <= 0:
        raise ValueError("amount_kop must be a positive int")
    if plan_price_kop is not None and (
        isinstance(plan_price_kop, bool) or not isinstance(plan_price_kop, int) or plan_price_kop < 0
    ):
        raise ValueError("plan_price_kop must be a non-negative int")
    if amount_kop < gateway.min_amount_kop:
        raise ValueError(f"amount is below the {gateway.key} minimum of {gateway.min_amount_kop} kop")

    payment, user = await _insert_payment(
        session_factory,
        Payment(
            id=str(uuid.uuid4()),
            provider=gateway.key,
            user_id=user_id,
            amount_kop=amount_kop,
            plan_id=plan_id,
            plan_price_kop=plan_price_kop,
            subscription_id=subscription_id,
            new_subscription=new_subscription,
            subscription_label=subscription_label,
            is_gift=is_gift,
            is_donation=is_donation,
            is_recurring=is_recurring,
            save_method=save_method,
            provider_amount=provider_amount,
            status=PaymentStatus.CREATED,
        ),
    )
    try:
        invoice = await gateway.create(payment, user, description or settings.payment_description)
    except Exception as exc:
        await mark_status(session_factory, payment.id, PaymentStatus.CANCELED)
        log.warning("payment %s: %s create failed: %s", payment.id, gateway.key, exc)
        if isinstance(exc, GatewayError):
            raise
        raise GatewayError(f"{gateway.key}: {type(exc).__name__}: {exc}") from exc

    async with session_factory.begin() as s:
        await s.execute(
            update(Payment)
            .where(Payment.id == payment.id)
            .values(
                provider_payment_id=func.coalesce(Payment.provider_payment_id, invoice.provider_payment_id),
                confirmation_url=invoice.url,
                provider_amount=invoice.provider_amount or Payment.provider_amount,
                provider_currency=invoice.provider_currency,
                # A webhook may already have credited it: only CREATED moves to PENDING.
                status=case((Payment.status == PaymentStatus.CREATED, PaymentStatus.PENDING), else_=Payment.status),
                updated_at=_now(),
            )
        )
        stored = await s.get(Payment, payment.id, populate_existing=True)
    assert stored is not None
    log.info("payment %s: %s invoice %s for user %s", stored.id, gateway.key, stored.provider_payment_id, user_id)
    return stored


async def _insert_payment(session_factory: SessionFactory, payment: Payment) -> tuple[Payment, User]:
    async with session_factory.begin() as s:
        user = await s.get(User, payment.user_id)
        if user is None:
            raise LookupError(f"user {payment.user_id} not found")
        if payment.new_subscription and payment.subscription_id is not None:
            raise ValueError("choose a new subscription or an existing subscription")
        if payment.subscription_id is not None:
            from app.services import subscriptions

            if await subscriptions.get_for_user(s, user.tg_id, payment.subscription_id) is None:
                raise ValueError("subscription does not belong to the payer")
        open_count = await s.scalar(
            select(func.count())
            .select_from(Payment)
            .where(Payment.user_id == user.tg_id, Payment.status.in_(OPEN_STATUSES))
        )
        if (open_count or 0) >= MAX_OPEN_PAYMENTS_PER_USER:
            raise ValueError("too many unpaid payments")
        s.add(payment)
    return payment, user


# ---------------------------------------------------------------------------------------------
# Status changes
# ---------------------------------------------------------------------------------------------
async def mark_status(session_factory: SessionFactory, payment_id: str, status: PaymentStatus) -> bool:
    """Move a payment to ``status`` if the transition is allowed; returns whether it changed.

    Allowed: CREATED -> PENDING; CREATED/PENDING -> CANCELED/EXPIRED; SUCCEEDED -> REFUNDED.
    SUCCEEDED is reachable only through :func:`credit`.
    """
    sources = _TRANSITIONS.get(status)
    if sources is None:
        raise ValueError(f"cannot mark a payment as {status}; use credit() for SUCCEEDED")
    async with session_factory.begin() as s:
        result = await s.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status.in_(sources))
            .values(status=status, updated_at=_now())
        )
        changed = result.rowcount == 1  # type: ignore[attr-defined]
        if changed and status is PaymentStatus.REFUNDED:
            await tax_receipts.on_refund(s, payment_id)
            payment = await s.get(Payment, payment_id)
            if payment is not None:
                await events.track(
                    s,
                    "payment.refunded",
                    payment.user_id,
                    payment.amount_kop,
                    provider=payment.provider,
                    payment_id=payment_id,
                )
    if changed:
        log.info("payment %s -> %s", payment_id, status)
    return changed


# ---------------------------------------------------------------------------------------------
# Credit
# ---------------------------------------------------------------------------------------------
async def credit(
    session_factory: SessionFactory,
    settings: Settings,
    payment_id: str,
    *,
    paid_amount_kop: int | None = None,
    provider_payment_id: str | None = None,
) -> CreditResult | None:
    """Mark a payment SUCCEEDED and credit the wallet exactly once.

    Returns None if the payment is unknown or was already credited. ``paid_amount_kop`` is what the
    provider says the user paid: less than expected credits the paid amount, more credits the
    expected amount (both are recorded as a ``payment.amount_mismatch`` event). Donations are never
    credited to the wallet and earn no referral bonus.
    """
    now = _now()
    async with session_factory.begin() as s:
        # Claim first (the write lock is taken by the first statement): exactly one caller wins.
        claimed = await s.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status.in_(CREDITABLE_STATUSES))
            .values(
                status=PaymentStatus.SUCCEEDED,
                credited_at=now,
                updated_at=now,
                provider_payment_id=func.coalesce(Payment.provider_payment_id, provider_payment_id),
            )
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            log.debug("payment %s: not creditable (unknown or already credited)", payment_id)
            return None
        payment = await s.get(Payment, payment_id, populate_existing=True)
        assert payment is not None
        result = await _apply_credit(s, settings, payment, paid_amount_kop)
    log.info(
        "payment %s: credited %s kop to user %s via %s", payment_id, result.amount_kop, result.user_id, result.provider
    )
    return result


def _credited_amount(expected_kop: int, paid_kop: int | None) -> int:
    """Credit what was paid, but never more than the invoice (guards against unit mix-ups)."""
    if paid_kop is None:
        return expected_kop
    return max(0, min(expected_kop, paid_kop))


async def _apply_credit(s: AsyncSession, settings: Settings, payment: Payment, paid_kop: int | None) -> CreditResult:
    expected = payment.amount_kop
    amount = _credited_amount(expected, paid_kop)
    mismatch = paid_kop is not None and paid_kop != expected
    if mismatch:
        payment.amount_kop = amount  # Payment.amount_kop always holds what was credited
        log.warning("payment %s: paid %s kop, expected %s kop", payment.id, paid_kop, expected)
        await events.track(
            s,
            "payment.amount_mismatch",
            payment.user_id,
            amount,
            payment_id=payment.id,
            expected_kop=expected,
            paid_kop=paid_kop,
        )

    inviter: tuple[int, int] | None = None
    if payment.is_donation:
        await events.track(
            s, "donation.received", payment.user_id, amount, provider=payment.provider, payment_id=payment.id
        )
    else:
        if amount > 0 and await wallet.apply(s, payment.user_id, amount, TxKind.PAYMENT, payment.id) is None:
            log.warning("payment %s: ledger row already existed", payment.id)
        if amount > 0:
            inviter = await referrals.on_invitee_payment(
                s, settings, payment.user_id, payment.id, amount, expected_kop=expected
            )
        await events.track(
            s, "payment.succeeded", payment.user_id, amount, provider=payment.provider, payment_id=payment.id
        )
    await tax_receipts.enqueue(s, settings, payment, amount)
    balance = await s.scalar(select(User.balance_kop).where(User.tg_id == payment.user_id))
    return CreditResult(
        payment_id=payment.id,
        user_id=payment.user_id,
        amount_kop=amount,
        new_balance_kop=int(balance or 0),
        plan_id=payment.plan_id,
        is_gift=payment.is_gift,
        is_donation=payment.is_donation,
        inviter=inviter,
        provider=payment.provider,
        expected_amount_kop=expected if mismatch else None,
        subscription_id=payment.subscription_id,
    )


# ---------------------------------------------------------------------------------------------
# Provider status -> local state
# ---------------------------------------------------------------------------------------------
async def apply_provider_status(
    session_factory: SessionFactory,
    settings: Settings,
    payment: Payment,
    status: ProviderStatus,
    on_credited: OnCredited | None = None,
) -> CreditResult | None:
    """Converge a payment to the provider's status; credits (and notifies) on success."""
    if status.status is PaymentStatus.SUCCEEDED:
        result = await credit(
            session_factory,
            settings,
            payment.id,
            paid_amount_kop=status.paid_amount_kop,
            provider_payment_id=status.provider_payment_id,
        )
        if status.saved_method is not None:
            await _remember_card(session_factory, settings, payment, status)
        if result is not None and on_credited is not None:
            await _notify(on_credited, result)
        return result
    if status.status in (PaymentStatus.CANCELED, PaymentStatus.EXPIRED, PaymentStatus.REFUNDED):
        await mark_status(session_factory, payment.id, status.status)
    return None


async def _remember_card(
    session_factory: SessionFactory, settings: Settings, payment: Payment, status: ProviderStatus
) -> None:
    """Keep the card a verified paid status reports, if the payer agreed; never blocks crediting."""
    from app.services import saved_cards

    assert status.saved_method is not None
    try:
        await saved_cards.remember(session_factory, settings, payment, status.saved_method)
    except Exception:
        log.exception("payment %s: storing the saved card failed", payment.id)


async def _notify(on_credited: OnCredited, result: CreditResult) -> None:
    """Run the post-credit hook; the money is already credited, so failures are only logged."""
    try:
        await on_credited(result)
    except Exception:
        log.exception("payment %s: on_credited hook failed", result.payment_id)


# ---------------------------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------------------------
async def handle_webhook(
    session_factory: SessionFactory,
    settings: Settings,
    gateway: PaymentGateway,
    raw: bytes,
    headers: Mapping[str, str],
    remote_ip: str | None,
    on_credited: OnCredited | None = None,
    *,
    query: str = "",
) -> WebhookResult:
    """Verify a webhook, re-check the status with the provider and credit.

    ``query`` is the request's raw query string: for a gateway with ``webhook_query_fields`` (a provider
    that may deliver by GET) it is the payload when the body is empty; other gateways never see it.

    Raises WebhookRejected (answer with its ``status_code``). If the provider cannot be reached the
    result has ``status_code=503`` so the provider retries; the reconciler will catch up meanwhile.
    """
    if not raw.strip() and query and getattr(gateway, "webhook_query_fields", False):
        raw = query.encode()
    result = await gateway.parse_webhook(raw, headers, remote_ip)
    payments = await _find_payments(session_factory, gateway.key, result.payment_ids, result.provider_ids)
    if not payments and (result.payment_ids or result.provider_ids):
        log.warning("%s webhook: no payment for ids %s / %s", gateway.key, result.payment_ids, result.provider_ids)
    known_transfers = {p.provider_payment_id for p in payments if p.provider_payment_id}
    failed = False
    for payment in payments:
        status = await _webhook_status(gateway, payment, result)
        if status is None:
            failed = True
            continue
        if result.trusted_status is not None and status.provider_payment_id:
            transfer_id = status.provider_payment_id
            if status.status is PaymentStatus.PENDING:
                if transfer_id not in known_transfers:  # else a redelivery of a recorded held transfer
                    await _record_held(session_factory, gateway, payment, status)
                continue
            if payment.status in CREDITABLE_STATUSES and payment.provider_payment_id not in (None, transfer_id):
                # The unpaid payment already holds another (held) transfer: this one is a further transfer
                # through the link; crediting it under the held one's row would hide the held one.
                if transfer_id not in known_transfers:
                    await _repeat_transfer(session_factory, settings, gateway, payment.id, status, on_credited)
                continue
        credited = await apply_provider_status(session_factory, settings, payment, status, on_credited)
        if credited is None and result.trusted_status is not None and status.provider_payment_id not in known_transfers:
            await _repeat_transfer(session_factory, settings, gateway, payment.id, status, on_credited)
    return replace(result, status_code=503, response_body="retry later") if failed else result


async def _repeat_transfer(
    session_factory: SessionFactory,
    settings: Settings,
    gateway: PaymentGateway,
    payment_id: str,
    status: ProviderStatus,
    on_credited: OnCredited | None,
) -> CreditResult | None:
    """A signed "paid" notification for an already credited payment (or for an unpaid one that holds
    another, held transfer): a redelivery or a second transfer.

    A second transfer (another provider transfer id) is credited as a new payment or reported,
    according to the gateway's ``repeat_payments``; a redelivery changes nothing.
    """
    mode = getattr(gateway, "repeat_payments", None)
    transfer_id = status.provider_payment_id
    if mode not in (REPEAT_CREDIT, REPEAT_ALERT) or status.status is not PaymentStatus.SUCCEEDED or not transfer_id:
        return None
    async with read_session(session_factory) as s:
        current = await s.get(Payment, payment_id)
    if current is None:
        return None
    holds_other = current.status in CREDITABLE_STATUSES and current.provider_payment_id not in (None, transfer_id)
    if current.status not in (PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED) and not holds_other:
        return None  # not credited (yet): nothing was lost
    if current.provider_payment_id == transfer_id:
        return None  # a redelivery of the transfer that was credited
    amount = status.paid_amount_kop or current.amount_kop
    if mode == REPEAT_ALERT or current.provider_payment_id is None:
        log.warning(
            "payment %s: %s reports another transfer %s (credited: %s); not recorded, reconcile it manually",
            current.id,
            gateway.key,
            transfer_id,
            current.provider_payment_id,
        )
        async with session_factory.begin() as s:
            await events.track(
                s,
                "payment.repeat_unrecorded",
                current.user_id,
                amount,
                provider=current.provider,
                payment_id=current.id,
                provider_payment_id=transfer_id,
            )
        return None
    repeat = await record_repeat(session_factory, current, transfer_id, amount)
    credited = await credit(
        session_factory,
        settings,
        repeat.id,
        paid_amount_kop=status.paid_amount_kop,
        provider_payment_id=transfer_id,
    )
    if credited is not None and on_credited is not None:
        await _notify(on_credited, credited)
    return credited


async def _record_held(
    session_factory: SessionFactory, gateway: PaymentGateway, payment: Payment, status: ProviderStatus
) -> None:
    """Record a transfer the provider reports as received but held (not credited to the receiver yet).

    The provider may never notify again once the receiver accepts the transfer, so it is recorded
    durably: the transfer id is stored on the unpaid payment, or, if the payment is paid already (or holds
    another transfer), on a PENDING repeat row (``repeat_payments="credit"``). Either row is then never
    expired automatically. A ``payment.held`` event and a warning name it for an admin, who credits it with
    :func:`credit` once the transfer is accepted; a later notification for it credits it as well.
    """
    transfer_id = status.provider_payment_id
    assert transfer_id
    amount = status.paid_amount_kop or payment.amount_kop
    held_on: str | None = None
    try:
        async with session_factory.begin() as s:
            claimed = await s.execute(
                update(Payment)
                .where(
                    Payment.id == payment.id,
                    Payment.status.in_(CREDITABLE_STATUSES),
                    Payment.provider_payment_id.is_(None),
                )
                .values(provider_payment_id=transfer_id, updated_at=_now())
            )
            if claimed.rowcount == 1:  # type: ignore[attr-defined]
                held_on = payment.id
                await _track_held(s, payment, held_on, transfer_id, amount)
    except IntegrityError:
        return  # UNIQUE(provider, provider_payment_id): recorded concurrently on another row
    if held_on is None:
        async with read_session(session_factory) as s:
            current = await s.get(Payment, payment.id)
        if current is None or current.provider_payment_id in (None, transfer_id):
            return  # gone, or recorded concurrently
        if getattr(gateway, "repeat_payments", None) == REPEAT_CREDIT:
            row = await record_repeat(session_factory, current, transfer_id, amount)
            if row.status is not PaymentStatus.PENDING:
                return  # this transfer was recorded (and credited) concurrently
            held_on = row.id
        async with session_factory.begin() as s:
            await _track_held(s, current, held_on, transfer_id, amount)
    log.warning(
        "payment %s: %s transfer %s of %s kop is held by the provider (not credited); "
        "credit it once the transfer is accepted",
        held_on or payment.id,
        gateway.key,
        transfer_id,
        amount,
    )


async def _track_held(s: AsyncSession, payment: Payment, held_on: str | None, transfer_id: str, amount: int) -> None:
    """The ``payment.held`` event; ``held_on`` is the row holding the transfer id (None: not stored)."""
    data: dict[str, str] = {"payment_id": held_on or payment.id, "provider_payment_id": transfer_id}
    if held_on != payment.id:
        data["original_payment_id"] = payment.id
    if held_on is None:
        data["recorded"] = "no"  # the transfer id could not be stored: reconcile it manually
    await events.track(s, "payment.held", payment.user_id, amount, provider=payment.provider, **data)


async def record_repeat(
    session_factory: SessionFactory,
    original: Payment,
    provider_payment_id: str,
    amount_kop: int,
    *,
    provider_amount: str | None = None,
) -> Payment:
    """The payment row for a further transfer through ``original``'s invoice, inserted (PENDING) once.

    The transfer becomes a plain wallet top-up (no plan is bought, no gift code issued); a donation
    stays a donation. A transfer seen before returns its existing row (UNIQUE provider id), so
    crediting it again is a no-op unless an earlier attempt crashed between recording and crediting.
    """
    repeat = Payment(
        id=str(uuid.uuid4()),
        provider=original.provider,
        provider_payment_id=provider_payment_id,
        user_id=original.user_id,
        amount_kop=amount_kop,
        plan_id=None,
        is_gift=False,
        is_donation=original.is_donation,
        is_recurring=False,
        provider_amount=provider_amount,
        provider_currency=original.provider_currency,
        status=PaymentStatus.PENDING,
    )
    try:
        async with session_factory.begin() as s:
            s.add(repeat)
            await s.flush()  # UNIQUE(provider, provider_payment_id) is checked here
            await events.track(
                s,
                "payment.repeat",
                original.user_id,
                amount_kop,
                provider=original.provider,
                payment_id=repeat.id,
                original_payment_id=original.id,
                provider_payment_id=provider_payment_id,
            )
    except IntegrityError:
        async with read_session(session_factory) as s:
            existing = await s.scalar(
                select(Payment).where(
                    Payment.provider == original.provider, Payment.provider_payment_id == provider_payment_id
                )
            )
        if existing is None:
            raise
        return existing
    log.warning(
        "payment %s: repeat transfer %s through the invoice of %s recorded as a top-up",
        repeat.id,
        provider_payment_id,
        original.id,
    )
    return repeat


async def _webhook_status(gateway: PaymentGateway, payment: Payment, result: WebhookResult) -> ProviderStatus | None:
    """The trusted status for one payment, or None if it could not be determined now."""
    if result.trusted_status is not None:
        return result.trusted_status
    if not gateway.supports_polling:
        log.error("%s webhook carries no trusted status and the provider cannot be polled", gateway.key)
        return ProviderStatus(PaymentStatus.PENDING)
    try:
        return await gateway.fetch(payment)
    except GatewayError as exc:
        log.warning("payment %s: %s status check failed: %s", payment.id, gateway.key, exc)
        return None


async def _find_payments(
    session_factory: SessionFactory, provider: str, payment_ids: Iterable[str], provider_ids: Iterable[str]
) -> list[Payment]:
    ours = [i for i in set(payment_ids) if i and len(i) <= 36]
    theirs = [i for i in set(provider_ids) if i and len(i) <= 128]
    if not ours and not theirs:
        return []
    async with read_session(session_factory) as s:
        rows = await s.scalars(
            select(Payment).where(
                Payment.provider == provider,
                or_(Payment.id.in_(ours), Payment.provider_payment_id.in_(theirs)),
            )
        )
        return list(rows.all())


# ---------------------------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------------------------
async def reconcile(
    session_factory: SessionFactory,
    settings: Settings,
    gateways: Mapping[str, PaymentGateway],
    on_credited: OnCredited | None = None,
) -> int:
    """Poll pending payments of pollable providers, then expire stale open payments.

    At most ``RECONCILE_BATCH`` payments are polled per run: the newest first (they are the ones being
    paid right now), with a share kept for the oldest overdue ones. A pollable payment older than
    ``PAYMENT_MAX_PENDING_HOURS`` expires only after a successful final check, so an overdue backlog
    drains over several runs and nothing is expired unchecked; open payments that cannot be polled
    expire at the cutoff, except those holding a transfer the provider reported as held (see
    :func:`_record_held`). Returns the number of payments credited.
    """
    pollable = {key: g for key, g in gateways.items() if g.supports_polling}
    holding = [key for key, g in gateways.items() if not g.supports_polling]
    by_our_id = [key for key, g in pollable.items() if getattr(g, "polls_by_payment_id", False)]
    cutoff = _now() - timedelta(hours=settings.payment_max_pending_hours)
    pending = await _pending_for(session_factory, pollable.keys(), by_our_id, cutoff=cutoff)
    semaphore = asyncio.Semaphore(RECONCILE_CONCURRENCY)

    async def check(payment: Payment) -> tuple[str, bool | None]:
        """(id, credited?); None for "credited?" when the check failed: retried next run, not expired."""
        async with semaphore:
            try:
                status = await pollable[payment.provider].fetch(payment)
            except GatewayError as exc:
                log.info("payment %s: %s status check failed: %s", payment.id, payment.provider, exc)
                return payment.id, None
            except Exception:  # one payment's failure must not abort the run (and skip the expiry)
                log.exception("payment %s: %s status check crashed", payment.id, payment.provider)
                return payment.id, None
            try:
                credited = await apply_provider_status(session_factory, settings, payment, status, on_credited)
            except Exception:  # e.g. "database is locked", a deadlock: the next run tries again
                log.exception("payment %s: applying %s status %s failed", payment.id, payment.provider, status.status)
                return payment.id, None
            return payment.id, credited is not None

    outcomes = await asyncio.gather(*(check(p) for p in pending))
    checked = {pid for pid, ok in outcomes if ok is not None}
    expired = await _expire_stale(session_factory, cutoff, pollable.keys(), by_our_id, checked=checked, holding=holding)
    credited = sum(1 for _, ok in outcomes if ok)
    if credited or expired:
        log.info("payments reconcile: %s credited, %s expired", credited, expired)
    return credited


def _pollable(providers: Iterable[str], by_our_id: Iterable[str]) -> ColumnElement[bool]:
    """PENDING payments the reconciler can poll: with a provider id, or of a provider in ``by_our_id``."""
    return and_(
        Payment.status == PaymentStatus.PENDING,
        Payment.provider.in_(list(providers)),
        or_(Payment.provider_payment_id.is_not(None), Payment.provider.in_(list(by_our_id))),
    )


async def _pending_for(
    session_factory: SessionFactory, providers: Iterable[str], by_our_id: Iterable[str], *, cutoff: datetime
) -> list[Payment]:
    """Payments to poll this run: the newest first, plus the oldest overdue ones (created before ``cutoff``).

    Overdue payments get at least ``RECONCILE_OVERDUE_SHARE`` of the batch (all of it if nothing else
    is pending), so neither a burst of fresh invoices nor an overdue backlog starves the other.
    """
    keys = list(providers)
    if not keys:
        return []
    batch = max(1, RECONCILE_BATCH)
    reserved = max(1, int(batch * RECONCILE_OVERDUE_SHARE))
    pollable = _pollable(keys, by_our_id)
    async with read_session(session_factory) as s:
        fresh = (
            await s.scalars(
                select(Payment)
                .where(pollable, Payment.created_at >= cutoff)
                .order_by(Payment.created_at.desc(), Payment.id)
                .limit(batch)
            )
        ).all()
        overdue = (
            await s.scalars(
                select(Payment)
                .where(pollable, Payment.created_at < cutoff)
                .order_by(Payment.created_at, Payment.id)
                .limit(batch - min(len(fresh), batch - reserved))
            )
        ).all()
    return [*fresh[: batch - len(overdue)], *overdue]


async def _expire_stale(
    session_factory: SessionFactory,
    cutoff: datetime,
    providers: Iterable[str],
    by_our_id: Iterable[str],
    *,
    checked: set[str],
    holding: Iterable[str] = (),
) -> int:
    """Expire open payments created before ``cutoff``: unpollable ones, and pollable ones checked this run.

    An open payment of an unpollable provider in ``holding`` that has a provider transfer id is never
    expired: such a payment only gets one from a held transfer (:func:`_record_held`), which waits for
    the receiver to accept it.
    """
    done: ColumnElement[bool] = not_(_pollable(providers, by_our_id))
    held = list(holding)
    if held:
        done = and_(done, not_(and_(Payment.provider.in_(held), Payment.provider_payment_id.is_not(None))))
    if checked:
        done = or_(done, Payment.id.in_(checked))
    stmt = (
        update(Payment)
        .where(Payment.status.in_(OPEN_STATUSES), Payment.created_at < cutoff, done)
        .values(status=PaymentStatus.EXPIRED, updated_at=_now())
    )
    async with session_factory.begin() as s:
        result = await s.execute(stmt)
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
