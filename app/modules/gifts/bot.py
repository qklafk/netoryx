"""Gift screens: the "Gift / promo code" hub, buying a plan as a gift (balance or payment), the user's gifts."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.core.context import AppContext
from app.core.filters import IsPrivate
from app.core.i18n import Translator
from app.core.ui import Screen, answer_callback, render
from app.db.models import Plan, User
from app.db.session import read_session
from app.keyboards import Button, kb, nav, to_menu
from app.keyboards.callbacks import Nav
from app.modules.gifts import service
from app.modules.payments.callbacks import PayCb, Purpose
from app.modules.payments.helpers import TEXT_INPUT, input_data, nonce, share_url, start_input, touch_input
from app.modules.promo.bot import PromoCb, redeem_screen
from app.modules.promo.service import redeem_code
from app.services.promo import PromoOutcome
from app.services.subscriptions import SubErrorCode, SubscriptionError

router = Router(name="gifts")
# User screens live in the private chat only: the access gates do not look at group chats.
router.message.filter(IsPrivate())
router.callback_query.filter(IsPrivate())

HUB_TARGETS = frozenset({"gift", "gifts"})
MAX_COPY = 256


class GiftCb(CallbackData, prefix="gift"):
    """``a``: ``hub`` | ``plans`` | ``plan`` | ``buy`` (``n`` = one-shot token) | ``mine`` | ``code``."""

    a: str
    plan: int = 0
    n: str = ""


class GiftInput(StatesGroup):
    code = State()


def _payments_on(ctx: AppContext) -> bool:
    return ctx.modules.is_enabled("payments", ctx.settings) and bool(ctx.gateways.enabled())


def _code_button(ctx: AppContext, _: Translator) -> Any:
    target: CallbackData = PromoCb(a="enter") if ctx.modules.is_enabled("promo", ctx.settings) else GiftCb(a="code")
    return Button.cb(_("gifts.hub.enter_code"), target)


def hub_screen(ctx: AppContext, _: Translator) -> Screen:
    rows = [
        [Button.cb(_("gifts.hub.give"), GiftCb(a="plans"), style="success")],
        [_code_button(ctx, _)],
        [Button.cb(_("gifts.hub.mine"), GiftCb(a="mine"))],
        [to_menu(_)],
    ]
    return Screen(_("gifts.hub.text", brand=_.esc(ctx.settings.brand_name)), kb(rows))


async def plans_screen(ctx: AppContext, _: Translator) -> Screen:
    async with read_session(ctx.session_factory) as session:
        plans = await service.gift_plans(session)
    back = [nav(_("nav.back"), "gift"), to_menu(_)]
    if not plans:
        return Screen(_("gifts.plans.empty"), kb([back]))
    rows = [
        [
            Button.cb(
                _("gifts.plans.item", plan=plan.title, price=_.money(plan.price_kop)),
                GiftCb(a="plan", plan=plan.id),
            )
        ]
        for plan in plans
    ]
    return Screen(_("gifts.plans.text"), kb([*rows, back]))


async def plan_screen(ctx: AppContext, _: Translator, user_id: int, plan_id: int) -> Screen | None:
    async with read_session(ctx.session_factory) as session:
        plan = await session.get(Plan, plan_id)
        balance = int(await session.scalar(select(User.balance_kop).where(User.tg_id == user_id)) or 0)
    if plan is None or not plan.is_active or plan.is_hidden or plan.price_kop <= 0:
        return None
    devices = _.plural(plan.device_limit, "plural.devices") if plan.device_limit else _("gifts.unlimited_devices")
    lines = [
        _(
            "gifts.plan.text",
            plan=_.esc(plan.title),
            period=_.duration(plan.duration_days),
            devices=devices,
            price=_.money(plan.price_kop),
        ),
        _("gifts.plan.balance", balance=_.money(balance)),
    ]
    rows: list[list[Any]] = []
    if balance >= plan.price_kop:
        label = _("gifts.plan.pay_balance", price=_.money(plan.price_kop))
        rows.append([Button.cb(label, GiftCb(a="buy", plan=plan.id, n=nonce()), style="success")])
    elif _payments_on(ctx):
        missing = plan.price_kop - balance
        lines.append(_("gifts.plan.missing", amount=_.money(missing)))
        rows.append([Button.cb(_("gifts.plan.pay"), PayCb(a="m", k=Purpose.GIFT.value, plan=plan.id), style="success")])
    else:
        lines.append(_("gifts.plan.no_payments"))
    rows.append([Button.cb(_("nav.back"), GiftCb(a="plans")), to_menu(_)])
    return Screen("\n\n".join(lines), kb(rows))


def ready_screen(ctx: AppContext, _: Translator, code: str, plan: Plan) -> Screen:
    link = service.gift_link(ctx.bot_username, code)
    text = _(
        "gifts.ready",
        plan=_.esc(plan.title),
        period=_.duration(plan.duration_days),
        link=_.esc(link),
        code=_.esc(code),
    )
    share_text = _("gifts.share_text", brand=ctx.settings.brand_name, period=_.duration(plan.duration_days))
    rows: list[list[Any]] = []
    if link.startswith("https://"):
        rows.append([Button.url(_("gifts.share"), share_url(link, share_text))])
        if len(link) <= MAX_COPY:
            rows.append([Button.copy(_("gifts.copy_link"), link)])
    rows.append([Button.cb(_("gifts.hub.mine"), GiftCb(a="mine")), to_menu(_)])
    return Screen(text, kb(rows))


async def mine_screen(ctx: AppContext, _: Translator, user_id: int) -> Screen:
    gifts = await service.own_gifts(ctx, user_id)
    back = [nav(_("nav.back"), "gift"), to_menu(_)]
    if not gifts:
        return Screen(_("gifts.mine.empty"), kb([[Button.cb(_("gifts.hub.give"), GiftCb(a="plans"))], back]))
    lines = [_("gifts.mine.title")]
    rows: list[list[Any]] = []
    for gift in gifts:
        status = _("gifts.mine.redeemed") if gift.redeemed else _("gifts.mine.waiting")
        lines.append(_("gifts.mine.item", plan=_.esc(gift.plan_title or "—"), code=_.esc(gift.code), status=status))
        link = service.gift_link(ctx.bot_username, gift.code)
        if not gift.redeemed and link.startswith("https://"):
            share_text = _("gifts.share_text", brand=ctx.settings.brand_name, period=_.duration(gift.days))
            rows.append([Button.url(_("gifts.mine.share", code=gift.code), share_url(link, share_text))])
    return Screen("\n\n".join(lines), kb([*rows, back]))


# --- handlers ---------------------------------------------------------------------------------------------------------
@router.callback_query(Nav.filter(F.to.in_(HUB_TARGETS)))
@router.callback_query(GiftCb.filter(F.a == "hub"))
async def open_hub(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, hub_screen(ctx, _))


@router.message(Command("gift"))
async def gift_command(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(message, hub_screen(ctx, _))


@router.callback_query(GiftCb.filter(F.a == "plans"))
async def open_plans(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await plans_screen(ctx, _))


@router.callback_query(GiftCb.filter(F.a == "plan"))
async def open_plan(callback: CallbackQuery, callback_data: GiftCb, ctx: AppContext, _: Translator) -> None:
    screen = await plan_screen(ctx, _, callback.from_user.id, callback_data.plan)
    if screen is None:
        await answer_callback(callback, _("gifts.plan.unavailable"), alert=True)
        return
    await render(callback, screen)


@router.callback_query(GiftCb.filter(F.a == "buy"))
async def buy(callback: CallbackQuery, callback_data: GiftCb, ctx: AppContext, _: Translator) -> None:
    if not callback_data.n:
        await answer_callback(callback, _("error.expired_button"), alert=True)
        return
    try:
        gift, plan = await service.buy_from_balance(ctx, callback.from_user.id, callback_data.plan, callback_data.n)
    except SubscriptionError as exc:
        if exc.code is SubErrorCode.INSUFFICIENT_FUNDS:
            screen = await plan_screen(ctx, _, callback.from_user.id, callback_data.plan)
            if screen is not None:
                await render(callback, screen, answer=_("gifts.error.funds"), alert=True)
                return
        await answer_callback(callback, _("gifts.plan.unavailable"), alert=True)
        return
    await render(callback, ready_screen(ctx, _, gift.code, plan))


@router.callback_query(GiftCb.filter(F.a == "mine"))
async def open_mine(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await mine_screen(ctx, _, callback.from_user.id))


@router.callback_query(GiftCb.filter(F.a == "code"))
async def ask_code(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await start_input(state, GiftInput.code)
    await render(callback, Screen(_("gifts.code.ask"), kb([[Button.cb(_("nav.cancel"), GiftCb(a="hub"))]])))


@router.message(GiftInput.code, TEXT_INPUT)
async def code_entered(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await input_data(state)
    if message.from_user is None:
        return
    result = await redeem_code(ctx, message.from_user.id, message.text or "")
    if result.outcome is PromoOutcome.NOT_FOUND:
        await touch_input(state)
        await render(message, Screen(_("gifts.code.not_found"), kb([[Button.cb(_("nav.cancel"), GiftCb(a="hub"))]])))
        return
    await state.clear()
    await render(message, redeem_screen(ctx, _, result, retry=GiftCb(a="code")))
