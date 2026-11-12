"""Cost sharing screens: the member's "I paid" button and the owner's monthly overview."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton

from app.core.filters import IsPrivate
from app.core.i18n import Translator
from app.core.permissions import HasPermission, Permission
from app.core.ui import Screen, answer_callback, render, send_screen
from app.keyboards import Button, Nav, kb, nav
from app.modules.admin.common import admin_router, to_admin, user_short
from app.modules.cost_sharing import service
from app.services.notify import notify_admins
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.core.context import AppContext
    from app.db.models import User

log = logging.getLogger(__name__)

NAV = "adm_cost"


class CostCb(CallbackData, prefix="cost"):
    """``action``: paid (member) | remind | start (owner); ``period``: ``YYYY-MM``."""

    action: str
    period: str


router = Router(name="cost_sharing")
router.callback_query.filter(IsPrivate())
admin = admin_router("cost_sharing_admin")
admin.callback_query.filter(HasPermission(Permission.USERS_EDIT))


def month_name(i18n_t: Translator, period: str) -> str:
    year, month = period.split("-", 1)
    return i18n_t("cost_sharing.month_year", month=i18n_t(f"cost_sharing.month.{int(month)}"), year=year)


def reminder_screen(ctx: AppContext, lang: str, record: service.Period, details: str) -> Screen:
    _ = ctx.i18n.translator(lang, ctx.settings.zone)
    text = _("cost_sharing.reminder", month=month_name(_, record.period), share=_.money(record.share_kop))
    if details:
        text += "\n\n" + _("cost_sharing.details", details=_.esc(details))
    markup = kb([[Button.cb(_("cost_sharing.btn.paid"), CostCb(action="paid", period=record.period), style="success")]])
    return Screen(text, markup)


async def remind(ctx: AppContext, record: service.Period, user_ids: list[int]) -> int:
    """Send the monthly reminder to ``user_ids``; returns how many got it."""
    config = service.config_from(ctx.settings)
    users = await service.names(ctx.session_factory, user_ids)
    sent = 0
    for uid in user_ids:
        user = users.get(uid)
        if user is None or user.bot_blocked:
            continue
        try:
            await send_screen(
                ctx.bot, uid, reminder_screen(ctx, ctx.i18n.normalize(user.language), record, config.details)
            )
            sent += 1
        except Exception as exc:
            log.info("cost sharing: could not remind %s: %s", uid, exc)
    return sent


@router.callback_query(CostCb.filter(F.action == "paid"))
async def member_paid(
    callback: CallbackQuery, callback_data: CostCb, ctx: AppContext, db_user: User | None, _: Translator
) -> None:
    if db_user is None:
        await answer_callback(callback)
        return
    record = await service.mark_paid(ctx.session_factory, callback_data.period, db_user.tg_id)
    if record is None:
        await answer_callback(callback, _("cost_sharing.unknown"), alert=True)
        return
    month = month_name(_, record.period)
    await render(callback, Screen(_("cost_sharing.thanks", month=month), kb([[nav(_("nav.menu"), "menu")]])))
    owner_lang = ctx.settings.default_language
    owner_t = ctx.i18n.translator(owner_lang, ctx.settings.zone)
    text = owner_t(
        "cost_sharing.owner_paid",
        name=owner_t.esc(user_short(db_user)),
        share=owner_t.money(record.share_kop),
        month=month_name(owner_t, record.period),
        paid=len(record.paid),
        total=len(record.members),
    )
    await notify_admins(ctx.bot, ctx.settings, text)


# --- owner ---------------------------------------------------------------------------------------------------------


async def overview(ctx: AppContext, _: Translator) -> Screen:
    config = service.config_from(ctx.settings)
    period = service.period_of(utcnow(), ctx.settings)
    record = await service.get_period(ctx.session_factory, period)
    rows: list[list[InlineKeyboardButton | None]] = []
    if not config.ready:
        text = _("cost_sharing.not_configured")
    elif record is None:
        text = _(
            "cost_sharing.not_started", month=month_name(_, period), day=config.day, total=_.money(config.total_kop)
        )
        rows.append([Button.cb(_("cost_sharing.btn.start"), CostCb(action="start", period=period))])
    else:
        users = await service.names(ctx.session_factory, record.members)
        lines = [
            _(
                "cost_sharing.overview",
                month=month_name(_, period),
                total=_.money(record.total_kop),
                share=_.money(record.share_kop),
                paid=len(record.paid),
                count=len(record.members),
            ),
            "",
        ]
        for uid in record.members:
            user = users.get(uid)
            label = _.esc(user_short(user)) if user is not None else str(uid)
            lines.append(f"{'✅' if uid in record.paid else '⏳'} {label}")
        text = "\n".join(lines)
        if record.unpaid:
            rows.append([Button.cb(_("cost_sharing.btn.remind"), CostCb(action="remind", period=period))])
    rows.append([nav(_("nav.refresh"), NAV)])
    rows.append([to_admin(_)])
    return Screen(text, kb(rows))


@admin.callback_query(Nav.filter(F.to == NAV))
async def open_overview(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await overview(ctx, _))


@admin.callback_query(CostCb.filter(F.action.in_({"remind", "start"})))
async def owner_actions(callback: CallbackQuery, callback_data: CostCb, ctx: AppContext, _: Translator) -> None:
    sent = 0
    if callback_data.action == "start":
        config = service.config_from(ctx.settings)
        record = await service.start_period(ctx.session_factory, ctx.settings, config, callback_data.period, utcnow())
        if record is not None:
            sent = await remind(ctx, record, record.members)
    else:
        existing = await service.get_period(ctx.session_factory, callback_data.period)
        if existing is not None:
            sent = await remind(ctx, existing, existing.unpaid)
    await render(callback, await overview(ctx, _), answer=_("cost_sharing.sent", count=sent))
