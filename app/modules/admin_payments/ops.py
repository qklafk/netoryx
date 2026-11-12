"""Admin payment operations: listing, manual crediting, re-running the post-payment actions and Stars refunds.

Network calls (provider status checks, Telegram refunds) are made outside database transactions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import ColumnElement, select

from app.db.models import LedgerAccount, Payment, PaymentProvider, PaymentStatus, TxKind, User
from app.db.session import read_session
from app.services import audit, hooks, wallet
from app.services.payments import service as payment_service
from app.services.payments.base import GatewayError
from app.services.payments.service import CreditResult

if TYPE_CHECKING:
    from app.core.context import AppContext
    from app.services.hooks import HookOutcome

log = logging.getLogger(__name__)

OPEN = (PaymentStatus.CREATED, PaymentStatus.PENDING)
FAILED = (PaymentStatus.CANCELED, PaymentStatus.EXPIRED)
FILTERS: dict[str, tuple[PaymentStatus, ...] | None] = {
    "all": None,
    "ok": (PaymentStatus.SUCCEEDED,),
    "open": OPEN,
    "fail": FAILED,
    "ref": (PaymentStatus.REFUNDED,),
}
_UUID = re.compile(r"[0-9a-fA-F-]{36}")


class PaymentOpError(Exception):
    """Refused; ``code`` is an i18n suffix (``admin_payments.error.<code>``), ``detail`` an optional message."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def status_filter(name: str) -> tuple[list[ColumnElement[bool]], bool]:
    """WHERE clauses for a list filter name (``all``/``ok``/``open``/``fail``/``ref``/``u<user id>``)."""
    if name.startswith("u") and name[1:].isdigit():
        return [Payment.user_id == int(name[1:])], True
    statuses = FILTERS.get(name)
    if statuses is None:
        return [], name in FILTERS
    return [Payment.status.in_(statuses)], True


async def page(ctx: AppContext, name: str, offset: int, limit: int) -> list[Payment]:
    where, _known = status_filter(name)
    stmt = select(Payment).where(*where).order_by(Payment.created_at.desc(), Payment.id).offset(offset).limit(limit)
    async with read_session(ctx.session_factory) as session, session.begin():
        return list((await session.scalars(stmt)).all())


async def find(ctx: AppContext, query: str) -> Payment | None:
    """A payment by our id or the provider's id."""
    text = query.strip()
    if not text or len(text) > 128:
        return None
    async with read_session(ctx.session_factory) as session, session.begin():
        if _UUID.fullmatch(text):
            payment = await session.get(Payment, text.lower())
            if payment is not None:
                return payment
        return await session.scalar(select(Payment).where(Payment.provider_payment_id == text).limit(1))


async def get(ctx: AppContext, payment_id: str) -> Payment | None:
    async with read_session(ctx.session_factory) as session, session.begin():
        return await session.get(Payment, payment_id)


def credit_result(payment: Payment, balance_kop: int) -> CreditResult:
    """The ``CreditResult`` of an already credited payment (input of the post-payment hook)."""
    return CreditResult(
        payment_id=payment.id,
        user_id=payment.user_id,
        amount_kop=payment.amount_kop,
        new_balance_kop=balance_kop,
        plan_id=payment.plan_id,
        is_gift=payment.is_gift,
        is_donation=payment.is_donation,
        inviter=None,
        provider=payment.provider,
    )


async def mark_paid(ctx: AppContext, payment_id: str, actor_id: int) -> HookOutcome:
    """Credit a payment the provider never confirmed to us (money arrived) and run the post-payment actions."""
    result = await payment_service.credit(ctx.session_factory, ctx.settings, payment_id)
    if result is None:
        raise PaymentOpError("not_creditable")
    async with ctx.session_factory.begin() as session:
        await audit.log(
            session,
            actor_id,
            "payment.mark_paid",
            "payment",
            payment_id,
            user_id=result.user_id,
            amount_kop=result.amount_kop,
        )
    return await hooks.on_payment_credited(ctx, result)


async def rerun_hook(ctx: AppContext, payment_id: str, actor_id: int) -> HookOutcome:
    """Run the post-payment actions of a credited payment again (idempotent: buys / notifies only what is missing)."""
    async with ctx.session_factory.begin() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None:
            raise PaymentOpError("not_found")
        if payment.status != PaymentStatus.SUCCEEDED:
            raise PaymentOpError("not_succeeded")
        balance = await session.scalar(select(User.balance_kop).where(User.tg_id == payment.user_id))
        result = credit_result(payment, int(balance or 0))
        await audit.log(session, actor_id, "payment.rerun_hook", "payment", payment_id, user_id=payment.user_id)
    return await hooks.on_payment_credited(ctx, result)


async def check(ctx: AppContext, payment_id: str) -> PaymentStatus:
    """Ask the provider about an open payment and apply its answer; returns the resulting status."""
    payment = await get(ctx, payment_id)
    if payment is None:
        raise PaymentOpError("not_found")
    gateway = ctx.gateways.get(payment.provider)
    if gateway is None or not gateway.supports_polling:
        raise PaymentOpError("no_polling")
    try:
        status = await gateway.fetch(payment)
    except GatewayError as exc:
        raise PaymentOpError("provider_error", str(exc)[:200]) from exc
    await payment_service.apply_provider_status(
        ctx.session_factory, ctx.settings, payment, status, ctx.on_payment_credited
    )
    fresh = await get(ctx, payment_id)
    return fresh.status if fresh is not None else status.status


@dataclass(frozen=True, slots=True)
class RefundResult:
    debited_kop: int


def can_refund_stars(payment: Payment) -> bool:
    return (
        payment.provider == PaymentProvider.STARS
        and payment.status == PaymentStatus.SUCCEEDED
        and bool(payment.provider_payment_id)
    )


async def refund_stars(ctx: AppContext, payment_id: str, actor_id: int, *, debit: bool) -> RefundResult:
    """Return the Stars to the payer, mark the payment REFUNDED and optionally take the credit back from the wallet
    (as much as is left on the balance)."""
    payment = await get(ctx, payment_id)
    if payment is None:
        raise PaymentOpError("not_found")
    if not can_refund_stars(payment):
        raise PaymentOpError("not_refundable")
    assert payment.provider_payment_id is not None
    try:
        await ctx.bot.refund_star_payment(
            user_id=payment.user_id, telegram_payment_charge_id=payment.provider_payment_id
        )
    except TelegramBadRequest as exc:
        raise PaymentOpError("refund_failed", str(exc)[:200]) from exc
    await payment_service.mark_status(ctx.session_factory, payment_id, PaymentStatus.REFUNDED)
    debited = 0
    async with ctx.session_factory.begin() as session:
        if debit and not payment.is_donation:
            balance = await wallet.balance(session, payment.user_id)
            amount = min(payment.amount_kop, max(0, balance))
            if amount > 0:
                applied = await wallet.apply(
                    session,
                    payment.user_id,
                    -amount,
                    TxKind.REFUND,
                    payment.id,
                    actor_id=actor_id,
                    account=LedgerAccount.MAIN,
                )
                debited = amount if applied is not None else 0
        await audit.log(
            session,
            actor_id,
            "payment.refund",
            "payment",
            payment_id,
            user_id=payment.user_id,
            provider=payment.provider,
            amount_kop=payment.amount_kop,
            debited_kop=debited,
        )
    log.info("admin %s refunded Stars payment %s (debited %s kop)", actor_id, payment_id, debited)
    return RefundResult(debited)
