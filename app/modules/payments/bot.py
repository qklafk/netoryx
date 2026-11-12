"""Payment screens: method picker, invoices (provider page or Telegram Stars), "check payment", donations."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select, update

from app.core.context import AppContext
from app.core.filters import IsPrivate
from app.core.i18n import Translator
from app.core.ui import Screen, answer_callback, delete_message, render
from app.db.models import User
from app.db.session import read_session
from app.keyboards import Button, grid, kb, nav, to_menu
from app.keyboards.callbacks import Nav
from app.modules.payments import flow
from app.modules.payments import onchain as onchain_view
from app.modules.payments.callbacks import PayCb, PayCheckCb, Purpose
from app.modules.payments.helpers import TEXT_INPUT, input_data, start_input
from app.services.payments import onchain
from app.services.payments.base import GatewayError
from app.services.payments.service import OPEN_STATUSES

log = logging.getLogger(__name__)

router = Router(name="payments")
# User screens live in the private chat only: the access gates do not look at group chats.
router.message.filter(IsPrivate())
router.callback_query.filter(IsPrivate())

DONATE_TARGETS = frozenset({"donate", "donation"})
_ORDER_ERRORS = {"amount": "payments.error.amount", "plan": "payments.error.plan"}
SAVE_CARD_PURPOSES = (Purpose.PLAN, Purpose.TOPUP)
SAVE_CARD = 2  # PayCb.r: pay and keep the card for auto-renewal


class PayInput(StatesGroup):
    email = State()


# --- screens ----------------------------------------------------------------------------------------------------------
def _back_row(_: Translator, order: flow.Order) -> list[Any]:
    if order.purpose is Purpose.TOPUP:
        return [nav(_("nav.back"), "topup"), to_menu(_)]
    if order.purpose is Purpose.GIFT:
        return [nav(_("nav.back"), "gift"), to_menu(_)]
    if order.purpose is Purpose.DONATION:
        return [nav(_("nav.back"), "donate"), to_menu(_)]
    return [to_menu(_)]


def _method_label(_: Translator, method: flow.Method, title: str, order: flow.Order) -> str:
    if method.recurring:
        return _("payments.method.stars_recurring", stars=_.number(method.stars))
    if method.stars:
        return _("payments.method.stars", title=title, stars=_.number(method.stars))
    if method.amount_kop != order.amount_kop:
        return _("payments.method.with_amount", title=title, amount=_.money(method.amount_kop))
    return title


def _order_text(_: Translator, order: flow.Order) -> str:
    plan = order.plan
    if order.purpose is Purpose.TOPUP:
        return _("payments.methods.topup", amount=_.money(order.amount_kop))
    if order.purpose is Purpose.DONATION:
        return _("payments.methods.donation", stars=_.number(order.stars))
    assert plan is not None
    key = "payments.methods.gift" if order.purpose is Purpose.GIFT else "payments.methods.plan"
    text = _(key, plan=_.esc(plan.title), period=_.duration(plan.duration_days), price=_.money(plan.price_kop))
    if 0 < order.balance_kop < plan.price_kop:
        text += "\n" + _(
            "payments.methods.missing", balance=_.money(order.balance_kop), amount=_.money(order.amount_kop)
        )
    return text + "\n\n" + _("payments.methods.choose")


def methods_screen(ctx: AppContext, _: Translator, order: flow.Order) -> Screen:
    """The payment method picker for a validated order."""
    rows: list[list[Any]] = []
    for method in flow.available_methods(ctx, order):
        gateway = ctx.gateways.get(method.key)
        title = flow.gateway_title(ctx, method.key, _.lang, gateway.title if gateway else method.key)
        data = PayCb(
            a="go",
            k=order.purpose.value,
            amt=order.stars if order.purpose is Purpose.DONATION else order.amount_kop,
            plan=order.plan_id or 0,
            prov=method.key,
        )
        rows.append([Button.cb(_method_label(_, method, title, order), data)])
        if order.purpose in SAVE_CARD_PURPOSES and flow.can_save_card(ctx.settings, gateway):
            keep = PayCb(
                a="go",
                k=order.purpose.value,
                amt=order.amount_kop,
                plan=order.plan_id or 0,
                prov=method.key,
                r=SAVE_CARD,
            )
            rows.append([Button.cb(_("payments.method.save_card", title=title), keep)])
        if gateway is not None and method.key == flow.STARS and order.purpose is Purpose.PLAN:
            recurring = flow.method_for(replace(order, recurring=True), gateway, ctx.settings)
            if recurring is not None and flow.stars_subscription_possible(order.plan, ctx.settings):
                data = PayCb(a="go", k=order.purpose.value, plan=order.plan_id or 0, prov=flow.STARS, r=1)
                rows.append([Button.cb(_method_label(_, recurring, title, order), data)])
    if not rows:
        return Screen(_("payments.no_methods"), kb([_back_row(_, order)]))
    rows.append(_back_row(_, order))
    return Screen(_order_text(_, order), kb(rows))


async def order_screen(
    ctx: AppContext,
    _: Translator,
    user_id: int,
    purpose: Purpose | str,
    *,
    amount_kop: int = 0,
    plan_id: int | None = None,
) -> Screen:
    """Method picker for a request (other modules call this for their "pay" buttons); errors become a screen."""
    try:
        order = await flow.build_order(ctx, user_id, purpose, amount_kop=amount_kop, plan_id=plan_id)
    except flow.OrderError as exc:
        return Screen(_(_ORDER_ERRORS.get(exc.code, "payments.error.unavailable")), kb([[to_menu(_)]]))
    return methods_screen(ctx, _, order)


def _invoice_text(ctx: AppContext, _: Translator, order: flow.Order, amount_kop: int, title: str) -> str:
    key = "payments.invoice.topup" if order.purpose is Purpose.TOPUP else "payments.invoice.plan"
    return _(
        key,
        amount=_.money(amount_kop),
        provider=_.esc(title),
        hours=_.plural(ctx.settings.payment_max_pending_hours, "plural.hours"),
    )


def _retry_kb(_: Translator, order: flow.Order) -> InlineKeyboardMarkup:
    data = PayCb(
        a="m",
        k=order.purpose.value,
        amt=order.stars if order.purpose is Purpose.DONATION else order.amount_kop,
        plan=order.plan_id or 0,
    )
    return kb([[Button.cb(_("payments.other_method"), data)], [to_menu(_)]])


# --- handlers ---------------------------------------------------------------------------------------------------------
async def _order_from(ctx: AppContext, user_id: int, data: PayCb) -> flow.Order:
    purpose = Purpose(data.k) if data.k in Purpose._value2member_map_ else Purpose.TOPUP
    return await flow.build_order(
        ctx,
        user_id,
        purpose,
        amount_kop=data.amt if purpose is not Purpose.DONATION else 0,
        stars_amount=data.amt if purpose is Purpose.DONATION else 0,
        plan_id=data.plan or None,
        recurring=data.r == 1,
        save_card=data.r == SAVE_CARD,
    )


@router.callback_query(PayCb.filter(F.a == "m"))
async def show_methods(
    callback: CallbackQuery, callback_data: PayCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    try:
        order = await _order_from(ctx, callback.from_user.id, callback_data)
    except flow.OrderError as exc:
        await answer_callback(callback, _(_ORDER_ERRORS.get(exc.code, "payments.error.unavailable")), alert=True)
        return
    await render(callback, methods_screen(ctx, _, order))


@router.callback_query(PayCb.filter(F.a == "go"))
async def choose_method(
    callback: CallbackQuery, callback_data: PayCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    user_id = callback.from_user.id
    try:
        order = await _order_from(ctx, user_id, callback_data)
    except flow.OrderError as exc:
        await answer_callback(callback, _(_ORDER_ERRORS.get(exc.code, "payments.error.unavailable")), alert=True)
        return
    gateway = ctx.gateways.get(callback_data.prov)
    if gateway is None or flow.method_for(order, gateway, ctx.settings) is None:
        await answer_callback(callback, _("payments.error.method_unavailable"), alert=True)
        return
    async with read_session(ctx.session_factory) as session:
        email = await session.scalar(select(User.receipt_email).where(User.tg_id == user_id))
    if flow.needs_receipt_email(ctx.settings, gateway.key, email):
        await start_input(state, PayInput.email, pay=callback_data.pack())
        back = PayCb(a="m", k=callback_data.k, amt=callback_data.amt, plan=callback_data.plan)
        await render(callback, Screen(_("payments.email.ask"), kb([[Button.cb(_("nav.cancel"), back)]])))
        return
    await answer_callback(callback, _("payments.creating"))
    chat_id = callback.message.chat.id if callback.message is not None else user_id
    await issue(ctx, _, callback, chat_id, user_id, order, gateway.key)


async def issue(
    ctx: AppContext,
    _: Translator,
    event: CallbackQuery | Message,
    chat_id: int,
    user_id: int,
    order: flow.Order,
    provider: str,
) -> None:
    """Create the payment with ``provider`` and show its invoice."""
    gateway = ctx.gateways.get(provider)
    if gateway is None:
        await render(event, Screen(_("payments.error.method_unavailable"), _retry_kb(_, order)))
        return
    title = flow.gateway_title(ctx, gateway.key, _.lang, gateway.title)
    try:
        payment = await flow.create(
            ctx, user_id, order, gateway, description_text=flow.description(_, order, ctx.settings.brand_name)
        )
    except GatewayError as exc:
        log.warning("user %s: %s invoice failed: %s", user_id, provider, exc)
        await render(event, Screen(_("payments.error.provider", provider=_.esc(title)), _retry_kb(_, order)))
        return
    except (ValueError, LookupError) as exc:
        log.info("user %s: %s payment refused: %s", user_id, provider, exc)
        await render(event, Screen(_("payments.error.unavailable"), _retry_kb(_, order)))
        return

    if gateway.key == flow.STARS:
        await _show_stars_invoice(ctx, _, event, chat_id, order, payment)
        return
    details = onchain.instructions(gateway, payment)
    if details is not None:
        screen = onchain_view.transfer_screen(
            _,
            payment,
            details,
            hours=ctx.settings.payment_max_pending_hours,
            check=Button.cb(_("payments.check_button"), PayCheckCb(id=payment.id)),
            footer=[to_menu(_)],
        )
        await render(event, screen)
        return
    if not payment.confirmation_url:
        await render(event, Screen(_("payments.error.provider", provider=_.esc(title)), _retry_kb(_, order)))
        return
    markup = kb(
        [
            [Button.url(_("payments.pay_button", amount=_.money(payment.amount_kop)), payment.confirmation_url)],
            [Button.cb(_("payments.check_button"), PayCheckCb(id=payment.id))],
            [to_menu(_)],
        ]
    )
    text = _invoice_text(ctx, _, order, payment.amount_kop, title)
    if payment.save_method:
        text += "\n\n" + _("payments.invoice.save_card_note")
    await render(event, Screen(text, markup))


async def _show_stars_invoice(
    ctx: AppContext, _: Translator, event: CallbackQuery | Message, chat_id: int, order: flow.Order, payment: Any
) -> None:
    stars_amount = int(payment.provider_amount or 0)
    try:
        if payment.is_recurring:
            link = await flow.stars_invoice_link(ctx, _, payment)
            markup = kb(
                [
                    [Button.url(_("payments.stars.pay_button", stars=_.number(stars_amount)), link)],
                    [
                        Button.cb(
                            _("payments.other_method"), PayCb(a="m", k=order.purpose.value, plan=order.plan_id or 0)
                        )
                    ],
                ]
            )
            await render(event, Screen(_("payments.stars.subscription_text", stars=_.number(stars_amount)), markup))
            return
        retry = _retry_kb(_, order).inline_keyboard[0][0]
        markup = kb([[Button.pay(_("payments.stars.pay_button", stars=_.number(stars_amount)))], [retry]])
        await flow.send_stars_invoice(ctx, _, chat_id, payment, reply_markup=markup)
    except TelegramAPIError as exc:
        log.warning("payment %s: Stars invoice failed: %s", payment.id, exc)
        await render(event, Screen(_("payments.error.provider", provider="Telegram Stars"), _retry_kb(_, order)))
        return
    if isinstance(event, CallbackQuery):
        await delete_message(event.message)


@router.callback_query(PayCheckCb.filter())
async def check_payment(callback: CallbackQuery, callback_data: PayCheckCb, ctx: AppContext, _: Translator) -> None:
    payment = await flow.check(ctx, callback.from_user.id, callback_data.id)
    if payment is None:
        await answer_callback(callback, _("error.not_found"), alert=True)
        return
    if flow.is_paid(payment):
        await render(callback, Screen(_("payments.check.paid", amount=_.money(payment.amount_kop)), kb([[to_menu(_)]])))
        return
    if payment.status in OPEN_STATUSES:
        await answer_callback(callback, _("payments.check.pending"), alert=True)
        return
    await render(
        callback, Screen(_("payments.check.closed"), kb([[nav(_("payments.new_payment"), "topup")], [to_menu(_)]]))
    )


@router.message(PayInput.email, TEXT_INPUT)
async def receipt_email(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    data = await input_data(state)
    if message.from_user is None:
        return
    email = flow.valid_email(message.text)
    if email is None:
        await message.answer(_("payments.email.invalid"))
        return
    await state.clear()
    user_id = message.from_user.id
    async with ctx.session_factory.begin() as session:
        await session.execute(update(User).where(User.tg_id == user_id).values(receipt_email=email))
    try:
        pay = PayCb.unpack(str(data.get("pay", "")))
        order = await _order_from(ctx, user_id, pay)
    except (ValueError, TypeError, flow.OrderError):
        await render(message, Screen(_("payments.error.unavailable"), kb([[to_menu(_)]])))
        return
    await message.answer(_("payments.email.saved"))
    await issue(ctx, _, message, message.chat.id, user_id, order, pay.prov)


# --- donations --------------------------------------------------------------------------------------------------------
def donate_screen(ctx: AppContext, _: Translator) -> Screen:
    settings = ctx.settings
    if not settings.donations_enabled or ctx.gateways.get(flow.STARS) is None:
        return Screen(_("payments.donate.unavailable"), kb([[to_menu(_)]]))
    buttons = [
        Button.cb(
            _("payments.donate.button", stars=_.number(n)),
            PayCb(a="go", k=Purpose.DONATION.value, amt=n, prov=flow.STARS),
        )
        for n in flow.DONATION_STARS
    ]
    return Screen(_("payments.donate.text", brand=_.esc(settings.brand_name)), kb([*grid(buttons, 3), [to_menu(_)]]))


@router.message(Command("donate"))
async def donate_command(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(message, donate_screen(ctx, _))


@router.callback_query(Nav.filter(F.to.in_(DONATE_TARGETS)))
async def donate_nav(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, donate_screen(ctx, _))
