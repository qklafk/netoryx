"""Buying a plan: plans list, plan card, payment from the wallet or a provider invoice for the missing amount.

A provider payment carries ``plan_id``: once it is credited, the post-payment hook buys the plan from the wallet
(balance + the credited amount), so the user pays exactly what is missing. Telegram Stars pays the full price in
Stars (``Plan.price_stars`` or the converted price).
"""

from __future__ import annotations

import logging
import re
import secrets

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.core.context import AppContext
from app.core.filters import IsPrivate
from app.core.i18n import Translator
from app.core.ui import render
from app.db.models import Payment, PaymentStatus, Plan
from app.db.session import read_session
from app.keyboards import Nav
from app.modules.payments.helpers import TEXT_INPUT, input_data, start_input
from app.modules.subscription import views
from app.modules.subscription.bot import Event, alert, require_state, show_card, sync_now
from app.modules.subscription.callbacks import BUY, CheckCb, PayCb, PlanCb
from app.modules.subscription.state import load_state, offered_plans, select_subscription
from app.services import admins, promo, subscriptions, users
from app.services import plans as plans_service
from app.services.payments import service as payments
from app.services.payments import stars
from app.services.payments.base import GatewayError, PaymentGateway
from app.services.subscriptions import SubErrorCode, SubscriptionError

log = logging.getLogger(__name__)

router = Router(name="subscription_checkout")
# User screens live in the private chat only: the access gates do not look at group chats.
router.message.filter(IsPrivate())
router.callback_query.filter(IsPrivate())

NONCE_BYTES = 6
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}")
MAX_EMAIL_LENGTH = 256
PAYMENTS_MODULE = "payments"


class Checkout(StatesGroup):
    email = State()  # YooKassa receipts need the buyer's e-mail
    receipt = State()


def new_nonce() -> str:
    return secrets.token_urlsafe(NONCE_BYTES)


async def under_maintenance(ctx: AppContext, settings: Settings, name: str, user_id: int | None) -> bool:
    """``name`` is switched off for this user by a partial maintenance (staff are exempt, as in the module guard)."""
    if not await ctx.maintenance.module_blocked(name, user_id, is_admin=False):
        return False
    if user_id is None:
        return True
    async with read_session(ctx.session_factory) as session, session.begin():
        return await admins.resolve_role(session, settings, user_id) is None


async def payments_on(ctx: AppContext, settings: Settings, user_id: int | None) -> bool:
    """Provider payments are confirmed (webhooks, reconcile, Stars pre-checkout) only while that module is on;
    new invoices are not offered while it is under a partial maintenance either."""
    if not ctx.modules.is_enabled(PAYMENTS_MODULE, settings):
        return False
    return not await under_maintenance(ctx, settings, PAYMENTS_MODULE, user_id)


async def provider_gateway(ctx: AppContext, settings: Settings, key: str, user_id: int | None) -> PaymentGateway | None:
    """The enabled gateway ``key``, or None while provider payments are off for this user."""
    return ctx.gateways.get(key) if await payments_on(ctx, settings, user_id) else None


def needs_email(gateway: PaymentGateway, settings: Settings, receipt_email: str | None) -> bool:
    """YooKassa 54-FZ receipts need an e-mail: the shop's fixed one or the buyer's."""
    return (
        gateway.key == "yookassa"
        and settings.yookassa_receipt_enabled
        and not settings.yookassa_receipt_email
        and not receipt_email
    )


def invoice_amount(price_kop: int, balance_kop: int, gateway: PaymentGateway) -> int:
    """What to charge through ``gateway``: the missing part of the price, at least the provider's minimum."""
    if gateway.key == views.STARS:
        return price_kop
    return max(price_kop - max(balance_kop, 0), gateway.min_amount_kop)


# --- plans ------------------------------------------------------------------------------------------------------------
@router.message(Command("buy"))
@router.callback_query(Nav.filter(F.to == BUY))
async def open_plans(
    event: Event, ctx: AppContext, settings: Settings, _: Translator, callback_data: Nav | None = None
) -> None:
    """Choose a renewal target before showing plans when the user has subscriptions."""
    if settings.app_mode != "shop":
        await alert(event, _("subscription.plans.unavailable"))
        return
    raw_target = callback_data.arg if callback_data is not None else ""
    if raw_target == "-1":
        await show_plans(event, ctx, settings, _, target=-1)
        return
    if raw_target.isdecimal() and int(raw_target) > 0:
        await show_plans(event, ctx, settings, _, target=int(raw_target))
        return
    state = await require_state(event, ctx, _)
    if state is None:
        return
    if state.total_subscriptions > 1:
        async with ctx.session_factory() as session:
            items = await subscriptions.list_for_user(session, state.user.tg_id)
        await render(event, views.purchase_targets(_, items))
        return
    if state.sub is not None:
        # Keep the familiar one-subscription checkout short.  Creating an
        # additional subscription remains available from the subscriptions
        # screen (its explicit "new" action carries the -1 sentinel).
        await show_plans(event, ctx, settings, _, target=state.sub.id)
        return
    await show_plans(event, ctx, settings, _, target=-1)


async def show_plans(event: Event, ctx: AppContext, settings: Settings, _: Translator, *, target: int = 0) -> None:
    if settings.app_mode != "shop":
        await alert(event, _("subscription.plans.unavailable"))
        return
    state = await require_state(event, ctx, _, target if target > 0 else None)
    if state is None:
        return
    plans = await offered_plans(ctx.session_factory, state.user.tg_id)
    async with read_session(ctx.session_factory) as session:
        discount = await promo.personal_discount(session, state.user.tg_id)
    prices = {plan.id: discount.price(plan.price_kop) for plan in plans}
    await render(event, views.plans_screen(_, state, plans, target=target, prices=prices))


async def plan_for_sale(ctx: AppContext, settings: Settings, plan_id: int, token: str, user_id: int) -> Plan | None:
    """An active tariff available to this user; hidden tariffs also need their signed link token."""
    async with read_session(ctx.session_factory) as session, session.begin():
        plan = await plans_service.get(session, plan_id)
        user = await users.get(session, user_id)
        if plan is None or user is None or not plan.is_active:
            return None
        paid = await plans_service.has_paid_before(session, user_id)
    if not plans_service.available_to(plan, user, paid=paid):
        return None
    if plan.is_hidden and not plans_service.link_token_valid(ctx.secret_box, plan.id, token):
        return None
    return plan


async def discounted_price(ctx: AppContext, user_id: int, plan: Plan) -> int:
    """Resolve a buyer-specific tariff price immediately before rendering or charging it."""
    async with read_session(ctx.session_factory) as session:
        return (await promo.personal_discount(session, user_id)).price(plan.price_kop)


@router.callback_query(PlanCb.filter())
async def open_plan(
    callback: CallbackQuery, callback_data: PlanCb, ctx: AppContext, settings: Settings, _: Translator
) -> None:
    await show_plan(callback, ctx, settings, _, callback_data.id, callback_data.t, target=callback_data.s)


async def show_plan(
    event: Event,
    ctx: AppContext,
    settings: Settings,
    _: Translator,
    plan_id: int,
    token: str = "",
    *,
    target: int = 0,
) -> None:
    """A plan's card; also opened by ``/start plan_<id>`` (hidden plans: ``plan_<id>-<token>``)."""
    if settings.app_mode != "shop":
        await alert(event, _("subscription.plans.unavailable"))
        return
    if event.from_user is None:
        return
    plan = await plan_for_sale(ctx, settings, plan_id, token, event.from_user.id)
    if plan is None:
        await alert(event, _("subscription.plans.gone"))
        await show_plans(event, ctx, settings, _, target=target)
        return
    state = await require_state(event, ctx, _, target if target > 0 else None)
    if state is None:
        return
    user_id = event.from_user.id if event.from_user else None
    pay_on = await payments_on(ctx, settings, user_id)
    gateways = ctx.gateways.enabled() if pay_on else []
    stars_on = pay_on and ctx.gateways.get(views.STARS) is not None
    price_kop = await discounted_price(ctx, event.from_user.id, plan)
    stars_price = plans_service.discounted_stars_price(plan, price_kop, settings) if stars_on else None
    wallet = ctx.modules.is_enabled("wallet", settings)
    screen = views.plan_screen(
        _,
        state,
        plan,
        gateways,
        nonce=new_nonce(),
        stars_price=stars_price,
        wallet_enabled=wallet,
        price_kop=price_kop,
        token=token if plan.is_hidden else "",
        target=target,
    )
    await render(event, screen)


# --- paying from the wallet -------------------------------------------------------------------------------------------
@router.callback_query(PayCb.filter(F.via == views.BALANCE))
async def pay_from_balance(
    callback: CallbackQuery, callback_data: PayCb, ctx: AppContext, settings: Settings, _: Translator
) -> None:
    if settings.app_mode != "shop":
        await alert(callback, _("subscription.plans.unavailable"))
        return
    plan = await plan_for_sale(ctx, settings, callback_data.plan, callback_data.t, callback.from_user.id)
    if plan is None:
        await alert(callback, _("subscription.plans.gone"))
        await show_plans(callback, ctx, settings, _)
        return
    # The nonce of the rendered button makes a double tap charge once (ledger ref "purchase:tap-<nonce>").
    payment_ref = f"tap-{callback_data.n or new_nonce()}"
    try:
        async with ctx.session_factory.begin() as session:
            user = await users.get(session, callback.from_user.id)
            if user is None:
                raise SubscriptionError(SubErrorCode.NO_PLAN)
            target = callback_data.s if callback_data.s > 0 else None
            purchase = await subscriptions.purchase_plan(
                session,
                settings,
                user,
                plan,
                payment_id=payment_ref,
                subscription_id=target,
                new_subscription=callback_data.s == -1,
                label=plan.title if callback_data.s == -1 else None,
                price_kop=(await promo.personal_discount(session, user.tg_id)).price(plan.price_kop),
            )
            sub_id = purchase.subscription_id
            log.info("user %s bought plan %s from the wallet (purchase %s)", user.tg_id, plan.id, purchase.id)
    except SubscriptionError as exc:
        await _purchase_failed(callback, ctx, settings, _, plan, exc.code, callback_data.t, callback_data.s)
        return
    synced = await sync_now(ctx, settings, sub_id) if sub_id is not None else False
    if sub_id is not None:
        await select_subscription(ctx.session_factory, callback.from_user.id, sub_id)
    state = await load_state(ctx.session_factory, callback.from_user.id, subscription_id=sub_id)
    if state is None:
        return
    await render(callback, views.purchased(_, plan, state, synced=synced))


async def _purchase_failed(
    callback: CallbackQuery,
    ctx: AppContext,
    settings: Settings,
    _: Translator,
    plan: Plan,
    code: SubErrorCode,
    token: str,
    target: int = 0,
) -> None:
    key = {
        SubErrorCode.INSUFFICIENT_FUNDS: "subscription.error.insufficient",
        SubErrorCode.DISABLED: "subscription.card.disabled_hint",
        SubErrorCode.BANNED: "subscription.error.banned",
    }.get(code, "subscription.plans.gone")
    await alert(callback, _(key))
    if code == SubErrorCode.INSUFFICIENT_FUNDS:
        await show_plan(callback, ctx, settings, _, plan.id, token, target=target)
    elif code == SubErrorCode.DISABLED:
        await show_card(callback, ctx, settings, _)


# --- paying through a provider ----------------------------------------------------------------------------------------
@router.callback_query(PayCb.filter(F.via != views.BALANCE))
async def pay_with_provider(
    callback: CallbackQuery,
    callback_data: PayCb,
    ctx: AppContext,
    settings: Settings,
    _: Translator,
    state: FSMContext,
) -> None:
    gateway = await provider_gateway(ctx, settings, callback_data.via, callback.from_user.id)
    if settings.app_mode != "shop" or gateway is None:
        await alert(callback, _("subscription.pay.unavailable"))
        await show_plan(callback, ctx, settings, _, callback_data.plan, callback_data.t)
        return
    user_state = await require_state(callback, ctx, _)
    if user_state is None:
        return
    if needs_email(gateway, settings, user_state.user.receipt_email):
        await start_input(
            state, Checkout.email, plan=callback_data.plan, via=gateway.key, t=callback_data.t, s=callback_data.s
        )
        await render(callback, views.ask_email(_))
        return
    await create_invoice(
        callback, ctx, settings, _, callback_data.plan, gateway, callback_data.t, target=callback_data.s, state=state
    )


@router.message(Checkout.email, TEXT_INPUT)
async def receive_email(
    message: Message, ctx: AppContext, settings: Settings, _: Translator, state: FSMContext
) -> None:
    data = await input_data(state)  # an abandoned step has expired: the text goes to other handlers
    email = (message.text or "").strip()
    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL.fullmatch(email):
        await message.answer(_("subscription.email.invalid"))
        return
    await state.clear()
    if message.from_user is None:
        return
    async with ctx.session_factory.begin() as session:
        user = await users.get(session, message.from_user.id)
        if user is not None:
            user.receipt_email = email
    gateway = await provider_gateway(ctx, settings, str(data.get("via", "")), message.from_user.id)
    plan_id = data.get("plan")
    if gateway is None or not isinstance(plan_id, int):
        await show_plans(message, ctx, settings, _)
        return
    target = data.get("s")
    await create_invoice(
        message,
        ctx,
        settings,
        _,
        plan_id,
        gateway,
        str(data.get("t", "")),
        target=target if isinstance(target, int) else 0,
    )


async def create_invoice(
    event: Event,
    ctx: AppContext,
    settings: Settings,
    _: Translator,
    plan_id: int,
    gateway: PaymentGateway,
    token: str = "",
    *,
    target: int = 0,
    state: FSMContext | None = None,
) -> None:
    """Create the payment for the missing amount (Stars: the full price) and show / send the invoice."""
    if event.from_user is None:
        return
    plan = await plan_for_sale(ctx, settings, plan_id, token, event.from_user.id)
    user_state = await require_state(event, ctx, _, target if target > 0 else None)
    if plan is None or user_state is None:
        if plan is None:
            await alert(event, _("subscription.plans.gone"))
        return
    balance = user_state.user.balance_kop
    price_kop = await discounted_price(ctx, event.from_user.id, plan)
    if gateway.key != views.STARS and balance >= price_kop:
        # The balance changed after the plan card was rendered.
        await show_plan(event, ctx, settings, _, plan.id, token, target=target)
        return
    is_stars = gateway.key == views.STARS
    try:
        payment = await payments.create_payment(
            ctx.session_factory,
            settings,
            gateway,
            user_state.user.tg_id,
            invoice_amount(price_kop, balance, gateway),
            plan_id=plan.id,
            plan_price_kop=price_kop,
            provider_amount=(
                str(plans_service.discounted_stars_price(plan, price_kop, settings)) if is_stars else None
            ),
            description=_("subscription.invoice.description", brand=settings.brand_name, plan=plan.title),
            subscription_id=target if target > 0 else None,
            new_subscription=target == -1,
            subscription_label=plan.title if target == -1 else None,
        )
    except (GatewayError, ValueError, LookupError) as exc:
        log.warning("invoice for plan %s via %s failed: %s", plan.id, gateway.key, exc)
        await alert(event, _("subscription.pay.failed"))
        return
    if is_stars:
        await _send_stars_invoice(event, ctx, settings, _, payment, plan)
        return
    if gateway.key == "manual":
        if state is None:
            await alert(event, _("subscription.pay.failed"))
            return
        await start_input(state, Checkout.receipt, payment_id=payment.id)
        await render(event, views.manual_transfer_screen(_, payment, plan, settings.manual_transfer_details or ""))
        return
    await render(event, views.invoice_screen(_, settings, payment, plan, gateway))


@router.message(Checkout.receipt, F.photo | F.document)
async def receive_manual_receipt(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    if message.from_user is None:
        return
    data = await input_data(state)
    payment_id = str(data.get("payment_id", ""))
    photo = message.photo[-1] if message.photo else None
    document = message.document
    file_id = photo.file_id if photo is not None else document.file_id if document is not None else None
    kind = "photo" if photo is not None else "document" if document is not None else None
    if not file_id or kind is None:
        await message.answer(_("subscription.manual.unsupported"))
        return
    async with ctx.session_factory.begin() as session:
        payment = await session.get(Payment, payment_id)
        if (
            payment is None
            or payment.user_id != message.from_user.id
            or payment.provider != "manual"
            or payment.status not in (PaymentStatus.CREATED, PaymentStatus.PENDING)
        ):
            await state.clear()
            await message.answer(_("error.expired_button"))
            return
        payment.receipt_file_id = file_id
        payment.receipt_media_type = kind
    await state.clear()
    await message.answer(_("subscription.manual.sent"))


@router.message(Checkout.receipt)
async def receive_unsupported_manual_receipt(message: Message, _: Translator) -> None:
    await message.answer(_("subscription.manual.unsupported"))


async def _send_stars_invoice(
    event: Event, ctx: AppContext, settings: Settings, _: Translator, payment: Payment, plan: Plan
) -> None:
    chat_id = event.from_user.id if event.from_user else payment.user_id
    try:
        params = stars.build_invoice_params(payment, plan, settings)
        await ctx.bot.send_invoice(chat_id=chat_id, **params)
    except Exception:
        log.exception("sending the Stars invoice for payment %s failed", payment.id)
        await payments.mark_status(ctx.session_factory, payment.id, PaymentStatus.CANCELED)
        await alert(event, _("subscription.pay.failed"))
        return
    await render(event, views.stars_invoice_sent(_, plan))


# --- payment status ---------------------------------------------------------------------------------------------------
@router.callback_query(CheckCb.filter())
async def check_payment(
    callback: CallbackQuery, callback_data: CheckCb, ctx: AppContext, settings: Settings, _: Translator
) -> None:
    async with read_session(ctx.session_factory) as session, session.begin():
        payment = await session.get(Payment, callback_data.id)
    if payment is None or payment.user_id != callback.from_user.id:
        await alert(callback, _("error.expired_button"))
        return
    if payment.status == PaymentStatus.SUCCEEDED:
        await show_card(
            callback, ctx, settings, _, header=_("subscription.pay.received"), subscription_id=payment.subscription_id
        )
        return
    if payment.status in (PaymentStatus.CANCELED, PaymentStatus.EXPIRED, PaymentStatus.REFUNDED):
        await alert(callback, _("subscription.pay.expired"))
        return
    gateway = ctx.gateways.get(payment.provider)
    if gateway is not None and gateway.supports_polling:
        try:
            status = await gateway.fetch(payment)
            credited = await payments.apply_provider_status(
                ctx.session_factory, settings, payment, status, ctx.on_payment_credited
            )
        except GatewayError as exc:
            log.info("payment %s: status check failed: %s", payment.id, exc)
            credited = None
        if credited is not None:
            async with read_session(ctx.session_factory) as session, session.begin():
                completed = await session.get(Payment, payment.id)
            await show_card(
                callback,
                ctx,
                settings,
                _,
                header=_("subscription.pay.received"),
                subscription_id=completed.subscription_id if completed is not None else payment.subscription_id,
            )
            return
    await alert(callback, _("subscription.pay.not_yet"))
