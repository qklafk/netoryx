"""Payment flow shared by the bot, the Mini App and other modules: what is being paid, by which provider, and how.

* :func:`build_order` validates a request (amounts in callback data or API bodies are never trusted) and computes
  the amount: a top-up as asked, a plan or a gift for exactly the amount missing on the balance (the post-credit
  hook then buys it from the wallet), a donation in Telegram Stars.
* :func:`available_methods` lists the providers that can take the order.
* :func:`create` persists the payment and creates the invoice (no DB transaction is open during the provider call).
* :func:`send_stars_invoice` / :func:`stars_invoice_link` turn a Telegram Stars payment into an invoice.
* :func:`check` re-checks an open payment with its provider ("I paid" button, Mini App polling).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiogram.types import LabeledPrice
from sqlalchemy import select

from app.db.models import Payment, PaymentProvider, PaymentStatus, Plan, User
from app.db.session import read_session
from app.modules.payments.callbacks import Purpose
from app.services import plans as plans_service
from app.services.payments import service, stars
from app.services.payments.base import GatewayError

if TYPE_CHECKING:
    from aiogram.types import InlineKeyboardMarkup, Message

    from app.config import Settings
    from app.core.context import AppContext
    from app.core.i18n import Translator
    from app.services.payments.base import PaymentGateway

log = logging.getLogger(__name__)

STARS = PaymentProvider.STARS.value
YOOKASSA = PaymentProvider.YOOKASSA.value
MANUAL = PaymentProvider.MANUAL.value
DONATION_STARS = (50, 100, 250, 500, 1000)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")


class OrderError(Exception):
    """The order cannot be paid. ``code``: ``amount``, ``plan``, ``unavailable``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Order:
    """A validated payment request."""

    purpose: Purpose
    amount_kop: int  # what the user pays in rubles (top-up / missing amount / donation value)
    balance_kop: int
    plan: Plan | None = None
    stars: int = 0  # donations: the Stars amount chosen
    recurring: bool = False  # plan via a Telegram Stars subscription renewing every 30 days
    save_card: bool = False  # the user agreed to keep the card for auto-renewal

    @property
    def plan_id(self) -> int | None:
        return self.plan.id if self.plan is not None else None


@dataclass(frozen=True, slots=True)
class Method:
    """One way to pay an order, as offered to the user."""

    key: str
    title: str
    amount_kop: int  # rubles charged by this provider (0 for Stars)
    stars: int = 0  # Stars charged (Telegram Stars only)
    recurring: bool = False


def gateway_title(ctx: AppContext, key: str, lang: str, fallback: str) -> str:
    """Localized provider name (``payments.provider.<key>``), else the gateway's own title."""
    text_key = f"payments.provider.{key}"
    has = getattr(ctx.i18n, "has", None)
    if has is not None and has(text_key):
        return str(ctx.i18n.t(text_key, lang))
    return fallback


def stars_subscription_possible(plan: Plan | None, settings: Settings) -> bool:
    """A plan can be paid by a Telegram Stars subscription: 30 days long and within Telegram's price cap."""
    if plan is None or plan.price_kop <= 0 or plan.duration_days * 86_400 != stars.SUBSCRIPTION_PERIOD:
        return False
    return 0 < plans_service.stars_price(plan, settings) <= stars.MAX_SUBSCRIPTION_STARS


async def _plan_and_balance(ctx: AppContext, user_id: int, plan_id: int | None) -> tuple[Plan | None, int]:
    async with read_session(ctx.session_factory) as session:
        balance = await session.scalar(select(User.balance_kop).where(User.tg_id == user_id))
        plan = await session.get(Plan, plan_id) if plan_id else None
    if balance is None:
        raise OrderError("unavailable")
    return plan, int(balance)


async def build_order(
    ctx: AppContext,
    user_id: int,
    purpose: Purpose | str,
    *,
    amount_kop: int = 0,
    plan_id: int | None = None,
    stars_amount: int = 0,
    recurring: bool = False,
    save_card: bool = False,
) -> Order:
    """Validate a payment request; raises :class:`OrderError`."""
    settings = ctx.settings
    try:
        purpose = Purpose(purpose)
    except ValueError:
        raise OrderError("unavailable") from None
    save_card = save_card and settings.saved_cards_enabled and purpose in (Purpose.PLAN, Purpose.TOPUP)
    plan, balance = await _plan_and_balance(ctx, user_id, plan_id if purpose in (Purpose.PLAN, Purpose.GIFT) else None)
    if purpose is Purpose.TOPUP:
        if not settings.min_topup_kop <= amount_kop <= settings.max_topup_kop:
            raise OrderError("amount")
        return Order(purpose, amount_kop, balance, save_card=save_card)
    if purpose is Purpose.DONATION:
        if not settings.donations_enabled:
            raise OrderError("unavailable")
        if stars_amount not in DONATION_STARS:
            raise OrderError("amount")
        return Order(purpose, stars.stars_to_kop(stars_amount, settings), balance, stars=stars_amount)
    if purpose is Purpose.GIFT and not settings.gifts_enabled:
        raise OrderError("unavailable")
    # Hidden plans are sold only from their own card, opened by the plan's secret link.
    if plan is None or not plan.is_active or plan.is_hidden or plan.price_kop <= 0:
        raise OrderError("plan")
    missing = plan.price_kop - balance
    amount = missing if missing > 0 else plan.price_kop
    wants_recurring = recurring and purpose is Purpose.PLAN and stars_subscription_possible(plan, settings)
    return Order(
        purpose, amount, balance, plan=plan, recurring=wants_recurring, save_card=save_card and not wants_recurring
    )


def can_save_card(settings: Settings, gateway: PaymentGateway | None) -> bool:
    """The provider can keep a card for auto-renewal and the owner allows it."""
    return settings.saved_cards_enabled and gateway is not None and callable(getattr(gateway, "charge_saved", None))


def method_for(order: Order, gateway: PaymentGateway, settings: Settings) -> Method | None:
    """How ``gateway`` would take ``order`` (None if it cannot)."""
    if gateway.key == STARS:
        if order.purpose is Purpose.DONATION:
            return Method(gateway.key, gateway.title, 0, stars=order.stars)
        if order.plan is not None:
            return Method(
                gateway.key,
                gateway.title,
                0,
                stars=plans_service.stars_price(order.plan, settings),
                recurring=order.recurring,
            )
        return Method(gateway.key, gateway.title, 0, stars=stars.kop_to_stars(order.amount_kop, settings))
    if order.purpose is Purpose.DONATION or order.recurring:
        return None  # donations and Stars subscriptions are Telegram Stars only
    if order.save_card and not can_save_card(settings, gateway):
        return None
    if gateway.key == MANUAL:
        return None  # receipts are collected by the plan checkout only; this picker has no receipt step
    amount = order.amount_kop
    if amount < gateway.min_amount_kop:
        if order.purpose is Purpose.TOPUP:
            return None
        amount = gateway.min_amount_kop  # a cart may top up a little more than missing
    return Method(gateway.key, gateway.title, amount)


def available_methods(ctx: AppContext, order: Order) -> list[Method]:
    """Providers that can take ``order``, in the configured order."""
    settings = ctx.settings
    methods = [method_for(order, gateway, settings) for gateway in ctx.gateways.enabled()]
    return [m for m in methods if m is not None]


def needs_receipt_email(settings: Settings, provider: str, user_email: str | None) -> bool:
    """YooKassa receipts (54-FZ) need the buyer's e-mail when the shop has no fixed one."""
    return (
        provider == YOOKASSA
        and settings.yookassa_receipt_enabled
        and not settings.yookassa_receipt_email
        and not user_email
    )


def valid_email(text: str | None) -> str | None:
    """Normalized e-mail if ``text`` looks like one."""
    value = (text or "").strip()
    return value.lower() if len(value) <= 254 and EMAIL_RE.fullmatch(value) else None


def description(_: Translator, order: Order, brand: str) -> str:
    """Payment description shown by the provider (and in the provider's receipt)."""
    plan_title = order.plan.title if order.plan is not None else ""
    if order.purpose is Purpose.PLAN:
        return _("payments.description.plan", brand=brand, plan=plan_title)
    if order.purpose is Purpose.GIFT:
        return _("payments.description.gift", brand=brand, plan=plan_title)
    if order.purpose is Purpose.DONATION:
        return _("payments.description.donation", brand=brand)
    return _("payments.description.topup", brand=brand)


async def create(
    ctx: AppContext, user_id: int, order: Order, gateway: PaymentGateway, *, description_text: str | None = None
) -> Payment:
    """Persist the payment and create the invoice. Raises ``GatewayError`` / ``ValueError`` / ``LookupError``."""
    settings = ctx.settings
    method = method_for(order, gateway, settings)
    if method is None:
        raise ValueError(f"{gateway.key} cannot take this order")
    provider_amount: str | None = None
    if gateway.key == STARS:
        if order.purpose is Purpose.DONATION:
            amount = order.amount_kop
            provider_amount = str(order.stars)
        elif order.plan is not None:
            amount = order.plan.price_kop  # a Stars price is for the whole plan
            provider_amount = str(method.stars)
        else:
            amount = order.amount_kop
    else:
        amount = method.amount_kop
    return await service.create_payment(
        ctx.session_factory,
        settings,
        gateway,
        user_id,
        amount,
        plan_id=order.plan_id,
        is_gift=order.purpose is Purpose.GIFT,
        is_donation=order.purpose is Purpose.DONATION,
        is_recurring=order.recurring and gateway.key == STARS,
        save_method=order.save_card and can_save_card(settings, gateway),
        provider_amount=provider_amount,
        description=description_text,
    )


def _stars_params(_: Translator, payment: Payment, plan: Plan | None, settings: Settings) -> dict[str, Any]:
    params = stars.build_invoice_params(payment, plan, settings)
    brand = settings.brand_name
    if payment.is_donation:
        title, text = _("payments.stars.title_donation", brand=brand), _("payments.stars.text_donation", brand=brand)
    elif plan is not None and payment.is_gift:
        title = _("payments.stars.title_gift", plan=plan.title)
        text = _("payments.stars.text_gift", brand=brand, plan=plan.title, period=_.duration(plan.duration_days))
    elif plan is not None:
        title = _("payments.stars.title_plan", brand=brand, plan=plan.title)
        key = "payments.stars.text_plan_recurring" if payment.is_recurring else "payments.stars.text_plan"
        text = _(key, brand=brand, plan=plan.title, period=_.duration(plan.duration_days))
    else:
        title = _("payments.stars.title_topup")
        text = _("payments.stars.text_topup", brand=brand, amount=_.money(payment.amount_kop))
    params["title"] = title[: stars.MAX_TITLE]
    params["description"] = text[: stars.MAX_DESCRIPTION]
    params["prices"] = [LabeledPrice(label=params["title"], amount=price.amount) for price in params["prices"]]
    return params


async def _payment_plan(ctx: AppContext, payment: Payment) -> Plan | None:
    if payment.plan_id is None:
        return None
    async with read_session(ctx.session_factory) as session:
        return await session.get(Plan, payment.plan_id)


async def send_stars_invoice(
    ctx: AppContext, _: Translator, chat_id: int, payment: Payment, reply_markup: InlineKeyboardMarkup | None = None
) -> Message:
    """Send the invoice message of a one-off Telegram Stars payment."""
    params = _stars_params(_, payment, await _payment_plan(ctx, payment), ctx.settings)
    params.pop("subscription_period", None)
    return await ctx.bot.send_invoice(chat_id=chat_id, reply_markup=reply_markup, **params)


async def stars_invoice_link(ctx: AppContext, _: Translator, payment: Payment) -> str:
    """Invoice link of a Telegram Stars payment (required for subscriptions; the Mini App opens it in place)."""
    params = _stars_params(_, payment, await _payment_plan(ctx, payment), ctx.settings)
    return await ctx.bot.create_invoice_link(**params)


async def get_own(ctx: AppContext, user_id: int, payment_id: str) -> Payment | None:
    """The user's payment by id (None for someone else's or an unknown one)."""
    if not payment_id or len(payment_id) > 36:
        return None
    async with read_session(ctx.session_factory) as session:
        payment = await session.get(Payment, payment_id)
    return payment if payment is not None and payment.user_id == user_id else None


async def check(ctx: AppContext, user_id: int, payment_id: str) -> Payment | None:
    """Re-check an open payment with its provider (crediting it if paid); returns its current state.

    None for an unknown payment or someone else's. Providers that cannot be polled are left to their webhooks.
    """
    payment = await get_own(ctx, user_id, payment_id)
    if payment is None or payment.status not in service.OPEN_STATUSES:
        return payment
    gateway = ctx.gateways.get(payment.provider)
    pollable = gateway is not None and gateway.supports_polling
    if not pollable or not (payment.provider_payment_id or getattr(gateway, "polls_by_payment_id", False)):
        return payment
    assert gateway is not None
    try:
        status = await gateway.fetch(payment)
    except GatewayError as exc:
        log.info("payment %s: status check failed: %s", payment.id, exc)
        return payment
    await service.apply_provider_status(
        ctx.session_factory, ctx.settings, payment, status, on_credited=ctx.on_payment_credited
    )
    return await get_own(ctx, user_id, payment_id)


def is_paid(payment: Payment) -> bool:
    return payment.status in (PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED)
