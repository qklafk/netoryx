"""Telegram Stars (XTR): top-ups, plan payments, donations and native 30-day subscriptions.

There is no provider API to call: the bot sends ``send_invoice`` / ``create_invoice_link`` built by
:func:`build_invoice_params` with ``payload = payment.id``. Telegram then sends a ``pre_checkout_query``
(answer within 10 s using :func:`validate_pre_checkout`) and a ``successful_payment`` message
(credit with :func:`credit_successful_payment`).

Units: a plan payment stores ``amount_kop = plan.price_kop`` and its Stars price in
``Payment.provider_amount``; a Stars top-up credits ``floor(stars * STARS_RUB_PER_STAR * 100)`` kopecks.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Any

from aiogram.types import LabeledPrice
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db.models import Payment, PaymentProvider, PaymentStatus, Subscription
from app.db.session import read_session
from app.services.payments import service
from app.services.payments.base import GatewayError, Invoice, ProviderStatus, WebhookRejected, WebhookResult
from app.services.subscriptions import stars_lapsed
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.config import Settings
    from app.db.models import Plan, User
    from app.db.session import SessionFactory

log = logging.getLogger(__name__)

CURRENCY = "XTR"
SUBSCRIPTION_PERIOD = 2_592_000  # 30 days: the only period Telegram allows
MAX_SUBSCRIPTION_STARS = 10_000
MAX_TITLE = 32
MAX_DESCRIPTION = 255

# Cancels a native Stars subscription of a user (``editUserStarSubscription(is_canceled=True)``).
ChargeCanceler = Callable[[int, str], Awaitable[Any]]


def _rate_kop(settings: Settings) -> Decimal:
    """Kopecks per Star, exact (the float setting is read through its decimal repr)."""
    return Decimal(str(settings.stars_rub_per_star)) * 100


def stars_to_kop(stars: int, settings: Settings) -> int:
    """Wallet credit for ``stars`` Stars, rounded down to a kopeck."""
    return int((stars * _rate_kop(settings)).to_integral_value(rounding=ROUND_DOWN))


def kop_to_stars(amount_kop: int, settings: Settings) -> int:
    """Stars needed to pay ``amount_kop`` (rounded up, at least 1)."""
    return max(1, math.ceil(Decimal(amount_kop) / _rate_kop(settings)))


class StarsGateway:
    key = PaymentProvider.STARS.value
    title = "⭐ Telegram Stars"
    supports_polling = False

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.min_amount_kop = max(1, stars_to_kop(1, settings))

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        """No API call: computes the Stars amount (unless preset) for the bot's invoice."""
        stars = expected_stars(payment, self._settings)
        return Invoice(provider_payment_id=None, url=None, provider_amount=str(stars), provider_currency=CURRENCY)

    async def fetch(self, payment: Payment) -> ProviderStatus:
        raise GatewayError("Telegram Stars payments arrive as bot updates and cannot be polled")

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        raise WebhookRejected("Telegram Stars has no webhooks", 404)

    async def close(self) -> None:
        return None


def expected_stars(payment: Payment, settings: Settings) -> int:
    """Stars this payment must be paid with: the preset ``provider_amount`` or the converted amount."""
    if payment.provider_amount and payment.provider_amount.isdigit() and int(payment.provider_amount) > 0:
        return int(payment.provider_amount)
    return kop_to_stars(payment.amount_kop, settings)


def _texts(payment: Payment, plan: Plan | None, settings: Settings) -> tuple[str, str]:
    """RU invoice title and description."""
    brand = settings.brand_name
    if payment.is_donation:
        return f"Поддержать {brand}", f"Добровольное пожертвование проекту {brand}. Спасибо!"
    if plan is not None and payment.is_gift:
        return f"Подарок: {plan.title}", f"Подарочный код на подписку {brand} «{plan.title}» ({plan.duration_days} дн.)"
    if plan is not None:
        return f"{brand}: {plan.title}", f"Подписка {brand} «{plan.title}» на {plan.duration_days} дн."
    rubles = payment.amount_kop / 100
    return "Пополнение баланса", f"Пополнение баланса {brand} на {rubles:g} ₽"


def build_invoice_params(payment: Payment, plan: Plan | None, settings: Settings) -> dict[str, Any]:
    """Keyword arguments for ``bot.send_invoice`` / ``bot.create_invoice_link``.

    Recurring payments include ``subscription_period`` and must be sent with ``create_invoice_link``
    (``send_invoice`` does not accept it). Raises ValueError if a subscription exceeds Telegram's limit.
    """
    stars = expected_stars(payment, settings)
    title, description = _texts(payment, plan, settings)
    title = title[:MAX_TITLE]
    params: dict[str, Any] = {
        "title": title,
        "description": description[:MAX_DESCRIPTION],
        "payload": payment.id,
        "currency": CURRENCY,
        "prices": [LabeledPrice(label=title, amount=stars)],
        "provider_token": "",
    }
    if payment.is_recurring:
        if stars > MAX_SUBSCRIPTION_STARS:
            raise ValueError(f"a Stars subscription costs at most {MAX_SUBSCRIPTION_STARS} Stars")
        params["subscription_period"] = SUBSCRIPTION_PERIOD
    return params


async def validate_pre_checkout(
    session_factory: SessionFactory,
    payload: str,
    total_amount: int,
    user_id: int,
    *,
    currency: str = CURRENCY,
    settings: Settings | None = None,
) -> bool:
    """Cheap pre-checkout check: our open Stars payment, same user, same Stars amount, XTR.

    A new Stars subscription is declined while the user's subscription is renewed by another one that is still
    running: only the linked one can be canceled from the bot, a second one would keep charging.
    """
    if currency != CURRENCY or not payload or len(payload) > 36:
        return False
    async with read_session(session_factory) as s:
        payment = await s.get(Payment, payload)
        if payment is None or payment.provider != PaymentProvider.STARS or payment.user_id != user_id:
            return False
        if payment.status not in service.OPEN_STATUSES:
            return False
        if payment.is_recurring and not payment.is_gift and not payment.is_donation:
            from app.services import subscriptions

            sub = (
                None
                if payment.new_subscription
                else await subscriptions.get_for_user(s, user_id, payment.subscription_id)
            )
            if sub is not None and sub.stars_charge_id is not None and not stars_lapsed(sub, utcnow()):
                log.info("stars: user %s already has a Stars subscription; a second one is declined", user_id)
                return False
    if payment.provider_amount and payment.provider_amount.isdigit():
        expected = int(payment.provider_amount)
    elif settings is not None:
        expected = kop_to_stars(payment.amount_kop, settings)
    else:
        return False
    return total_amount == expected


async def credit_successful_payment(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    payload: str,
    user_id: int,
    total_amount: int,
    charge_id: str,
    currency: str = CURRENCY,
    is_recurring: bool = False,
    is_first_recurring: bool = False,
    on_credited: service.OnCredited | None = None,
    cancel_charge: ChargeCanceler | None = None,
) -> service.CreditResult | None:
    """Credit a ``successful_payment`` update; None if it was already processed or is not ours.

    Deduplicated on ``telegram_payment_charge_id``. A subscription renewal (``is_recurring`` and not
    ``is_first_recurring``) carries the original payload; it is recorded as a new payment. A second,
    distinct charge for an invoice that is already paid (the same invoice confirmed on two devices)
    is recorded as a separate wallet top-up (``payment.repeat`` event), so no paid Stars go unaccounted.

    ``on_credited`` (the post-credit hook that buys the plan) runs here, before the user's
    subscription is linked to the native Stars subscription (:func:`link_recurring`) so that wallet
    auto-renewal skips it; a first purchase creates the subscription only inside the hook.
    ``CreditResult.recurring_charge_id`` names the Stars subscription for recurring payments.

    The user's subscription is linked to one Stars subscription at a time. A new one replaces the linked one, and
    a renewal of a Stars subscription other than the linked one does not take the link over; either way the
    Stars subscription left unlinked is canceled through ``cancel_charge(user_id, charge_id)`` (awaited outside
    of any transaction), since nothing in the bot could stop it any more.
    """
    if currency != CURRENCY or not charge_id:
        log.warning("stars: ignoring payment with currency %r", currency)
        return None
    async with read_session(session_factory) as s:
        original = await s.get(Payment, payload) if payload and len(payload) <= 36 else None
    if original is None or original.provider != PaymentProvider.STARS or original.user_id != user_id:
        log.warning("stars: successful_payment %s does not match a payment of user %s", charge_id, user_id)
        return None

    payment = original
    if is_recurring and not is_first_recurring:
        payment = await _record_renewal(session_factory, original, charge_id)

    expected = expected_stars(payment, settings)
    paid_kop = payment.amount_kop if total_amount == expected else stars_to_kop(total_amount, settings)
    result = await service.credit(
        session_factory, settings, payment.id, paid_amount_kop=paid_kop, provider_payment_id=charge_id
    )
    if result is None and payment is original:
        repeat = await _record_repeat_charge(session_factory, original.id, charge_id, paid_kop, total_amount)
        if repeat is None:
            return None  # a redelivery of the charge that was credited
        payment, is_recurring = repeat, False  # a plain top-up: not linked to a Stars subscription
        result = await service.credit(
            session_factory, settings, payment.id, paid_amount_kop=paid_kop, provider_payment_id=charge_id
        )
    if result is None:
        return None
    if is_recurring:
        # Renewals reference the subscription by its first charge (stored on the original payment).
        first_charge = charge_id if payment is original else (original.provider_payment_id or charge_id)
        result = replace(result, recurring_charge_id=first_charge)
    if on_credited is not None:
        await service._notify(on_credited, result)
    if result.recurring_charge_id and payment.plan_id is not None and not payment.is_gift and not payment.is_donation:
        async with read_session(session_factory) as session:
            stored = await session.get(Payment, payment.id)
            target_id = stored.subscription_id if stored else None
        _, orphan = await _link(
            session_factory,
            user_id,
            result.recurring_charge_id,
            take_over=payment is original,
            subscription_id=target_id,
        )
        if orphan is not None:
            await _cancel_orphan(cancel_charge, user_id, orphan)
    return result


async def link_recurring(
    session_factory: SessionFactory, user_id: int, charge_id: str, subscription_id: int | None = None
) -> bool:
    """Mark the user's subscription as renewed by a native Stars subscription (no wallet auto-renew).

    Replaces a previously linked Stars subscription (which the caller should cancel). Returns False if the user
    has no subscription yet.
    """
    linked, _ = await _link(session_factory, user_id, charge_id, take_over=True, subscription_id=subscription_id)
    return linked


async def _link(
    session_factory: SessionFactory,
    user_id: int,
    charge_id: str,
    *,
    take_over: bool,
    subscription_id: int | None = None,
) -> tuple[bool, str | None]:
    """``(linked, Stars subscription left unlinked)``.

    With ``take_over`` (a new Stars subscription) ``charge_id`` replaces the linked one; otherwise (a renewal)
    it is linked only if no other one is, and is itself the one left unlinked.
    """
    async with session_factory.begin() as s:
        from app.services import subscriptions

        sub = await subscriptions.lock_for_user(s, user_id, subscription_id)
        if sub is None:
            return False, None
        current = sub.stars_charge_id
        if current is not None and current != charge_id and not take_over:
            log.warning(
                "stars: renewal of %s for user %s while %s is linked: not linking it", charge_id, user_id, current
            )
            return False, charge_id
        sub.stars_charge_id = charge_id
    if current != charge_id:
        log.info("stars: subscription of user %s linked to Stars subscription %s", user_id, charge_id)
    if current is not None and current != charge_id:
        log.warning("stars: Stars subscription %s of user %s replaced by %s", current, user_id, charge_id)
        return True, current
    return True, None


async def _cancel_orphan(cancel_charge: ChargeCanceler | None, user_id: int, charge_id: str) -> None:
    if cancel_charge is None:
        log.warning("stars: unlinked Stars subscription %s of user %s was not canceled", charge_id, user_id)
        return
    try:
        await cancel_charge(user_id, charge_id)
    except Exception:
        log.exception("stars: canceling the unlinked Stars subscription %s of user %s failed", charge_id, user_id)


async def unlink_recurring(session_factory: SessionFactory, user_id: int, charge_id: str | None = None) -> bool:
    """Forget the native Stars subscription (canceled or lapsed): wallet auto-renew applies again.

    With ``charge_id`` only that Stars subscription is unlinked. Returns whether anything changed.
    """
    stmt = update(Subscription).where(Subscription.user_id == user_id, Subscription.stars_charge_id.is_not(None))
    if charge_id is not None:
        stmt = stmt.where(Subscription.stars_charge_id == charge_id)
    async with session_factory.begin() as s:
        result = await s.execute(stmt.values(stars_charge_id=None))
    return result.rowcount == 1  # type: ignore[attr-defined]


async def _record_repeat_charge(
    session_factory: SessionFactory, payment_id: str, charge_id: str, paid_kop: int, stars: int
) -> Payment | None:
    """The payment row for a further charge on an already paid invoice; None if ``charge_id`` is the one credited."""
    async with read_session(session_factory) as s:
        original = await s.get(Payment, payment_id)
    if original is None or original.status not in (PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED):
        return None
    if original.provider_payment_id in (None, charge_id):
        return None
    log.warning("stars: second charge %s for the already paid invoice %s", charge_id, payment_id)
    return await service.record_repeat(session_factory, original, charge_id, paid_kop, provider_amount=str(stars))


async def _record_renewal(session_factory: SessionFactory, original: Payment, charge_id: str) -> Payment:
    """The payment row for a subscription renewal charge, inserted (PENDING) on first sight.

    A redelivered charge returns the existing row; crediting it again is a no-op unless the
    previous attempt crashed between recording and crediting.
    """
    renewal = Payment(
        id=str(uuid.uuid4()),
        provider=original.provider,
        provider_payment_id=charge_id,
        user_id=original.user_id,
        amount_kop=original.amount_kop,
        plan_id=original.plan_id,
        # Native Stars renewals reuse the first invoice terms, including its personal discount.
        plan_price_kop=original.plan_price_kop,
        subscription_id=original.subscription_id,
        is_gift=False,
        is_donation=original.is_donation,
        is_recurring=True,
        provider_amount=original.provider_amount,
        provider_currency=CURRENCY,
        status=PaymentStatus.PENDING,
    )
    try:
        async with session_factory.begin() as s:
            s.add(renewal)
    except IntegrityError:  # UNIQUE(provider, provider_payment_id): this charge was seen already
        async with read_session(session_factory) as s:
            existing = await s.scalar(
                select(Payment).where(Payment.provider == original.provider, Payment.provider_payment_id == charge_id)
            )
        if existing is None:
            raise
        log.info("stars: renewal charge %s already recorded as %s", charge_id, existing.id)
        return existing
    return renewal
