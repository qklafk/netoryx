"""Partner payout requests in the bot admin: queue with anti-fraud flags, approve, mark paid, reject with a comment."""

from __future__ import annotations

import logging

from aiogram import F
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.core.context import AppContext
from app.core.filters import HasPermission, Permission, has_permission
from app.core.i18n import Translator
from app.core.ui import Screen, answer_callback, render
from app.db.models import AdminRole, User, Withdrawal, WithdrawalStatus
from app.db.session import read_session
from app.keyboards import Btn, Button, Nav, kb, nav
from app.keyboards.common import confirm_kb
from app.modules.admin.common import (
    PAGE_SIZE,
    AdminInputCb,
    admin_input_group,
    admin_router,
    input_button,
    to_admin,
    user_short,
    user_title,
    when,
)
from app.services import partner
from app.services.notify import safe_send
from app.services.partner import FraudFlag, PartnerError

log = logging.getLogger(__name__)

router = admin_router("admin_withdrawals")

WITHDRAWALS_NAV = "adm_wd"
STATUS_ICON = {
    WithdrawalStatus.PENDING: "⏳",
    WithdrawalStatus.APPROVED: "👍",
    WithdrawalStatus.PAID: "✅",
    WithdrawalStatus.REJECTED: "✖️",
}
DONE = (WithdrawalStatus.PAID, WithdrawalStatus.REJECTED)


class WdCb(CallbackData, prefix="admw"):
    a: str  # card | approve | paid | paid_ok | reject | hist
    id: int = 0
    page: int = 0


@admin_input_group
class WdInput(StatesGroup):
    comment = State()  # data: id


def _label(_: Translator, withdrawal: Withdrawal, user: User | None) -> str:
    who = user_short(user) if user is not None else str(withdrawal.user_id)
    icon = STATUS_ICON.get(withdrawal.status, "•")
    return f"{icon} #{withdrawal.id} · {_.money(withdrawal.amount_kop)} · {withdrawal.method} · {who}"[:64]


# --- queue and history ------------------------------------------------------------------------------------------------


async def queue_screen(ctx: AppContext, _: Translator, note: str | None = None) -> Screen:
    async with read_session(ctx.session_factory) as session, session.begin():
        items = await partner.list_pending(session, limit=50, with_flags=False)
    rows: list[list[Btn]] = [
        [Button.cb(_label(_, item.withdrawal, item.user), WdCb(a="card", id=item.withdrawal.id))] for item in items
    ]
    rows.append([Button.cb(_("admin_withdrawals.btn.history"), WdCb(a="hist"))])
    rows.append([nav(_("nav.refresh"), WITHDRAWALS_NAV), to_admin(_)])
    total = sum(item.withdrawal.amount_kop for item in items)
    text = (
        _("admin_withdrawals.queue", count=len(items), total=_.money(total))
        if items
        else _("admin_withdrawals.queue_empty")
    )
    if not partner.withdrawals_available(ctx.settings):
        text += "\n\n" + _("admin_withdrawals.disabled_note")
    if note:
        text = f"{note}\n\n{text}"
    return Screen(text, kb(rows))


@router.callback_query(Nav.filter(F.to == WITHDRAWALS_NAV), HasPermission(Permission.WITHDRAWALS))
async def show_queue(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await queue_screen(ctx, _))


@router.callback_query(WdCb.filter(F.a == "hist"), HasPermission(Permission.WITHDRAWALS))
async def show_history(callback: CallbackQuery, callback_data: WdCb, ctx: AppContext, _: Translator) -> None:
    page = max(0, callback_data.page)
    stmt = (
        select(Withdrawal, User)
        .outerjoin(User, User.tg_id == Withdrawal.user_id)
        .where(Withdrawal.status.in_(DONE))
        .order_by(Withdrawal.id.desc())
        .offset(page * PAGE_SIZE)
        .limit(PAGE_SIZE + 1)
    )
    async with read_session(ctx.session_factory) as session, session.begin():
        items = (await session.execute(stmt)).all()
    rows: list[list[Btn]] = [[Button.cb(_label(_, w, u), WdCb(a="card", id=w.id))] for w, u in items[:PAGE_SIZE]]
    pager = [
        Button.cb("◀️", WdCb(a="hist", page=page - 1)) if page > 0 else None,
        Button.cb("▶️", WdCb(a="hist", page=page + 1)) if len(items) > PAGE_SIZE else None,
    ]
    rows.append([b for b in pager if b is not None])
    rows.append([nav(_("admin_withdrawals.btn.back"), WITHDRAWALS_NAV), to_admin(_)])
    text = _("admin_withdrawals.history", page=page + 1) if items else _("admin_withdrawals.history_empty")
    await render(callback, Screen(text, kb(rows)))


# --- card ------------------------------------------------------------------------------------------------------------


def flag_text(_: Translator, flag: FraudFlag) -> str:
    detail = dict(flag.detail)
    if "share" in detail:
        detail["share"] = round(float(detail["share"]) * 100)
    return _(f"admin_withdrawals.flag.{flag.code.value}", **detail)


async def card_screen(
    ctx: AppContext, _: Translator, role: AdminRole, withdrawal_id: int, note: str | None = None
) -> Screen:
    settings = ctx.settings
    async with read_session(ctx.session_factory) as session, session.begin():
        withdrawal = await session.get(Withdrawal, withdrawal_id)
        if withdrawal is None:
            return await queue_screen(ctx, _, _("admin.not_found"))
        user = await session.get(User, withdrawal.user_id)
        stats = await partner.partner_stats(session, settings, withdrawal.user_id)
        flags = await partner.fraud_flags(session, withdrawal.user_id, settings=settings)
    lines = [
        _("admin_withdrawals.card.title", id=withdrawal.id),
        _("admin_withdrawals.card.status", status=_(f"admin_withdrawals.status.{withdrawal.status.value}")),
        _(
            "admin_withdrawals.card.partner",
            user=user_title(user) if user is not None else f"<code>{withdrawal.user_id}</code>",
            since=_.date(user.created_at) if user is not None else "—",
        ),
        _(
            "admin_withdrawals.card.amount",
            amount=_.money(withdrawal.amount_kop),
            method=_.esc(withdrawal.method),
        ),
        _("admin_withdrawals.card.details", details=_.esc(withdrawal.details)),
        _(
            "admin_withdrawals.card.dates",
            created=when(_, withdrawal.created_at),
            processed=when(_, withdrawal.processed_at),
        ),
    ]
    if withdrawal.admin_comment:
        lines.append(_("admin_withdrawals.card.comment", comment=_.esc(withdrawal.admin_comment)))
    lines += [
        "",
        _(
            "admin_withdrawals.card.stats",
            earned=_.money(stats.total_earned_kop),
            users=_.number(stats.total_users),
            paid_users=_.number(stats.total_paid_users),
            withdrawn=_.money(stats.withdrawn_kop),
            balance=_.money(stats.referral_balance_kop),
        ),
    ]
    if flags:
        lines += ["", _("admin_withdrawals.card.flags")] + [f"• {flag_text(_, flag)}" for flag in flags]
    else:
        lines += ["", _("admin_withdrawals.card.no_flags")]
    if note:
        lines.insert(0, f"{note}\n")
    wid = withdrawal.id
    rows: list[list[Btn | None]] = []
    if withdrawal.status == WithdrawalStatus.PENDING:
        rows.append([Button.cb(_("admin_withdrawals.btn.approve"), WdCb(a="approve", id=wid))])
    if withdrawal.status in partner.OPEN_STATUSES:
        rows.append(
            [
                Button.cb(_("admin_withdrawals.btn.paid"), WdCb(a="paid", id=wid), style="success"),
                Button.cb(_("admin_withdrawals.btn.reject"), WdCb(a="reject", id=wid), style="danger"),
            ]
        )
    rows.append(
        [
            Button.cb(_("admin_withdrawals.btn.user"), Nav(to="adm_user", arg=str(withdrawal.user_id)))
            if has_permission(role, Permission.USERS_VIEW)
            else None
        ]
    )
    rows.append([nav(_("admin_withdrawals.btn.back"), WITHDRAWALS_NAV), to_admin(_)])
    return Screen("\n".join(lines), kb(rows))


@router.callback_query(WdCb.filter(F.a == "card"), HasPermission(Permission.WITHDRAWALS))
async def show_card(
    callback: CallbackQuery, callback_data: WdCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    await render(callback, await card_screen(ctx, _, admin_role, callback_data.id))


# --- decisions -------------------------------------------------------------------------------------------------------


async def _notify_partner(ctx: AppContext, withdrawal: Withdrawal) -> None:
    """Tell the partner about the decision in their language (after the commit)."""
    async with read_session(ctx.session_factory) as session, session.begin():
        language = await session.scalar(select(User.language).where(User.tg_id == withdrawal.user_id))
    if language is None:
        return
    i18n = ctx.i18n
    key = f"admin_withdrawals.notify.{withdrawal.status.value}"
    text = i18n.t(key, language, id=withdrawal.id, amount=i18n.money(withdrawal.amount_kop, language))
    if withdrawal.admin_comment:
        text += "\n" + i18n.t("admin_withdrawals.notify.comment", language, comment=i18n.esc(withdrawal.admin_comment))
    await safe_send(ctx.bot, withdrawal.user_id, text, session_factory=ctx.session_factory)


async def _decide(
    callback: CallbackQuery | Message,
    ctx: AppContext,
    _: Translator,
    role: AdminRole,
    withdrawal_id: int,
    action: str,
    comment: str | None = None,
) -> None:
    actor = callback.from_user.id if callback.from_user else 0
    operations = {"approve": partner.approve, "paid": partner.mark_paid, "reject": partner.reject}
    try:
        async with ctx.session_factory.begin() as session:
            withdrawal = await operations[action](session, withdrawal_id, actor, comment=comment)
    except PartnerError as exc:
        note = _(f"admin_withdrawals.error.{exc.code.value}")
    else:
        await _notify_partner(ctx, withdrawal)
        note = _(f"admin_withdrawals.done.{action}")
    await render(callback, await card_screen(ctx, _, role, withdrawal_id, note))


@router.callback_query(WdCb.filter(F.a == "approve"), HasPermission(Permission.WITHDRAWALS))
async def approve(
    callback: CallbackQuery, callback_data: WdCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    await _decide(callback, ctx, _, admin_role, callback_data.id, "approve")


@router.callback_query(WdCb.filter(F.a == "paid"), HasPermission(Permission.WITHDRAWALS))
async def ask_paid(callback: CallbackQuery, callback_data: WdCb, ctx: AppContext, _: Translator) -> None:
    async with read_session(ctx.session_factory) as session, session.begin():
        withdrawal = await session.get(Withdrawal, callback_data.id)
    if withdrawal is None:
        await answer_callback(callback, _("admin.not_found"), alert=True)
        return
    text = _(
        "admin_withdrawals.confirm_paid",
        amount=_.money(withdrawal.amount_kop),
        method=_.esc(withdrawal.method),
        details=_.esc(withdrawal.details),
    )
    yes = WdCb(a="paid_ok", id=withdrawal.id)
    await render(callback, Screen(text, confirm_kb(_, yes, WdCb(a="card", id=withdrawal.id))))


@router.callback_query(WdCb.filter(F.a == "paid_ok"), HasPermission(Permission.WITHDRAWALS))
async def mark_paid(
    callback: CallbackQuery, callback_data: WdCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    await _decide(callback, ctx, _, admin_role, callback_data.id, "paid")


@router.callback_query(WdCb.filter(F.a == "reject"), HasPermission(Permission.WITHDRAWALS))
async def ask_reject(callback: CallbackQuery, callback_data: WdCb, _: Translator, state: FSMContext) -> None:
    await state.set_state(WdInput.comment)
    await state.set_data({"id": callback_data.id})
    rows = [
        [input_button(_("admin_withdrawals.btn.reject_silent"), "wd_reject", callback_data.id)],
        [Button.cb(_("nav.back"), WdCb(a="card", id=callback_data.id))],
    ]
    await render(callback, Screen(_("admin_withdrawals.ask_comment"), kb(rows)))


@router.message(WdInput.comment, HasPermission(Permission.WITHDRAWALS))
async def reject_with_comment(
    message: Message, ctx: AppContext, _: Translator, admin_role: AdminRole, state: FSMContext
) -> None:
    data = await state.get_data()
    await state.clear()
    withdrawal_id = int(data.get("id", 0))
    comment = (message.text or "").strip() or None
    await _decide(message, ctx, _, admin_role, withdrawal_id, "reject", comment)


@router.callback_query(
    WdInput.comment, AdminInputCb.filter(F.action == "wd_reject"), HasPermission(Permission.WITHDRAWALS)
)
async def reject_without_comment(
    callback: CallbackQuery,
    callback_data: AdminInputCb,
    ctx: AppContext,
    _: Translator,
    admin_role: AdminRole,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    await state.clear()
    withdrawal_id = int(data.get("id", 0))
    if str(withdrawal_id) != callback_data.value:
        await answer_callback(callback, _("error.expired_button"))
        return
    await _decide(callback, ctx, _, admin_role, withdrawal_id, "reject")
