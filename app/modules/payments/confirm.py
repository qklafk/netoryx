"""Telegram Stars confirmations: ``pre_checkout_query``, ``successful_payment`` and ``refunded_payment``.

Money that Telegram has already taken must be accounted for even while the payments module is switched off (or a
partial maintenance hides it), and no other handler (a catch-all message handler of another module) may swallow
it. So these handlers are registered on the dispatcher itself, outside every module switch: the dispatcher's own
handlers run before any router.
"""

from __future__ import annotations

import logging
import weakref
from typing import TYPE_CHECKING, Any

from aiogram import Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, PreCheckoutQuery
from sqlalchemy import select

from app.db.models import Payment, PaymentProvider, PaymentStatus
from app.db.session import read_session
from app.services.payments import service, stars

if TYPE_CHECKING:
    from app.core.context import AppContext
    from app.core.i18n import Translator

log = logging.getLogger(__name__)

STARS = PaymentProvider.STARS.value
_INSTALLED: weakref.WeakKeyDictionary[Dispatcher, list[tuple[Any, Any]]] = weakref.WeakKeyDictionary()


async def pre_checkout(query: PreCheckoutQuery, ctx: AppContext, _: Translator) -> None:
    """Approve only our open Stars payment of this user for the exact amount (Telegram waits at most 10 s)."""
    ok = False
    if ctx.modules.is_enabled("payments", ctx.settings) and ctx.gateways.get(STARS) is not None:
        try:
            ok = await stars.validate_pre_checkout(
                ctx.session_factory,
                query.invoice_payload,
                query.total_amount,
                query.from_user.id,
                currency=query.currency,
                settings=ctx.settings,
            )
        except Exception:
            log.exception("pre-checkout %s: validation failed", query.id)
    if ok:
        await query.answer(ok=True)
    else:
        log.info("pre-checkout %s of user %s declined", query.id, query.from_user.id)
        await query.answer(ok=False, error_message=_("payments.stars.declined"))


async def successful_payment(message: Message, ctx: AppContext) -> None:
    """Credit a Stars payment (the post-credit hook buys the plan and tells the user)."""
    paid = message.successful_payment
    if paid is None or message.from_user is None:
        return
    result = await stars.credit_successful_payment(
        ctx.session_factory,
        ctx.settings,
        payload=paid.invoice_payload,
        user_id=message.from_user.id,
        total_amount=paid.total_amount,
        charge_id=paid.telegram_payment_charge_id,
        currency=paid.currency,
        is_recurring=bool(paid.is_recurring),
        is_first_recurring=bool(paid.is_first_recurring),
        on_credited=ctx.on_payment_credited,
        cancel_charge=_charge_canceler(ctx),
    )
    if result is None:
        log.info(
            "stars: charge %s of user %s was already processed", paid.telegram_payment_charge_id, message.from_user.id
        )


def _charge_canceler(ctx: AppContext) -> stars.ChargeCanceler:
    """Cancels a Stars subscription no longer linked to the user's subscription (it would keep charging)."""

    async def cancel(user_id: int, charge_id: str) -> None:
        try:
            await ctx.bot.edit_user_star_subscription(
                user_id=user_id, telegram_payment_charge_id=charge_id, is_canceled=True
            )
        except TelegramBadRequest as exc:  # already ended or canceled: nothing more will be charged
            log.warning("stars: subscription %s of user %s was not canceled: %s", charge_id, user_id, exc.message)
            return
        log.info("stars: unlinked subscription %s of user %s canceled", charge_id, user_id)

    return cancel


async def refunded_payment(message: Message, ctx: AppContext) -> None:
    """A Stars payment was refunded (by an admin): mark it and stop treating its subscription as Stars-renewed."""
    refund = message.refunded_payment
    if refund is None or message.from_user is None:
        return
    charge_id = refund.telegram_payment_charge_id
    async with read_session(ctx.session_factory) as session:
        payment_id = await session.scalar(
            select(Payment.id).where(Payment.provider == STARS, Payment.provider_payment_id == charge_id)
        )
    if payment_id is None:
        log.warning("stars: refund of unknown charge %s", charge_id)
        return
    await service.mark_status(ctx.session_factory, payment_id, PaymentStatus.REFUNDED)
    await stars.unlink_recurring(ctx.session_factory, message.from_user.id, charge_id)


def install(dispatcher: Dispatcher) -> bool:
    """Register the confirmation handlers on ``dispatcher`` (once); True if they were added now."""
    if dispatcher in _INSTALLED:
        return False
    handlers = [
        (dispatcher.pre_checkout_query, dispatcher.pre_checkout_query.register(pre_checkout)),
        (dispatcher.message, dispatcher.message.register(successful_payment, F.successful_payment)),
        (dispatcher.message, dispatcher.message.register(refunded_payment, F.refunded_payment)),
    ]
    _INSTALLED[dispatcher] = handlers
    return True


def uninstall(dispatcher: Dispatcher) -> None:
    """Remove the handlers added by :func:`install`."""
    for observer, callback in _INSTALLED.pop(dispatcher, []):
        observer.handlers[:] = [h for h in observer.handlers if h.callback is not callback]


async def on_startup(ctx: AppContext) -> None:
    install(ctx.dispatcher)


async def on_shutdown(ctx: AppContext) -> None:
    uninstall(ctx.dispatcher)
