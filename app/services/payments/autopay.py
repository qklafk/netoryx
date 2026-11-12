"""Auto-renewal paid from a saved card when the balance is short.

For every subscription due for auto-renewal whose owner cannot cover the plan from the wallet and has an active
saved card of an enabled provider:

1. one short transaction inserts the charge as a ``Payment`` for the missing amount (at least the provider minimum)
   whose id is derived from the renewed period (:func:`app.services.saved_cards.autopay_payment_id`), so a period
   can only ever get one charge, however many times the job runs;
2. the card is charged with no transaction open (the payment id is the provider's idempotency key);
3. the answer goes through the normal crediting path (:func:`service.apply_provider_status`). The post-credit hook
   then renews the subscription from the wallet with the ledger ref of the period, the same ref the wallet
   auto-renewal uses, so the period is charged once even if both race. A pending charge is finished by its webhook
   or the reconciler.

The shortfall rather than the plan price is charged: whatever was already on the balance is used first, exactly as
a wallet auto-renewal would. A charge whose outcome is unknown (network error) stays ``CREATED`` and is retried
with the same idempotency key on the next runs for :data:`RETRY_WINDOW`, then given up (``EXPIRED``; a late
provider confirmation is still credited). A declined charge is not retried for that period; a card the provider
reports as permanently unusable is deactivated.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db.models import Payment, PaymentStatus, Plan, SavedPaymentMethod, Subscription, User
from app.db.session import SessionFactory, read_session
from app.services import saved_cards, subscriptions
from app.services.payments import service
from app.services.payments.base import ChargeResult, GatewayError, PaymentGateway

log = logging.getLogger(__name__)

# CloudPayments keeps idempotency keys for one hour; retry an unknown outcome only well within it.
RETRY_WINDOW = timedelta(minutes=50)


class ChargeOutcome(StrEnum):
    PAID = "paid"  # credited; the post-credit hook renewed the subscription
    PENDING = "pending"  # accepted by the provider, waiting for the final status
    DECLINED = "declined"  # refused; the user is told to top up or use another card
    ERROR = "error"  # unknown outcome; retried on the next run


@dataclass(frozen=True, slots=True)
class CardCharge:
    user_id: int
    subscription_id: int
    payment_id: str
    amount_kop: int
    card_title: str
    outcome: ChargeOutcome
    reason: str | None = None
    card_removed: bool = False  # the provider rejected the card for good; it was deactivated


@dataclass(frozen=True, slots=True)
class _Prepared:
    payment: Payment
    user: User
    token: str
    method_id: int
    title: str
    description: str


def recurring_gateways(gateways: Mapping[str, PaymentGateway]) -> dict[str, Any]:
    """Enabled gateways that can charge a saved card."""
    return {key: g for key, g in gateways.items() if callable(getattr(g, "charge_saved", None))}


async def _candidates(
    session_factory: SessionFactory, settings: Settings, now: datetime, providers: list[str]
) -> list[int]:
    """Subscriptions in the renewal window whose owner has too little balance but an active card."""
    after, until = subscriptions.renew_window(settings, now)
    has_card = (
        select(SavedPaymentMethod.id)
        .where(
            SavedPaymentMethod.user_id == Subscription.user_id,
            SavedPaymentMethod.is_active.is_(True),
            SavedPaymentMethod.provider.in_(providers),
        )
        .exists()
    )
    async with read_session(session_factory) as s:
        rows = await s.scalars(
            select(Subscription)
            .join(User, Subscription.user_id == User.tg_id)
            .join(Plan, Subscription.plan_id == Plan.id)
            .where(
                Subscription.auto_renew.is_(True),
                Subscription.expires_at > after,
                Subscription.expires_at <= until,
                User.is_banned.is_(False),
                User.balance_kop < Plan.price_kop,
                has_card,
            )
            .order_by(Subscription.expires_at, Subscription.id)
        )
        return [sub.id for sub in rows.all() if subscriptions.is_renewable(sub, settings, now)]


async def _prepare(
    session_factory: SessionFactory,
    settings: Settings,
    gateways: Mapping[str, Any],
    sub_id: int,
    now: datetime,
) -> _Prepared | None:
    """Record (or pick up for a retry) the charge of this period; None when there is nothing to charge."""
    try:
        async with session_factory.begin() as s:
            sub = await s.scalar(
                select(Subscription)
                .where(Subscription.id == sub_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if sub is None or not subscriptions.is_renewable(sub, settings, now):
                return None
            user = await s.get(User, sub.user_id, populate_existing=True)
            plan = await s.get(Plan, sub.plan_id) if sub.plan_id is not None else None
            if user is None or user.is_banned or plan is None:
                return None
            payment_id = saved_cards.autopay_payment_id(sub)
            payment = await s.get(Payment, payment_id, populate_existing=True)
            if payment is not None:
                if payment.status is not PaymentStatus.CREATED:
                    return None  # charged (or refused) before for this period
                if payment.created_at < now - RETRY_WINDOW:
                    payment.status = PaymentStatus.EXPIRED
                    log.warning("payment %s: card charge outcome unknown, giving up", payment.id)
                    return None
                method = await s.get(SavedPaymentMethod, payment.saved_method_id) if payment.saved_method_id else None
                if method is None or not method.is_active or method.provider not in gateways:
                    payment.status = PaymentStatus.CANCELED
                    return None
            else:
                shortfall = plan.price_kop - user.balance_kop
                if shortfall <= 0:
                    return None  # the wallet auto-renewal covers it
                method = await saved_cards.pick(s, user.tg_id, list(gateways))
                if method is None:
                    return None
                payment = Payment(
                    id=payment_id,
                    provider=method.provider,
                    user_id=user.tg_id,
                    amount_kop=max(shortfall, int(gateways[method.provider].min_amount_kop)),
                    plan_id=plan.id,
                    subscription_id=sub.id,
                    saved_method_id=method.id,
                    status=PaymentStatus.CREATED,
                )
                s.add(payment)
                await s.flush()
            s.expunge(payment)
            s.expunge(user)
            return _Prepared(
                payment=payment,
                user=user,
                token=method.token,
                method_id=method.id,
                title=method.title,
                description=f"{settings.brand_name}: {plan.title}",
            )
    except IntegrityError:
        return None  # recorded concurrently by another run


async def _store_pending(session_factory: SessionFactory, payment_id: str, provider_payment_id: str | None) -> None:
    async with session_factory.begin() as s:
        await s.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status == PaymentStatus.CREATED)
            .values(status=PaymentStatus.PENDING, provider_payment_id=provider_payment_id, updated_at=datetime.now(UTC))
        )


async def _charge_one(
    session_factory: SessionFactory,
    settings: Settings,
    gateway: Any,
    prepared: _Prepared,
    on_credited: service.OnCredited | None,
) -> CardCharge:
    payment = prepared.payment

    def result(outcome: ChargeOutcome, reason: str | None = None, removed: bool = False) -> CardCharge:
        assert payment.subscription_id is not None
        return CardCharge(
            payment.user_id,
            payment.subscription_id,
            payment.id,
            payment.amount_kop,
            prepared.title,
            outcome,
            reason,
            removed,
        )

    try:
        charge: ChargeResult = await gateway.charge_saved(payment, prepared.user, prepared.token, prepared.description)
    except GatewayError as exc:
        log.warning("payment %s: %s card charge failed: %s", payment.id, payment.provider, exc)
        return result(ChargeOutcome.ERROR, "unavailable")
    except Exception:
        log.exception("payment %s: %s card charge crashed", payment.id, payment.provider)
        return result(ChargeOutcome.ERROR, "unavailable")

    status = charge.status
    if status.status is PaymentStatus.SUCCEEDED:
        await saved_cards.mark_used(session_factory, prepared.method_id)
        await service.apply_provider_status(session_factory, settings, payment, status, on_credited)
        return result(ChargeOutcome.PAID)
    if status.status is PaymentStatus.PENDING:
        await _store_pending(session_factory, payment.id, status.provider_payment_id)
        return result(ChargeOutcome.PENDING)
    await service.mark_status(session_factory, payment.id, PaymentStatus.CANCELED)
    if charge.method_invalid:
        await saved_cards.deactivate_invalid(session_factory, prepared.method_id)
    log.info("payment %s: %s declined the card charge (%s)", payment.id, payment.provider, charge.reason)
    return result(ChargeOutcome.DECLINED, charge.reason, charge.method_invalid)


async def charge_due(
    session_factory: SessionFactory,
    settings: Settings,
    gateways: Mapping[str, PaymentGateway],
    now: datetime,
    on_credited: service.OnCredited | None = None,
) -> list[CardCharge]:
    """Charge saved cards for due auto-renewals the balance cannot cover. Does nothing when saved cards are off."""
    if not settings.saved_cards_enabled:
        return []
    usable = recurring_gateways(gateways)
    if not usable:
        return []
    charges: list[CardCharge] = []
    for sub_id in await _candidates(session_factory, settings, now, list(usable)):
        try:
            prepared = await _prepare(session_factory, settings, usable, sub_id, now)
            if prepared is None:
                continue
            charges.append(
                await _charge_one(session_factory, settings, usable[prepared.payment.provider], prepared, on_credited)
            )
        except Exception:
            log.exception("card auto-renewal of subscription %s failed", sub_id)
    return charges
