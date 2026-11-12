"""Plans section of the bot admin: list, create, edit every field, hide, archive and reorder."""

from __future__ import annotations

import logging
import re
from typing import Any

from aiogram import F
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import AppContext
from app.core.filters import HasPermission, Permission
from app.core.i18n import Translator
from app.core.ui import Screen, answer_callback, render
from app.db.models import Plan, Purchase, PurchaseKind, Subscription, SubscriptionStatus
from app.db.session import read_session
from app.keyboards import Btn, Button, Nav, kb, nav
from app.modules.admin.common import admin_input_group, admin_router, parse_amount_kop, parse_int, to_admin, yes_no
from app.services import audit, plans
from app.utils.time import MAX_PERIOD_DAYS, utcnow

log = logging.getLogger(__name__)

router = admin_router("admin_plans")

PLANS_NAV = "adm_plans"
MAX_LIMIT = 1_000_000
MAX_STARS = 100_000
# Editable through a text input: field -> i18n suffix of the prompt
TEXT_FIELDS = (
    "title",
    "duration_days",
    "price_kop",
    "price_stars",
    "device_limit",
    "traffic_limit_gb",
    "availability_rule",
    "allowed_user_ids",
)
TOGGLES = ("is_hidden", "includes_tg_proxy", "is_active")
CREATE_STEPS = ("title", "duration_days", "price_kop", "device_limit", "traffic_limit_gb")


class PlanCb(CallbackData, prefix="admp"):
    a: str  # card | edit | toggle | up | down | new
    id: int = 0
    f: str = ""  # field


@admin_input_group
class PlanInput(StatesGroup):
    edit = State()  # data: plan_id, field
    create = State()  # data: step, values


# --- list -------------------------------------------------------------------------------------------------------------


def plan_label(_: Translator, plan: Plan) -> str:
    marks = ("" if plan.is_active else "🗄 ") + ("🙈 " if plan.is_hidden else "")
    return f"{marks}{plan.title} · {_.money(plan.price_kop)} · {_.duration(plan.duration_days)}"[:64]


async def list_screen(ctx: AppContext, _: Translator, note: str | None = None) -> Screen:
    async with read_session(ctx.session_factory) as session, session.begin():
        items = await plans.list_all(session)
    rows: list[list[Btn]] = [[Button.cb(plan_label(_, plan), PlanCb(a="card", id=plan.id))] for plan in items]
    rows.append([Button.cb(_("admin_plans.btn.new"), PlanCb(a="new"))])
    rows.append([to_admin(_)])
    text = _("admin_plans.list") if items else _("admin_plans.list_empty")
    if note:
        text = f"{note}\n\n{text}"
    return Screen(text, kb(rows))


@router.callback_query(Nav.filter(F.to == PLANS_NAV), HasPermission(Permission.PLANS))
async def show_list(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await list_screen(ctx, _))


# --- card ------------------------------------------------------------------------------------------------------------


async def _plan_stats(session: AsyncSession, plan_id: int) -> tuple[int, int, int]:
    now = utcnow()
    active = await session.scalar(
        select(func.count(Subscription.id)).where(
            Subscription.plan_id == plan_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at > now,
        )
    )
    count, revenue = (
        await session.execute(
            select(func.count(Purchase.id), func.coalesce(func.sum(Purchase.price_kop), 0)).where(
                Purchase.plan_id == plan_id, Purchase.kind.in_((PurchaseKind.PURCHASE, PurchaseKind.AUTO_RENEW))
            )
        )
    ).one()
    return int(active or 0), int(count or 0), int(revenue or 0)


async def card_screen(ctx: AppContext, _: Translator, plan_id: int, note: str | None = None) -> Screen:
    settings = ctx.settings
    async with read_session(ctx.session_factory) as session, session.begin():
        plan = await plans.get(session, plan_id)
        stats = await _plan_stats(session, plan_id) if plan is not None else (0, 0, 0)
    if plan is None:
        return await list_screen(ctx, _, _("admin.not_found"))
    stars = (
        _.stars(plan.price_stars)
        if plan.price_stars
        else _("admin_plans.card.stars_auto", stars=_.stars(plans.stars_price(plan, settings)))
    )
    devices = _("fmt.unlimited") if plan.device_limit == 0 else _.plural(plan.device_limit, "plural.devices")
    traffic = _("fmt.unlimited") if plan.traffic_limit_gb == 0 else _.plural(plan.traffic_limit_gb, "plural.gb")
    lines = [
        _("admin_plans.card.title", title=_.esc(plan.title), id=plan.id),
        _("admin_plans.card.duration", duration=_.duration(plan.duration_days), days=plan.duration_days),
        _(
            "admin_plans.card.price",
            price=_.money(plan.price_kop),
            monthly=_.money(plans.monthly_price_kop(plan)),
            stars=stars,
        ),
        _("admin_plans.card.limits", devices=devices, traffic=traffic),
        _(
            "admin_plans.card.flags",
            status=_("admin_plans.card.active" if plan.is_active else "admin_plans.card.archived"),
            hidden=yes_no(_, plan.is_hidden),
            proxy=yes_no(_, plan.includes_tg_proxy),
            order=plan.sort_order,
        ),
        _(
            "admin_plans.card.audience",
            rule=_.esc(plan.availability_rule),
            users=_.number(len(plan.allowed_user_ids)),
        ),
        _("admin_plans.card.stats", active=_.number(stats[0]), purchases=_.number(stats[1]), revenue=_.money(stats[2])),
    ]
    if plan.is_hidden and ctx.bot_username:
        link = f"https://t.me/{ctx.bot_username}?start={plans.link_payload(ctx.secret_box, plan.id)}"
        lines.append(_("admin_plans.card.link", link=link))
    if note:
        lines.insert(0, f"{note}\n")
    pid = plan.id

    def edit(field: str) -> Btn:
        return Button.cb(_(f"admin_plans.btn.{field}"), PlanCb(a="edit", id=pid, f=field))

    def toggle(field: str, on: bool) -> Btn:
        return Button.cb(_(f"admin_plans.btn.{field}_{'off' if on else 'on'}"), PlanCb(a="toggle", id=pid, f=field))

    rows: list[list[Btn]] = [
        [edit("title"), edit("duration_days")],
        [edit("price_kop"), edit("price_stars")],
        [edit("device_limit"), edit("traffic_limit_gb")],
        [edit("availability_rule"), edit("allowed_user_ids")],
        [toggle("is_hidden", plan.is_hidden), toggle("includes_tg_proxy", plan.includes_tg_proxy)],
        [toggle("is_active", plan.is_active)],
        [Button.cb("⬆️", PlanCb(a="up", id=pid)), Button.cb("⬇️", PlanCb(a="down", id=pid))],
        [nav(_("admin_plans.btn.back"), PLANS_NAV), to_admin(_)],
    ]
    return Screen("\n".join(lines), kb(rows))


@router.callback_query(PlanCb.filter(F.a == "card"), HasPermission(Permission.PLANS))
async def show_card(callback: CallbackQuery, callback_data: PlanCb, ctx: AppContext, _: Translator) -> None:
    await render(callback, await card_screen(ctx, _, callback_data.id))


# --- editing ----------------------------------------------------------------------------------------------------------


def parse_field(field: str, text: str | None) -> tuple[bool, Any]:
    """``(ok, value)`` for a value typed for ``field``."""
    clean = (text or "").strip()
    if field == "title":
        ok = 0 < len(clean) <= plans.MAX_TITLE_LENGTH
        return ok, clean
    if field == "duration_days":
        value = parse_int(clean, minimum=1, maximum=MAX_PERIOD_DAYS)
    elif field == "availability_rule":
        return clean in {rule.value for rule in plans.AvailabilityRule}, clean
    elif field == "allowed_user_ids":
        values = [parse_int(item, minimum=1) for item in re.split(r"[\s,]+", clean) if item]
        valid = len(values) <= 10_000 and all(value is not None for value in values) and len(set(values)) == len(values)
        return valid, values
    elif field == "price_kop":
        value = 0 if parse_int(clean) == 0 else parse_amount_kop(clean)
    elif field == "price_stars":
        stars = parse_int(clean, minimum=0, maximum=MAX_STARS)
        return stars is not None, (stars or None)
    else:
        value = parse_int(clean, minimum=0, maximum=MAX_LIMIT)
    return value is not None, value


@router.callback_query(PlanCb.filter(F.a == "edit"), HasPermission(Permission.PLANS))
async def ask_field(callback: CallbackQuery, callback_data: PlanCb, _: Translator, state: FSMContext) -> None:
    if callback_data.f not in TEXT_FIELDS:
        await answer_callback(callback, _("error.expired_button"))
        return
    await state.set_state(PlanInput.edit)
    await state.set_data({"plan_id": callback_data.id, "field": callback_data.f})
    back = kb([[Button.cb(_("nav.back"), PlanCb(a="card", id=callback_data.id))]])
    await render(callback, Screen(_(f"admin_plans.ask.{callback_data.f}"), back))


@router.message(PlanInput.edit, HasPermission(Permission.PLANS))
async def field_input(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    plan_id, field = int(data.get("plan_id", 0)), str(data.get("field", ""))
    ok, value = parse_field(field, message.text)
    if not ok:
        back = kb([[Button.cb(_("nav.back"), PlanCb(a="card", id=plan_id))]])
        await render(message, Screen(_("admin_plans.invalid") + "\n\n" + _(f"admin_plans.ask.{field}"), back))
        return
    await state.clear()
    note = await _update(ctx, _, plan_id, message.from_user.id if message.from_user else 0, **{field: value})
    await render(message, await card_screen(ctx, _, plan_id, note))


async def _update(ctx: AppContext, _: Translator, plan_id: int, actor_id: int, **fields: Any) -> str:
    async with ctx.session_factory.begin() as session:
        plan = await plans.get(session, plan_id)
        if plan is None:
            return _("admin.not_found")
        known = {k: v for k, v in fields.items() if k not in ("is_hidden", "includes_tg_proxy")}
        try:
            if known:
                await plans.update(session, plan, **known)
        except ValueError:
            return _("admin_plans.invalid")
        for key in ("is_hidden", "includes_tg_proxy"):
            if key in fields:
                setattr(plan, key, bool(fields[key]))
        await session.flush()
        await audit.log(session, actor_id, "plan.update", "plan", plan_id, **fields)
    return _("admin.saved")


@router.callback_query(PlanCb.filter(F.a == "toggle"), HasPermission(Permission.PLANS))
async def toggle_field(callback: CallbackQuery, callback_data: PlanCb, ctx: AppContext, _: Translator) -> None:
    if callback_data.f not in TOGGLES:
        await answer_callback(callback, _("error.expired_button"))
        return
    async with read_session(ctx.session_factory) as session, session.begin():
        plan = await plans.get(session, callback_data.id)
        current = bool(getattr(plan, callback_data.f)) if plan is not None else None
    if current is None:
        await render(callback, await list_screen(ctx, _, _("admin.not_found")))
        return
    note = await _update(ctx, _, callback_data.id, callback.from_user.id, **{callback_data.f: not current})
    await render(callback, await card_screen(ctx, _, callback_data.id), answer=note)


@router.callback_query(PlanCb.filter(F.a.in_({"up", "down"})), HasPermission(Permission.PLANS))
async def move(callback: CallbackQuery, callback_data: PlanCb, ctx: AppContext, _: Translator) -> None:
    async with ctx.session_factory.begin() as session:
        ordered = await plans.list_all(session)
        index = next((i for i, p in enumerate(ordered) if p.id == callback_data.id), None)
        target = None if index is None else index + (-1 if callback_data.a == "up" else 1)
        if index is not None and target is not None and 0 <= target < len(ordered):
            ordered[index], ordered[target] = ordered[target], ordered[index]
            for position, plan in enumerate(ordered):
                if plan.sort_order != position:
                    plan.sort_order = position
            await session.flush()
            await audit.log(
                session, callback.from_user.id, "plan.reorder", "plan", callback_data.id, direction=callback_data.a
            )
    await render(callback, await card_screen(ctx, _, callback_data.id))


# --- creation ---------------------------------------------------------------------------------------------------------


def _create_prompt(_: Translator, step: int) -> Screen:
    field = CREATE_STEPS[step]
    text = _("admin_plans.create.step", step=step + 1, total=len(CREATE_STEPS)) + "\n" + _(f"admin_plans.ask.{field}")
    return Screen(text, kb([[nav(_("admin.cancel"), PLANS_NAV)]]))


@router.callback_query(PlanCb.filter(F.a == "new"), HasPermission(Permission.PLANS))
async def start_create(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    await state.set_state(PlanInput.create)
    await state.set_data({"step": 0, "values": {}})
    await render(callback, _create_prompt(_, 0))


@router.message(PlanInput.create, HasPermission(Permission.PLANS))
async def create_input(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    step = int(data.get("step", 0))
    values: dict[str, Any] = dict(data.get("values", {}))
    if not 0 <= step < len(CREATE_STEPS):
        await state.clear()
        return
    field = CREATE_STEPS[step]
    ok, value = parse_field(field, message.text)
    if not ok:
        prompt = _create_prompt(_, step)
        await render(message, Screen(_("admin_plans.invalid") + "\n\n" + prompt.text, prompt.kb))
        return
    values[field] = value
    if step + 1 < len(CREATE_STEPS):
        await state.update_data(step=step + 1, values=values)
        await render(message, _create_prompt(_, step + 1))
        return
    await state.clear()
    actor = message.from_user.id if message.from_user else 0
    try:
        async with ctx.session_factory.begin() as session:
            plan = await plans.create(session, **values)
            await audit.log(session, actor, "plan.create", "plan", plan.id, **values)
            plan_id = plan.id
    except ValueError:
        await render(message, await list_screen(ctx, _, _("admin_plans.invalid")))
        return
    await render(message, await card_screen(ctx, _, plan_id, _("admin_plans.created")))
