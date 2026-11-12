"""Referral screens: link, QR and sharing, program terms per reward mode, stats per level; partner mode: transfer to
the main balance and payout requests."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.core.context import AppContext
from app.core.filters import IsPrivate
from app.core.i18n import Translator
from app.core.ui import Screen, answer_callback, render
from app.db.models import Withdrawal
from app.db.session import read_session
from app.keyboards import Button, kb, nav, to_menu
from app.keyboards.callbacks import Nav
from app.modules.payments.helpers import (
    TEXT_INPUT,
    input_data,
    nonce,
    parse_rubles,
    share_url,
    start_input,
    touch_input,
)
from app.modules.referral import service
from app.services import partner
from app.services.partner import PartnerError, PartnerErrorCode
from app.services.qr import make_qr_png_async

log = logging.getLogger(__name__)

router = Router(name="referral")
# User screens live in the private chat only: the access gates do not look at group chats.
router.message.filter(IsPrivate())
router.callback_query.filter(IsPrivate())

REFERRAL_TARGETS = frozenset({"referral", "ref", "partner"})
MAX_COPY = 256
WITHDRAWALS_SHOWN = 10


class RefCb(CallbackData, prefix="ref"):
    """``a``: ``main`` | ``qr`` | ``lvl`` | ``tr`` / ``trc`` / ``tro`` (transfer) | ``wd`` / ``wdm`` / ``wda`` /
    ``wdok`` / ``wdl`` (payouts). ``v`` = kopecks, ``s`` = method or one-shot token."""

    a: str
    v: int = 0
    s: str = ""


class RefInput(StatesGroup):
    transfer = State()
    amount = State()
    details = State()


def _partner_mode(ctx: AppContext) -> bool:
    return ctx.settings.referral_reward_mode == "partner"


def _withdrawals(ctx: AppContext) -> bool:
    return partner.withdrawals_available(ctx.settings)


def _method_title(_: Translator, ctx: AppContext, method: str) -> str:
    key = f"referral.method.{method}"
    return _(key) if ctx.i18n.has(key) else method


def _status_title(_: Translator, withdrawal: Withdrawal) -> str:
    return _(f"referral.withdrawal.status.{withdrawal.status.value}")


def _back_main(_: Translator) -> list[Any]:
    return [Button.cb(_("nav.back"), RefCb(a="main")), to_menu(_)]


# --- main screen ------------------------------------------------------------------------------------------------------
async def main_screen(ctx: AppContext, _: Translator, user_id: int) -> Screen:
    settings = ctx.settings
    data = await service.overview(ctx, user_id)
    if data is None:
        return Screen(_("error.not_found"), kb([[to_menu(_)]]))
    stats = data.stats
    lines = [_("referral.title"), ""]
    lines.extend(service.terms(_, settings, withdrawals=_withdrawals(ctx)))
    lines += ["", _("referral.link", link=_.esc(data.link)), ""]
    lines.append(_("referral.stats.invited", users=_.number(stats.total_users), paid=_.number(stats.total_paid_users)))
    if settings.referral_reward_mode == "days":
        if stats.total_earned_days:
            lines.append(_("referral.stats.earned_days", days=_.plural(stats.total_earned_days, "plural.days")))
    else:
        lines.append(_("referral.stats.earned", amount=_.money(stats.total_earned_kop)))
    if _partner_mode(ctx) or stats.referral_balance_kop > 0:
        lines.append(_("referral.stats.partner_balance", amount=_.money(stats.referral_balance_kop)))
        if stats.pending_withdrawal_kop:
            lines.append(_("referral.stats.pending", amount=_.money(stats.pending_withdrawal_kop)))
    lines += ["", _("referral.legal")]

    share_text = _("referral.share_text", brand=settings.brand_name)
    rows: list[list[Any]] = []
    if data.link.startswith("https://"):
        rows.append([Button.url(_("referral.share"), share_url(data.link, share_text))])
        row: list[Any] = [Button.cb(_("referral.qr"), RefCb(a="qr"))]
        if len(data.link) <= MAX_COPY:
            row.insert(0, Button.copy(_("referral.copy"), data.link))
        rows.append(row)
    if len(stats.levels) > 1:
        rows.append([Button.cb(_("referral.levels_button"), RefCb(a="lvl"))])
    if stats.referral_balance_kop > 0:
        rows.append([Button.cb(_("referral.transfer.button"), RefCb(a="tr"))])
    if _withdrawals(ctx):
        rows.append([Button.cb(_("referral.withdraw.button"), RefCb(a="wd"))])
    if data.has_withdrawals:
        rows.append([Button.cb(_("referral.withdrawals.button"), RefCb(a="wdl"))])
    rows.append([to_menu(_)])
    return Screen("\n".join(lines), kb(rows))


async def levels_screen(ctx: AppContext, _: Translator, user_id: int) -> Screen:
    data = await service.overview(ctx, user_id)
    if data is None:
        return Screen(_("error.not_found"), kb([[to_menu(_)]]))
    percents = ctx.settings.referral_levels_percent
    days_mode = ctx.settings.referral_reward_mode == "days"
    lines = [_("referral.levels.title"), ""]
    for level in data.stats.levels:
        pct = percents[level.level - 1] if level.level <= len(percents) else 0
        earned = _.plural(level.earned_days, "plural.days") if days_mode else _.money(level.earned_kop)
        lines.append(
            _(
                "referral.levels.item",
                level=level.level,
                pct=pct,
                users=_.number(level.users),
                paid=_.number(level.paid_users),
                earned=earned,
            )
        )
    return Screen("\n\n".join(lines), kb([_back_main(_)]))


@router.callback_query(Nav.filter(F.to.in_(REFERRAL_TARGETS)))
@router.callback_query(RefCb.filter(F.a == "main"))
async def open_main(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, await main_screen(ctx, _, callback.from_user.id))


@router.message(Command("ref", "referral", "partner"))
async def referral_command(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await state.clear()
    await render(message, await main_screen(ctx, _, message.from_user.id))


@router.callback_query(RefCb.filter(F.a == "qr"))
async def show_qr(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    data = await service.overview(ctx, callback.from_user.id)
    if data is None or not data.link.startswith("https://"):
        await answer_callback(callback, _("error.unavailable"), alert=True)
        return
    photo = await make_qr_png_async(data.link)
    await render(callback, Screen(_("referral.qr_caption", link=_.esc(data.link)), kb([_back_main(_)]), photo=photo))


@router.callback_query(RefCb.filter(F.a == "lvl"))
async def show_levels(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await levels_screen(ctx, _, callback.from_user.id))


# --- transfer to the main balance -------------------------------------------------------------------------------------
async def transfer_screen(ctx: AppContext, _: Translator, user_id: int) -> Screen | None:
    _main, referral = await service.balances(ctx, user_id)
    if referral <= 0:
        return None
    rows = [
        [Button.cb(_("referral.transfer.all", amount=_.money(referral)), RefCb(a="tro", v=referral, s=nonce()))],
        [Button.cb(_("referral.transfer.custom"), RefCb(a="trc"))],
        _back_main(_),
    ]
    return Screen(_("referral.transfer.text", balance=_.money(referral)), kb(rows))


def _confirm_transfer(_: Translator, amount_kop: int) -> Screen:
    rows = [
        [Button.cb(_("referral.transfer.confirm"), RefCb(a="tro", v=amount_kop, s=nonce()), style="success")],
        [Button.cb(_("nav.cancel"), RefCb(a="tr"))],
    ]
    return Screen(_("referral.transfer.ask_confirm", amount=_.money(amount_kop)), kb(rows))


@router.callback_query(RefCb.filter(F.a == "tr"))
async def open_transfer(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    screen = await transfer_screen(ctx, _, callback.from_user.id)
    if screen is None:
        await answer_callback(callback, _("referral.transfer.empty"), alert=True)
        return
    await render(callback, screen)


@router.callback_query(RefCb.filter(F.a == "trc"))
async def ask_transfer_amount(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    _main, referral = await service.balances(ctx, callback.from_user.id)
    await start_input(state, RefInput.transfer)
    text = _("referral.transfer.ask", balance=_.money(referral))
    await render(callback, Screen(text, kb([[Button.cb(_("nav.cancel"), RefCb(a="tr"))]])))


@router.message(RefInput.transfer, TEXT_INPUT)
async def transfer_amount_entered(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await input_data(state)
    if message.from_user is None:
        return
    _main, referral = await service.balances(ctx, message.from_user.id)
    rubles = parse_rubles(message.text)
    if rubles is None or not 0 < rubles * 100 <= referral:
        text = _("referral.transfer.invalid", balance=_.money(referral))
        await render(message, Screen(text, kb([[Button.cb(_("nav.cancel"), RefCb(a="tr"))]])))
        return
    await state.clear()
    await render(message, _confirm_transfer(_, rubles * 100))


@router.callback_query(RefCb.filter(F.a == "tro"))
async def do_transfer(callback: CallbackQuery, callback_data: RefCb, ctx: AppContext, _: Translator) -> None:
    user_id = callback.from_user.id
    if callback_data.v <= 0 or not callback_data.s:
        await answer_callback(callback, _("error.expired_button"), alert=True)
        return
    try:
        async with ctx.session_factory.begin() as session:
            result = await partner.transfer_to_main(
                session, user_id, callback_data.v, ref=f"transfer:{user_id}:{callback_data.s}"
            )
    except PartnerError as exc:
        key = "referral.transfer.no_funds" if exc.code is PartnerErrorCode.INSUFFICIENT_FUNDS else "error.alert"
        await answer_callback(callback, _(key), alert=True)
        return
    main, referral = await service.balances(ctx, user_id)
    text = _(
        "referral.transfer.done" if result.applied else "referral.transfer.already",
        amount=_.money(result.amount_kop),
        main=_.money(main),
        partner=_.money(referral),
    )
    rows = [[nav(_("referral.to_balance"), "balance")], _back_main(_)]
    await render(callback, Screen(text, kb(rows)))


# --- payout requests --------------------------------------------------------------------------------------------------
async def withdraw_screen(ctx: AppContext, _: Translator, user_id: int) -> Screen:
    settings = ctx.settings
    async with read_session(ctx.session_factory) as session:
        open_request = await partner.open_withdrawal(session, user_id)
    _main, referral = await service.balances(ctx, user_id)
    if open_request is not None:
        text = _(
            "referral.withdraw.has_open",
            id=open_request.id,
            amount=_.money(open_request.amount_kop),
            status=_status_title(_, open_request),
        )
        return Screen(text, kb([[Button.cb(_("referral.withdrawals.button"), RefCb(a="wdl"))], _back_main(_)]))
    if referral < settings.min_withdrawal_kop:
        text = _("referral.withdraw.too_little", min=_.money(settings.min_withdrawal_kop), balance=_.money(referral))
        return Screen(text, kb([_back_main(_)]))
    rows = [[Button.cb(_method_title(_, ctx, m), RefCb(a="wdm", s=m))] for m in settings.withdrawal_methods]
    text = _("referral.withdraw.text", balance=_.money(referral), min=_.money(settings.min_withdrawal_kop))
    return Screen(text, kb([*rows, _back_main(_)]))


def _ask_amount(ctx: AppContext, _: Translator, referral: int) -> Screen:
    rows = [
        [Button.cb(_("referral.withdraw.all", amount=_.money(referral)), RefCb(a="wda", v=referral))],
        [Button.cb(_("nav.cancel"), RefCb(a="main"))],
    ]
    text = _("referral.withdraw.ask_amount", balance=_.money(referral), min=_.money(ctx.settings.min_withdrawal_kop))
    return Screen(text, kb(rows))


def _ask_details(ctx: AppContext, _: Translator, method: str) -> Screen:
    key = f"referral.details.{method}"
    text = _(key) if ctx.i18n.has(key) else _("referral.details.default", method=_.esc(method))
    return Screen(text, kb([[Button.cb(_("nav.cancel"), RefCb(a="main"))]]))


@router.callback_query(RefCb.filter(F.a == "wd"))
async def open_withdraw(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    if not _withdrawals(ctx):
        await answer_callback(callback, _("referral.withdraw.unavailable"), alert=True)
        return
    await render(callback, await withdraw_screen(ctx, _, callback.from_user.id))


@router.callback_query(RefCb.filter(F.a == "wdm"))
async def choose_method(
    callback: CallbackQuery, callback_data: RefCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    if not _withdrawals(ctx) or callback_data.s not in ctx.settings.withdrawal_methods:
        await answer_callback(callback, _("referral.withdraw.unavailable"), alert=True)
        return
    _main, referral = await service.balances(ctx, callback.from_user.id)
    await start_input(state, RefInput.amount, method=callback_data.s)
    await render(callback, _ask_amount(ctx, _, referral))


async def _amount_chosen(
    event: Message | CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext, user_id: int, amount_kop: int
) -> None:
    settings = ctx.settings
    _main, referral = await service.balances(ctx, user_id)
    if not settings.min_withdrawal_kop <= amount_kop <= referral:
        text = _("referral.withdraw.bad_amount", min=_.money(settings.min_withdrawal_kop), balance=_.money(referral))
        await render(event, Screen(text, kb([[Button.cb(_("nav.cancel"), RefCb(a="main"))]])))
        return
    data = await state.get_data()
    await state.set_state(RefInput.details)
    await touch_input(state, amount=amount_kop)
    await render(event, _ask_details(ctx, _, str(data.get("method", ""))))


@router.message(RefInput.amount, TEXT_INPUT)
async def withdraw_amount_entered(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await input_data(state)
    if message.from_user is None:
        return
    rubles = parse_rubles(message.text)
    await _amount_chosen(message, ctx, _, state, message.from_user.id, (rubles or 0) * 100)


@router.callback_query(RefCb.filter(F.a == "wda"))
async def withdraw_all(
    callback: CallbackQuery, callback_data: RefCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    if await state.get_state() != RefInput.amount.state:
        await answer_callback(callback, _("error.expired_button"), alert=True)
        return
    await _amount_chosen(callback, ctx, _, state, callback.from_user.id, callback_data.v)


@router.message(RefInput.details, TEXT_INPUT)
async def details_entered(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    data = await input_data(state)
    details = " ".join((message.text or "").split())
    if not details or len(details) > partner.MAX_DETAILS_LENGTH:
        await render(message, _ask_details(ctx, _, str(data.get("method", ""))))
        return
    await touch_input(state, details=details)
    text = _(
        "referral.withdraw.confirm",
        amount=_.money(int(data.get("amount", 0))),
        method=_.esc(_method_title(_, ctx, str(data.get("method", "")))),
        details=_.esc(details),
    )
    rows = [
        [Button.cb(_("referral.withdraw.send"), RefCb(a="wdok"), style="success")],
        [Button.cb(_("nav.cancel"), RefCb(a="main"))],
    ]
    await render(message, Screen(text, kb(rows)))


_PARTNER_ERRORS = {
    PartnerErrorCode.DISABLED: "referral.withdraw.unavailable",
    PartnerErrorCode.BELOW_MINIMUM: "referral.withdraw.error.below_minimum",
    PartnerErrorCode.INSUFFICIENT_FUNDS: "referral.withdraw.error.funds",
    PartnerErrorCode.PENDING_EXISTS: "referral.withdraw.error.pending",
    PartnerErrorCode.BAD_DETAILS: "referral.withdraw.error.details",
    PartnerErrorCode.BAD_METHOD: "referral.withdraw.unavailable",
}


@router.callback_query(RefCb.filter(F.a == "wdok"))
async def send_withdrawal(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    if await state.get_state() != RefInput.details.state or not data.get("details"):
        await answer_callback(callback, _("error.expired_button"), alert=True)
        return
    await state.clear()
    user_id = callback.from_user.id
    try:
        async with ctx.session_factory.begin() as session:
            withdrawal = await partner.request_withdrawal(
                session,
                ctx.settings,
                user_id,
                int(data.get("amount", 0)),
                str(data.get("method", "")),
                str(data.get("details", "")),
            )
    except PartnerError as exc:
        key = _PARTNER_ERRORS.get(exc.code, "error.alert")
        await render(callback, Screen(_(key), kb([_back_main(_)])))
        return
    await service.notify_withdrawal_requested(ctx, withdrawal, callback.from_user.username)
    text = _("referral.withdraw.sent", id=withdrawal.id, amount=_.money(withdrawal.amount_kop))
    await render(
        callback, Screen(text, kb([[Button.cb(_("referral.withdrawals.button"), RefCb(a="wdl"))], _back_main(_)]))
    )


@router.callback_query(RefCb.filter(F.a == "wdl"))
async def list_withdrawals(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    async with read_session(ctx.session_factory) as session:
        rows = await partner.list_for_user(session, callback.from_user.id, limit=WITHDRAWALS_SHOWN)
    if not rows:
        text = _("referral.withdrawals.empty")
    else:
        items = [
            _(
                "referral.withdrawals.item",
                id=w.id,
                amount=_.money(w.amount_kop),
                method=_.esc(_method_title(_, ctx, w.method)),
                status=_status_title(_, w),
                date=_.date(w.created_at),
            )
            for w in rows
        ]
        text = _("referral.withdrawals.title") + "\n\n" + "\n\n".join(items)
    await render(callback, Screen(text, kb([_back_main(_)])))
