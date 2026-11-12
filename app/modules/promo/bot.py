"""Promo / gift code entry: ``/promo [code]``, the "enter code" screen and the redemption result."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.core.context import AppContext
from app.core.filters import IsPrivate
from app.core.i18n import Translator
from app.core.ui import Screen, render
from app.keyboards import Button, kb, nav, to_menu
from app.keyboards.callbacks import Nav
from app.modules.payments.helpers import TEXT_INPUT, input_data, start_input, touch_input
from app.modules.promo.service import RedeemResult, redeem_code
from app.services.promo import PromoOutcome

router = Router(name="promo")
# User screens live in the private chat only: the access gates do not look at group chats.
router.message.filter(IsPrivate())
router.callback_query.filter(IsPrivate())

# "gift" is the main menu's "Gift / promo code" entry: answered here when the gifts module is off.
PROMO_TARGETS = frozenset({"promo", "gift", "gifts"})


class PromoCb(CallbackData, prefix="promo"):
    a: str  # "enter" | "cancel"


class PromoInput(StatesGroup):
    code = State()


def _back_row(ctx: AppContext, _: Translator) -> list[Any]:
    if ctx.modules.is_enabled("gifts", ctx.settings):
        return [nav(_("nav.back"), "gift"), to_menu(_)]
    return [to_menu(_)]


def enter_screen(ctx: AppContext, _: Translator) -> Screen:
    return Screen(_("promo.enter"), kb([[Button.cb(_("nav.cancel"), PromoCb(a="cancel"))]]))


def redeem_screen(ctx: AppContext, _: Translator, result: RedeemResult, *, retry: CallbackData | None = None) -> Screen:
    """What the user sees after a redemption attempt (also used for ``/start promo_<code>`` links).

    ``retry`` is the "enter another code" button's callback (the promo entry screen by default).
    """
    if not result.ok:
        text = _(f"promo.error.{result.outcome.value}")
        rows = [[Button.cb(_("promo.try_another"), retry or PromoCb(a="enter"))], [to_menu(_)]]
        return Screen(text, kb(rows))
    title = _("promo.ok.gift", plan=_.esc(result.plan_title or "")) if result.is_gift else _("promo.ok.title")
    lines = [title]
    if result.days > 0 and result.expires_at is not None:
        lines.append(_("promo.ok.days", days=_.duration(result.days), date=_.date(result.expires_at)))
    if result.bonus_kop > 0:
        lines.append(_("promo.ok.money", amount=_.money(result.bonus_kop), balance=_.money(result.balance_kop)))
    return Screen("\n".join(lines), kb([[to_menu(_)]]))


async def _redeem_and_render(
    event: Message | CallbackQuery, ctx: AppContext, _: Translator, user_id: int, code: str
) -> RedeemResult:
    result = await redeem_code(ctx, user_id, code)
    await render(event, redeem_screen(ctx, _, result))
    return result


@router.callback_query(Nav.filter(F.to.in_(PROMO_TARGETS)))
@router.callback_query(PromoCb.filter(F.a == "enter"))
async def open_enter(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await start_input(state, PromoInput.code)
    await render(callback, enter_screen(ctx, _))


@router.callback_query(PromoCb.filter(F.a == "cancel"))
async def cancel(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, Screen(_("promo.canceled"), kb([_back_row(ctx, _)])))


@router.message(Command("promo"))
async def promo_command(
    message: Message, command: CommandObject, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    if message.from_user is None:
        return
    if command.args and command.args.strip():
        await state.clear()
        await _redeem_and_render(message, ctx, _, message.from_user.id, command.args)
        return
    await start_input(state, PromoInput.code)
    await render(message, enter_screen(ctx, _))


@router.message(PromoInput.code, TEXT_INPUT)
async def code_entered(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await input_data(state)
    if message.from_user is None:
        return
    result = await redeem_code(ctx, message.from_user.id, message.text or "")
    if result.ok or result.outcome is not PromoOutcome.NOT_FOUND:
        await state.clear()
        await render(message, redeem_screen(ctx, _, result))
        return
    await touch_input(state)  # a typo: let the user try again
    await render(
        message, Screen(_("promo.error.not_found_retry"), kb([[Button.cb(_("nav.cancel"), PromoCb(a="cancel"))]]))
    )
