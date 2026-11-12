"""Payments section of the bot admin: filtered list, details, provider check, manual credit, re-run, Stars refund."""

from __future__ import annotations

import logging

from aiogram import F
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.core.context import AppContext
from app.core.filters import HasPermission, Permission, has_permission
from app.core.i18n import Translator
from app.core.ui import Screen, answer_callback, render
from app.db.models import AdminRole, Payment, PaymentStatus, Plan
from app.db.session import read_session
from app.keyboards import Btn, Button, Nav, kb, nav
from app.keyboards.common import confirm_kb
from app.modules.admin.common import (
    PAGE_SIZE,
    admin_input_group,
    admin_router,
    first_time,
    forget,
    parse_int,
    to_admin,
    when,
)
from app.modules.admin_payments import ops
from app.services.hooks import HookOutcome

log = logging.getLogger(__name__)

router = admin_router("admin_payments")

PAYMENTS_NAV = "adm_pay"
LIST_FILTERS = ("all", "ok", "open", "fail", "ref")
STATUS_ICON = {
    PaymentStatus.CREATED: "🆕",
    PaymentStatus.PENDING: "⏳",
    PaymentStatus.SUCCEEDED: "✅",
    PaymentStatus.CANCELED: "✖️",
    PaymentStatus.EXPIRED: "⌛️",
    PaymentStatus.REFUNDED: "↩️",
}
CREDITABLE = (*ops.OPEN, *ops.FAILED)


class PayCb(CallbackData, prefix="apay"):
    a: str  # v (view) | chk | paid | paid_ok | hook | rf | rf_ok
    id: str
    arg: str = ""


class PayListCb(CallbackData, prefix="apl"):
    f: str = "all"
    page: int = 0


@admin_input_group
class PayInput(StatesGroup):
    query = State()


# --- list -------------------------------------------------------------------------------------------------------------


def row_label(_: Translator, payment: Payment) -> str:
    icon = STATUS_ICON.get(payment.status, "•")
    created = payment.created_at.astimezone(_.tz) if _.tz is not None else payment.created_at
    return f"{icon} {_.money(payment.amount_kop)} · {payment.provider} · {payment.user_id} · {created:%d.%m %H:%M}"[:64]


async def list_screen(ctx: AppContext, _: Translator, name: str, page: int, note: str | None = None) -> Screen:
    _where, known = ops.status_filter(name)
    if not known:
        name = "all"
    page = max(0, page)
    items = await ops.page(ctx, name, page * PAGE_SIZE, PAGE_SIZE + 1)
    filters = [
        Button.cb(("• " if f == name else "") + _(f"admin_payments.filter.{f}"), PayListCb(f=f)) for f in LIST_FILTERS
    ]
    rows: list[list[Btn]] = [filters[:3], filters[3:]]
    rows += [[Button.cb(row_label(_, p), PayCb(a="v", id=p.id))] for p in items[:PAGE_SIZE]]
    pager = [
        Button.cb("◀️", PayListCb(f=name, page=page - 1)) if page > 0 else None,
        Button.cb("▶️", PayListCb(f=name, page=page + 1)) if len(items) > PAGE_SIZE else None,
    ]
    rows.append([b for b in pager if b is not None])
    rows.append([Button.cb(_("admin_payments.btn.search"), PayListCb(f="search")), to_admin(_)])
    title = (
        _(f"admin_payments.filter.{name}")
        if name in LIST_FILTERS
        else _("admin_payments.filter.user", user_id=name[1:])
    )
    text = _("admin_payments.list", title=title, page=page + 1)
    if not items:
        text += "\n\n" + _("admin_payments.list_empty")
    if note:
        text = f"{note}\n\n{text}"
    return Screen(text, kb(rows))


@router.callback_query(Nav.filter(F.to == PAYMENTS_NAV), HasPermission(Permission.PAYMENTS_VIEW))
async def show_payments(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await list_screen(ctx, _, "all", 0))


@router.callback_query(PayListCb.filter(F.f == "search"), HasPermission(Permission.PAYMENTS_VIEW))
async def ask_search(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    await state.set_state(PayInput.query)
    await render(callback, Screen(_("admin_payments.search_prompt"), kb([[nav(_("nav.back"), PAYMENTS_NAV)]])))


@router.callback_query(PayListCb.filter(), HasPermission(Permission.PAYMENTS_VIEW))
async def show_page(callback: CallbackQuery, callback_data: PayListCb, ctx: AppContext, _: Translator) -> None:
    await render(callback, await list_screen(ctx, _, callback_data.f, callback_data.page))


@router.message(PayInput.query, HasPermission(Permission.PAYMENTS_VIEW))
async def search_input(
    message: Message, ctx: AppContext, _: Translator, admin_role: AdminRole, state: FSMContext
) -> None:
    query = (message.text or "").strip()
    user_id = parse_int(query)
    if user_id is not None:
        await state.clear()
        await render(message, await list_screen(ctx, _, f"u{user_id}", 0))
        return
    payment = await ops.find(ctx, query)
    if payment is None:
        await render(
            message,
            Screen(
                _("admin_payments.not_found_query") + "\n\n" + _("admin_payments.search_prompt"),
                kb([[nav(_("nav.back"), PAYMENTS_NAV)]]),
            ),
        )
        return
    await state.clear()
    await render(message, await details_screen(ctx, _, admin_role, payment.id))


# --- details ----------------------------------------------------------------------------------------------------------


async def details_screen(
    ctx: AppContext, _: Translator, role: AdminRole, payment_id: str, note: str | None = None
) -> Screen:
    async with read_session(ctx.session_factory) as session, session.begin():
        payment = await session.get(Payment, payment_id)
        plan = await session.get(Plan, payment.plan_id) if payment is not None and payment.plan_id else None
    if payment is None:
        return await list_screen(ctx, _, "all", 0, _("admin.not_found"))
    if payment.is_donation:
        purpose = _("admin_payments.purpose.donation")
    elif payment.plan_id is not None:
        key = "admin_payments.purpose.gift" if payment.is_gift else "admin_payments.purpose.plan"
        purpose = _(key, plan=_.esc(plan.title if plan is not None else f"#{payment.plan_id}"))
    else:
        purpose = _("admin_payments.purpose.topup")
    if payment.is_recurring:
        purpose += " · " + _("admin_payments.purpose.recurring")
    provider_amount = (
        f"{_.esc(payment.provider_amount)} {_.esc(payment.provider_currency or '')}".strip()
        if payment.provider_amount
        else "—"
    )
    lines = [
        _("admin_payments.details.title", id=payment.id),
        _("admin_payments.details.status", status=_(f"admin.payment_status.{payment.status.value}")),
        _("admin_payments.details.amount", amount=_.money(payment.amount_kop), provider_amount=provider_amount),
        _(
            "admin_payments.details.provider",
            provider=_.esc(payment.provider),
            provider_id=_.esc(payment.provider_payment_id or "—"),
        ),
        _("admin_payments.details.user", user_id=payment.user_id),
        _("admin_payments.details.purpose", purpose=purpose),
        _("admin_payments.details.dates", created=when(_, payment.created_at), credited=when(_, payment.credited_at)),
    ]
    if payment.receipt_file_id:
        lines.append(_("admin_payments.details.receipt", kind=payment.receipt_media_type or "file"))
    if note:
        lines.insert(0, f"{note}\n")
    edit = has_permission(role, Permission.PAYMENTS_EDIT)
    pid = payment.id
    gateway = ctx.gateways.get(payment.provider)
    rows: list[list[Btn | None]] = []
    if payment.status in ops.OPEN and gateway is not None and gateway.supports_polling:
        rows.append([Button.cb(_("admin_payments.btn.check"), PayCb(a="chk", id=pid))])
    if edit and payment.status in CREDITABLE:
        rows.append([Button.cb(_("admin_payments.btn.mark_paid"), PayCb(a="paid", id=pid))])
    if payment.receipt_file_id:
        rows.append([Button.cb(_("admin_payments.btn.receipt"), PayCb(a="receipt", id=pid))])
    if edit and payment.status == PaymentStatus.SUCCEEDED:
        rows.append([Button.cb(_("admin_payments.btn.rerun"), PayCb(a="hook", id=pid))])
        if ops.can_refund_stars(payment):
            rows.append([Button.cb(_("admin_payments.btn.refund"), PayCb(a="rf", id=pid))])
    rows.append(
        [
            Button.cb(_("admin_payments.btn.user"), Nav(to="adm_user", arg=str(payment.user_id)))
            if has_permission(role, Permission.USERS_VIEW)
            else None,
            Button.cb(_("admin_payments.btn.user_payments"), PayListCb(f=f"u{payment.user_id}")),
        ]
    )
    rows.append([nav(_("admin_payments.btn.back"), PAYMENTS_NAV), to_admin(_)])
    return Screen("\n".join(lines), kb(rows))


@router.callback_query(PayCb.filter(F.a == "v"), HasPermission(Permission.PAYMENTS_VIEW))
async def show_details(
    callback: CallbackQuery, callback_data: PayCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    await render(callback, await details_screen(ctx, _, admin_role, callback_data.id))


@router.callback_query(PayCb.filter(F.a == "receipt"), HasPermission(Permission.PAYMENTS_VIEW))
async def send_receipt(callback: CallbackQuery, callback_data: PayCb, ctx: AppContext, _: Translator) -> None:
    payment = await ops.get(ctx, callback_data.id)
    if payment is None or not payment.receipt_file_id:
        await answer_callback(callback, _("admin.not_found"), alert=True)
        return
    await answer_callback(callback, _("admin.working"))
    if payment.receipt_media_type == "photo":
        await ctx.bot.send_photo(callback.from_user.id, payment.receipt_file_id)
    else:
        await ctx.bot.send_document(callback.from_user.id, payment.receipt_file_id)


@router.callback_query(PayCb.filter(F.a == "chk"), HasPermission(Permission.PAYMENTS_VIEW))
async def check_provider(
    callback: CallbackQuery, callback_data: PayCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    await answer_callback(callback, _("admin.working"))
    try:
        status = await ops.check(ctx, callback_data.id)
    except ops.PaymentOpError as exc:
        note = _error(_, exc)
    else:
        note = _("admin_payments.checked", status=_(f"admin.payment_status.{status.value}"))
    await render(callback, await details_screen(ctx, _, admin_role, callback_data.id, note))


# --- manual credit and re-run -----------------------------------------------------------------------------------------


@router.callback_query(PayCb.filter(F.a == "paid"), HasPermission(Permission.PAYMENTS_EDIT))
async def ask_mark_paid(callback: CallbackQuery, callback_data: PayCb, ctx: AppContext, _: Translator) -> None:
    payment = await ops.get(ctx, callback_data.id)
    if payment is None:
        await answer_callback(callback, _("admin.not_found"), alert=True)
        return
    text = _("admin_payments.confirm_paid", amount=_.money(payment.amount_kop), user_id=payment.user_id)
    yes = PayCb(a="paid_ok", id=payment.id)
    await render(callback, Screen(text, confirm_kb(_, yes, PayCb(a="v", id=payment.id), danger=True)))


@router.callback_query(PayCb.filter(F.a.in_({"paid_ok", "hook"})), HasPermission(Permission.PAYMENTS_EDIT))
async def credit_or_rerun(
    callback: CallbackQuery, callback_data: PayCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    await answer_callback(callback, _("admin.working"))
    try:
        if callback_data.a == "paid_ok":
            outcome = await ops.mark_paid(ctx, callback_data.id, callback.from_user.id)
        else:
            outcome = await ops.rerun_hook(ctx, callback_data.id, callback.from_user.id)
    except ops.PaymentOpError as exc:
        note = _error(_, exc)
    except Exception:
        log.exception("admin action on payment %s failed", callback_data.id)
        note = _("admin_payments.error.failed")
    else:
        note = outcome_text(_, outcome, credited=callback_data.a == "paid_ok")
    await render(callback, await details_screen(ctx, _, admin_role, callback_data.id, note))


def outcome_text(_: Translator, outcome: HookOutcome, *, credited: bool) -> str:
    parts = [_("admin_payments.outcome.credited" if credited else "admin_payments.outcome.rerun")]
    if outcome.purchase_new:
        parts.append(_("admin_payments.outcome.purchase"))
    elif outcome.purchase_id is not None:
        parts.append(_("admin_payments.outcome.purchase_before"))
    if outcome.gift_code:
        parts.append(_("admin_payments.outcome.gift", code=_.esc(outcome.gift_code)))
    if outcome.error is not None:
        parts.append(_("admin_payments.outcome.error", reason=_.esc(outcome.error.value)))
    if outcome.notified_users:
        parts.append(_("admin_payments.outcome.notified", count=len(outcome.notified_users)))
    return "\n".join(parts)


# --- Stars refund -----------------------------------------------------------------------------------------------------


@router.callback_query(PayCb.filter(F.a == "rf"), HasPermission(Permission.PAYMENTS_EDIT))
async def ask_refund(callback: CallbackQuery, callback_data: PayCb, ctx: AppContext, _: Translator) -> None:
    payment = await ops.get(ctx, callback_data.id)
    if payment is None or not ops.can_refund_stars(payment):
        await answer_callback(callback, _("admin_payments.error.not_refundable"), alert=True)
        return
    text = _("admin_payments.confirm_refund", amount=_.esc(payment.provider_amount or "?"), user_id=payment.user_id)
    debit = Button.cb(_("admin_payments.btn.refund_debit"), PayCb(a="rf_ok", id=payment.id, arg="d"), style="danger")
    rows: list[list[Btn | None]] = [
        [None if payment.is_donation else debit],
        [Button.cb(_("admin_payments.btn.refund_keep"), PayCb(a="rf_ok", id=payment.id, arg="k"))],
        [Button.cb(_("nav.cancel"), PayCb(a="v", id=payment.id))],
    ]
    await render(callback, Screen(text, kb(rows)))


@router.callback_query(PayCb.filter(F.a == "rf_ok"), HasPermission(Permission.PAYMENTS_EDIT))
async def refund(
    callback: CallbackQuery, callback_data: PayCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    key = f"refund:{callback_data.id}"
    if not first_time(key):
        await answer_callback(callback)
        return
    await answer_callback(callback, _("admin.working"))
    try:
        result = await ops.refund_stars(ctx, callback_data.id, callback.from_user.id, debit=callback_data.arg == "d")
    except ops.PaymentOpError as exc:
        forget(key)
        note = _error(_, exc)
    else:
        note = _("admin_payments.refunded", debited=_.money(result.debited_kop))
    await render(callback, await details_screen(ctx, _, admin_role, callback_data.id, note))


def _error(_: Translator, exc: ops.PaymentOpError) -> str:
    text = _(f"admin_payments.error.{exc.code}")
    if exc.detail:
        text += f"\n<code>{_.esc(exc.detail)}</code>"
    return text
