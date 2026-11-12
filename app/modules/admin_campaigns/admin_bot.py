"""Advertising campaigns in the bot admin: tracked /start links with a registration -> trial -> purchase funnel."""

from __future__ import annotations

import logging

from aiogram import F
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.core.context import AppContext
from app.core.filters import HasPermission, Permission
from app.core.i18n import Translator
from app.core.ui import Screen, render
from app.db.session import read_session
from app.keyboards import Btn, Button, Nav, kb, nav
from app.modules.admin.common import admin_input_group, admin_router, to_admin, when
from app.services import campaigns

log = logging.getLogger(__name__)

router = admin_router("admin_campaigns")

CAMPAIGNS_NAV = "adm_camp"
MAX_LISTED = 30


class CampCb(CallbackData, prefix="admk"):
    a: str  # card | pause | resume | new
    code: str = ""


@admin_input_group
class CampInput(StatesGroup):
    create = State()


def percent(value: float) -> str:
    return f"{value * 100:.1f}".rstrip("0").rstrip(".")


def start_link(ctx: AppContext, code: str) -> str | None:
    if not ctx.bot_username:
        return None
    return f"https://t.me/{ctx.bot_username}?start={campaigns.deep_link_payload(code)}"


async def list_screen(ctx: AppContext, _: Translator, note: str | None = None) -> Screen:
    async with read_session(ctx.session_factory) as session, session.begin():
        items = await campaigns.stats(session)
    rows: list[list[Btn]] = []
    for item in items[:MAX_LISTED]:
        mark = "▶️" if item.is_active else "⏸"
        label = f"{mark} {item.code} · 👤 {item.registrations} · 💳 {item.paying_users} · {_.money(item.revenue_kop)}"
        rows.append([Button.cb(label[:64], CampCb(a="card", code=item.code))])
    rows.append([Button.cb(_("admin_campaigns.btn.new"), CampCb(a="new"))])
    rows.append([nav(_("nav.refresh"), CAMPAIGNS_NAV), to_admin(_)])
    text = _("admin_campaigns.list") if items else _("admin_campaigns.list_empty")
    if note:
        text = f"{note}\n\n{text}"
    return Screen(text, kb(rows))


@router.callback_query(Nav.filter(F.to == CAMPAIGNS_NAV), HasPermission(Permission.PROMO))
async def show_list(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await list_screen(ctx, _))


async def card_screen(ctx: AppContext, _: Translator, code: str, note: str | None = None) -> Screen:
    async with read_session(ctx.session_factory) as session, session.begin():
        campaign = await campaigns.get(session, code)
        stats = await campaigns.stats(session, code=code) if campaign is not None else []
    if campaign is None or not stats:
        return await list_screen(ctx, _, _("admin.not_found"))
    item = stats[0]
    link = start_link(ctx, campaign.code)
    lines = [
        _("admin_campaigns.card.title", name=_.esc(campaign.name), code=_.esc(campaign.code)),
        _(
            "admin_campaigns.card.status",
            status=_("admin_campaigns.card.active" if campaign.is_active else "admin_campaigns.card.paused"),
            created=when(_, campaign.created_at),
        ),
        "",
        _(
            "admin_campaigns.card.funnel",
            registrations=_.number(item.registrations),
            trials=_.number(item.trials),
            paying=_.number(item.paying_users),
            conversion=percent(item.conversion),
            purchases=_.number(item.purchases),
            revenue=_.money(item.revenue_kop),
        ),
    ]
    if link:
        lines += ["", _("admin_campaigns.card.link", link=_.esc(link))]
    if note:
        lines.insert(0, f"{note}\n")
    toggle = (
        Button.cb(_("admin_campaigns.btn.pause"), CampCb(a="pause", code=campaign.code))
        if campaign.is_active
        else Button.cb(_("admin_campaigns.btn.resume"), CampCb(a="resume", code=campaign.code))
    )
    rows: list[list[Btn | None]] = [
        [Button.copy(_("admin_campaigns.btn.copy"), link) if link and len(link) <= 256 else None],
        [toggle],
        [nav(_("admin_campaigns.btn.back"), CAMPAIGNS_NAV), to_admin(_)],
    ]
    return Screen("\n".join(lines), kb(rows))


@router.callback_query(CampCb.filter(F.a == "card"), HasPermission(Permission.PROMO))
async def show_card(callback: CallbackQuery, callback_data: CampCb, ctx: AppContext, _: Translator) -> None:
    await render(callback, await card_screen(ctx, _, callback_data.code))


@router.callback_query(CampCb.filter(F.a.in_({"pause", "resume"})), HasPermission(Permission.PROMO))
async def toggle(callback: CallbackQuery, callback_data: CampCb, ctx: AppContext, _: Translator) -> None:
    async with ctx.session_factory.begin() as session:
        await campaigns.set_active(session, callback_data.code, callback_data.a == "resume", callback.from_user.id)
    await render(callback, await card_screen(ctx, _, callback_data.code), answer=_("admin.saved"))


@router.callback_query(CampCb.filter(F.a == "new"), HasPermission(Permission.PROMO))
async def ask_new(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    await state.set_state(CampInput.create)
    await render(callback, Screen(_("admin_campaigns.ask"), kb([[nav(_("admin.cancel"), CAMPAIGNS_NAV)]])))


@router.message(CampInput.create, HasPermission(Permission.PROMO))
async def create(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    code, _sep, name = (message.text or "").strip().partition(" ")
    back = kb([[nav(_("admin.cancel"), CAMPAIGNS_NAV)]])
    actor = message.from_user.id if message.from_user else 0
    try:
        async with ctx.session_factory.begin() as session:
            campaign = await campaigns.create(session, code, name.strip(), actor)
    except campaigns.CampaignExists:
        await render(message, Screen(_("admin_campaigns.exists") + "\n\n" + _("admin_campaigns.ask"), back))
        return
    except ValueError:
        await render(message, Screen(_("admin_campaigns.invalid") + "\n\n" + _("admin_campaigns.ask"), back))
        return
    await state.clear()
    await render(message, await card_screen(ctx, _, campaign.code, _("admin_campaigns.created")))
