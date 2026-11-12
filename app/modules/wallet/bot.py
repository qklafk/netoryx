"""Wallet screens: balance (main + partner), top-up with quick or custom amounts, operations history."""

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
from app.db.models import BalanceTransaction, LedgerAccount, SavedPaymentMethod, User
from app.db.session import read_session
from app.keyboards import Button, grid, kb, nav, to_menu
from app.keyboards.callbacks import Nav
from app.modules.payments.bot import order_screen
from app.modules.payments.callbacks import PayCb, Purpose
from app.modules.payments.helpers import TEXT_INPUT, input_data, parse_rubles, signed_money, start_input
from app.services import saved_cards, wallet

router = Router(name="wallet")
# User screens live in the private chat only: the access gates do not look at group chats.
router.message.filter(IsPrivate())
router.callback_query.filter(IsPrivate())

BALANCE_TARGETS = frozenset({"balance", "wallet"})
TOPUP_TARGETS = frozenset({"topup", "top_up"})
HISTORY_PAGE = 10


class WalletCb(CallbackData, prefix="wal"):
    """``a``: ``main`` | ``top`` | ``custom`` | ``hist`` (``v`` = show rows older than this ledger id, 0 = newest) |
    ``cards`` | ``rmcard`` (``v`` = saved card id)."""

    a: str
    v: int = 0


class WalletInput(StatesGroup):
    amount = State()


def _payments_on(ctx: AppContext) -> bool:
    return ctx.modules.is_enabled("payments", ctx.settings) and bool(ctx.gateways.enabled())


def _partner_shown(ctx: AppContext, referral_kop: int) -> bool:
    settings = ctx.settings
    partner_mode = settings.referral_reward_mode == "partner" and ctx.modules.is_enabled("referral", settings)
    return referral_kop > 0 or partner_mode


async def balance_screen(ctx: AppContext, _: Translator, user_id: int) -> Screen:
    async with read_session(ctx.session_factory) as session:
        row = (
            await session.execute(select(User.balance_kop, User.referral_balance_kop).where(User.tg_id == user_id))
        ).first()
        cards = await session.scalar(
            select(SavedPaymentMethod.id)
            .where(SavedPaymentMethod.user_id == user_id, SavedPaymentMethod.is_active.is_(True))
            .limit(1)
        )
    main_kop, referral_kop = (int(row[0]), int(row[1])) if row is not None else (0, 0)
    lines = [_("wallet.balance.title", balance=_.money(main_kop))]
    if _partner_shown(ctx, referral_kop):
        lines.append(_("wallet.balance.partner", balance=_.money(referral_kop)))
    lines.append("")
    lines.append(_("wallet.balance.hint"))
    rows: list[list[Any]] = []
    if _payments_on(ctx):
        rows.append([Button.cb(_("wallet.topup_button"), WalletCb(a="top"), style="success")])
    rows.append([Button.cb(_("wallet.history_button"), WalletCb(a="hist"))])
    if cards is not None or ctx.settings.saved_cards_enabled:
        rows.append([Button.cb(_("wallet.cards_button"), WalletCb(a="cards"))])
    if _partner_shown(ctx, referral_kop) and ctx.modules.is_enabled("referral", ctx.settings):
        rows.append([nav(_("wallet.partner_button"), "referral")])
    rows.append([to_menu(_)])
    return Screen("\n".join(lines), kb(rows))


def topup_screen(ctx: AppContext, _: Translator) -> Screen:
    settings = ctx.settings
    if not _payments_on(ctx):
        return Screen(_("wallet.topup.unavailable"), kb([[nav(_("nav.back"), "balance"), to_menu(_)]]))
    amounts = [
        rub for rub in settings.topup_quick_amounts_rub if settings.min_topup_rub <= rub <= settings.max_topup_rub
    ]
    buttons = [
        Button.cb(_.money(rub * 100), PayCb(a="m", k=Purpose.TOPUP.value, amt=rub * 100))
        for rub in dict.fromkeys(amounts)
    ]
    text = _(
        "wallet.topup.text",
        min=_.money(settings.min_topup_kop),
        max=_.money(settings.max_topup_kop),
    )
    rows = [*grid(buttons, 2), [Button.cb(_("wallet.topup.custom"), WalletCb(a="custom"))]]
    rows.append([nav(_("nav.back"), "balance"), to_menu(_)])
    return Screen(text, kb(rows))


def _tx_label(_: Translator, tx: BalanceTransaction) -> str:
    label = _(f"wallet.tx.{tx.kind.value}")
    if tx.account is LedgerAccount.REFERRAL:
        label += " " + _("wallet.tx.partner_account")
    return label


async def history_screen(ctx: AppContext, _: Translator, user_id: int, before_id: int = 0) -> Screen:
    async with read_session(ctx.session_factory) as session:
        rows = await wallet.history(session, user_id, limit=HISTORY_PAGE + 1, before_id=before_id or None)
    page, more = rows[:HISTORY_PAGE], len(rows) > HISTORY_PAGE
    if not page:
        text = _("wallet.history.empty") if not before_id else _("wallet.history.end")
    else:
        items = [
            _(
                "wallet.history.item",
                date=_.date(tx.created_at),
                label=_tx_label(_, tx),
                amount=signed_money(_, tx.amount_kop),
            )
            for tx in page
        ]
        text = _("wallet.history.title") + "\n\n" + "\n".join(items)
    navigation = []
    if before_id:
        navigation.append(Button.cb(_("wallet.history.newest"), WalletCb(a="hist")))
    if more:
        navigation.append(Button.cb(_("wallet.history.older"), WalletCb(a="hist", v=page[-1].id)))
    return Screen(text, kb([navigation, [nav(_("nav.back"), "balance"), to_menu(_)]]))


async def cards_screen(ctx: AppContext, _: Translator, user_id: int) -> Screen:
    """Saved cards for auto-renewal, each with a remove button."""
    async with read_session(ctx.session_factory) as session:
        cards = await saved_cards.list_active(session, user_id)
    rows: list[list[Any]] = []
    if cards:
        lines = [_("wallet.cards.title"), ""]
        lines.extend(_("wallet.cards.item", title=_.esc(card.title), date=_.date(card.created_at)) for card in cards)
        lines += ["", _("wallet.cards.hint")]
        rows.extend(
            [Button.cb(_("wallet.cards.remove", title=card.title), WalletCb(a="rmcard", v=card.id))] for card in cards
        )
        text = "\n".join(lines)
    else:
        text = _("wallet.cards.empty") if ctx.settings.saved_cards_enabled else _("wallet.cards.off")
    rows.append([Button.cb(_("nav.back"), WalletCb(a="main")), to_menu(_)])
    return Screen(text, kb(rows))


# --- handlers ---------------------------------------------------------------------------------------------------------
@router.callback_query(Nav.filter(F.to.in_(BALANCE_TARGETS)))
async def open_balance(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, await balance_screen(ctx, _, callback.from_user.id))


@router.message(Command("balance"))
async def balance_command(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await state.clear()
    await render(message, await balance_screen(ctx, _, message.from_user.id))


@router.callback_query(WalletCb.filter(F.a == "main"))
async def back_to_balance(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, await balance_screen(ctx, _, callback.from_user.id))


@router.callback_query(Nav.filter(F.to.in_(TOPUP_TARGETS)))
@router.callback_query(WalletCb.filter(F.a == "top"))
async def open_topup(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, topup_screen(ctx, _))


@router.callback_query(WalletCb.filter(F.a == "custom"))
async def ask_amount(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    settings = ctx.settings
    await start_input(state, WalletInput.amount)
    text = _("wallet.topup.ask", min=_.money(settings.min_topup_kop), max=_.money(settings.max_topup_kop))
    await render(callback, Screen(text, kb([[Button.cb(_("nav.cancel"), WalletCb(a="top"))]])))


@router.message(WalletInput.amount, TEXT_INPUT)
async def amount_entered(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await input_data(state)
    if message.from_user is None:
        return
    settings = ctx.settings
    rubles = parse_rubles(message.text)
    if rubles is None or not settings.min_topup_rub <= rubles <= settings.max_topup_rub:
        text = _("wallet.topup.invalid", min=_.money(settings.min_topup_kop), max=_.money(settings.max_topup_kop))
        await render(message, Screen(text, kb([[Button.cb(_("nav.cancel"), WalletCb(a="top"))]])))
        return
    await state.clear()
    screen = await order_screen(ctx, _, message.from_user.id, Purpose.TOPUP, amount_kop=rubles * 100)
    await render(message, screen)


@router.callback_query(WalletCb.filter(F.a == "hist"))
async def open_history(callback: CallbackQuery, callback_data: WalletCb, ctx: AppContext, _: Translator) -> None:
    await render(callback, await history_screen(ctx, _, callback.from_user.id, max(0, callback_data.v)))


@router.callback_query(WalletCb.filter(F.a == "cards"))
async def open_cards(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await cards_screen(ctx, _, callback.from_user.id))


@router.callback_query(WalletCb.filter(F.a == "rmcard"))
async def remove_card(callback: CallbackQuery, callback_data: WalletCb, ctx: AppContext, _: Translator) -> None:
    user_id = callback.from_user.id
    async with ctx.session_factory.begin() as session:
        removed = await saved_cards.deactivate(session, user_id, callback_data.v)
    await answer_callback(callback, _("wallet.cards.removed" if removed else "error.not_found"))
    await render(callback, await cards_screen(ctx, _, user_id))
