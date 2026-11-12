"""Promo codes in the bot admin: list, create (step by step), details with statistics, deactivate; gift codes."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from aiogram import F
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.core.context import AppContext
from app.core.filters import HasPermission, Permission
from app.core.i18n import Translator
from app.core.ui import Screen, answer_callback, render
from app.db.models import Plan, PromoActivation, PromoCode, PromoKind
from app.db.session import read_session
from app.keyboards import Btn, Button, Nav, kb, nav
from app.modules.admin.common import (
    PAGE_SIZE,
    AdminInputCb,
    admin_input_group,
    admin_router,
    input_button,
    parse_amount_kop,
    parse_int,
    to_admin,
    when,
    yes_no,
)
from app.services import audit, promo
from app.utils.time import MAX_PERIOD_DAYS, utcnow

log = logging.getLogger(__name__)

router = admin_router("admin_promo")

PROMO_NAV = "adm_promo"
MAX_USES = 1_000_000
MAX_VALID_DAYS = 3650
STEPS = ("code", "bonus_kop", "bonus_days", "max_uses", "valid_days", "only_new")


class PromoCb(CallbackData, prefix="admc"):
    a: str  # list | card | off | gifts | new
    id: int = 0
    page: int = 0


@admin_input_group
class PromoInput(StatesGroup):
    create = State()  # data: step, values


def _bonus(_: Translator, code: PromoCode) -> str:
    parts = []
    if code.bonus_kop:
        parts.append(f"+{_.money(code.bonus_kop)}")
    if code.bonus_days:
        parts.append(f"+{_.plural(code.bonus_days, 'plural.days')}")
    return " ".join(parts) or "—"


def _uses(_: Translator, code: PromoCode) -> str:
    limit = "∞" if code.max_uses == 0 else str(code.max_uses)
    return f"{code.uses}/{limit}"


# --- list -------------------------------------------------------------------------------------------------------------


async def list_screen(ctx: AppContext, _: Translator, page: int = 0, note: str | None = None) -> Screen:
    page = max(0, page)
    stmt = (
        select(PromoCode)
        .where(PromoCode.kind == PromoKind.PROMO)
        .order_by(PromoCode.is_active.desc(), PromoCode.id.desc())
        .offset(page * PAGE_SIZE)
        .limit(PAGE_SIZE + 1)
    )
    async with read_session(ctx.session_factory) as session, session.begin():
        items = list((await session.scalars(stmt)).all())
    rows: list[list[Btn]] = []
    for code in items[:PAGE_SIZE]:
        mark = "" if code.is_active else "✖️ "
        label = f"{mark}{code.code} · {_bonus(_, code)} · {_uses(_, code)}"[:64]
        rows.append([Button.cb(label, PromoCb(a="card", id=code.id))])
    pager = [
        Button.cb("◀️", PromoCb(a="list", page=page - 1)) if page > 0 else None,
        Button.cb("▶️", PromoCb(a="list", page=page + 1)) if len(items) > PAGE_SIZE else None,
    ]
    rows.append([b for b in pager if b is not None])
    rows.append(
        [
            Button.cb(_("admin_promo.btn.new"), PromoCb(a="new")),
            Button.cb(_("admin_promo.btn.gifts"), PromoCb(a="gifts")),
        ]
    )
    rows.append([to_admin(_)])
    text = _("admin_promo.list", page=page + 1) if items else _("admin_promo.list_empty")
    if note:
        text = f"{note}\n\n{text}"
    return Screen(text, kb(rows))


@router.callback_query(Nav.filter(F.to == PROMO_NAV), HasPermission(Permission.PROMO))
async def show_list(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await list_screen(ctx, _))


@router.callback_query(PromoCb.filter(F.a == "list"), HasPermission(Permission.PROMO))
async def show_page(callback: CallbackQuery, callback_data: PromoCb, ctx: AppContext, _: Translator) -> None:
    await render(callback, await list_screen(ctx, _, callback_data.page))


# --- card -------------------------------------------------------------------------------------------------------------


async def card_screen(ctx: AppContext, _: Translator, promo_id: int, note: str | None = None) -> Screen:
    async with read_session(ctx.session_factory) as session, session.begin():
        code = await session.get(PromoCode, promo_id)
        activations = int(
            await session.scalar(select(func.count(PromoActivation.id)).where(PromoActivation.promo_id == promo_id))
            or 0
        )
        plan = await session.get(Plan, code.plan_id) if code is not None and code.plan_id else None
    if code is None:
        return await list_screen(ctx, _, note=_("admin.not_found"))
    now = utcnow()
    if not code.is_active:
        status = _("admin_promo.status.off")
    elif code.expires_at is not None and code.expires_at <= now:
        status = _("admin_promo.status.expired")
    elif code.max_uses and code.uses >= code.max_uses:
        status = _("admin_promo.status.exhausted")
    else:
        status = _("admin_promo.status.on")
    kind = "admin_promo.card.gift_title" if code.kind == PromoKind.GIFT else "admin_promo.card.title"
    lines = [
        _(kind, code=_.esc(code.code)),
        _("admin_promo.card.status", status=status),
        _("admin_promo.card.bonus", bonus=_bonus(_, code)),
        _("admin_promo.card.uses", uses=_uses(_, code), activations=_.number(activations)),
        _(
            "admin_promo.card.rules",
            only_new=yes_no(_, code.only_new_users),
            expires=when(_, code.expires_at) if code.expires_at else _("fmt.forever"),
        ),
        _(
            "admin_promo.card.created",
            created=when(_, code.created_at),
            by=f"<code>{code.created_by}</code>" if code.created_by else "—",
        ),
    ]
    if plan is not None:
        lines.append(_("admin_promo.card.plan", plan=_.esc(plan.title)))
    link = None
    if ctx.bot_username and code.kind == PromoKind.PROMO:
        link = f"https://t.me/{ctx.bot_username}?start=promo_{code.code}"
        lines.append(_("admin_promo.card.link", link=_.esc(link)))
    if note:
        lines.insert(0, f"{note}\n")
    rows: list[list[Btn | None]] = [
        [Button.copy(_("admin_promo.btn.copy_link"), link) if link and len(link) <= 256 else None],
        [
            Button.cb(_("admin_promo.btn.deactivate"), PromoCb(a="off", id=code.id), style="danger")
            if code.is_active
            else None
        ],
        [
            Button.cb(_("admin_promo.btn.back"), PromoCb(a="gifts" if code.kind == PromoKind.GIFT else "list")),
            to_admin(_),
        ],
    ]
    return Screen("\n".join(lines), kb(rows))


@router.callback_query(PromoCb.filter(F.a == "card"), HasPermission(Permission.PROMO))
async def show_card(callback: CallbackQuery, callback_data: PromoCb, ctx: AppContext, _: Translator) -> None:
    await render(callback, await card_screen(ctx, _, callback_data.id))


@router.callback_query(PromoCb.filter(F.a == "off"), HasPermission(Permission.PROMO))
async def deactivate(callback: CallbackQuery, callback_data: PromoCb, ctx: AppContext, _: Translator) -> None:
    async with ctx.session_factory.begin() as session:
        code = await session.get(PromoCode, callback_data.id)
        if code is not None and code.is_active:
            await promo.deactivate(session, code)
            await audit.log(session, callback.from_user.id, "promo.deactivate", "promo", code.id, code=code.code)
    await render(callback, await card_screen(ctx, _, callback_data.id), answer=_("admin.saved"))


@router.callback_query(PromoCb.filter(F.a == "gifts"), HasPermission(Permission.PROMO))
async def show_gifts(callback: CallbackQuery, callback_data: PromoCb, ctx: AppContext, _: Translator) -> None:
    page = max(0, callback_data.page)
    stmt = (
        select(PromoCode)
        .where(PromoCode.kind == PromoKind.GIFT)
        .order_by(PromoCode.id.desc())
        .offset(page * PAGE_SIZE)
        .limit(PAGE_SIZE + 1)
    )
    async with read_session(ctx.session_factory) as session, session.begin():
        items = list((await session.scalars(stmt)).all())
    rows: list[list[Btn]] = []
    for code in items[:PAGE_SIZE]:
        used = "✅" if code.max_uses and code.uses >= code.max_uses else "🎁"
        rows.append([Button.cb(f"{used} {code.code} · {_bonus(_, code)}"[:64], PromoCb(a="card", id=code.id))])
    pager = [
        Button.cb("◀️", PromoCb(a="gifts", page=page - 1)) if page > 0 else None,
        Button.cb("▶️", PromoCb(a="gifts", page=page + 1)) if len(items) > PAGE_SIZE else None,
    ]
    rows.append([b for b in pager if b is not None])
    rows.append([Button.cb(_("admin_promo.btn.back"), PromoCb(a="list")), to_admin(_)])
    text = _("admin_promo.gifts", page=page + 1) if items else _("admin_promo.gifts_empty")
    await render(callback, Screen(text, kb(rows)))


# --- creation ---------------------------------------------------------------------------------------------------------


def _prompt(_: Translator, step: int, error: bool = False) -> Screen:
    name = STEPS[step]
    text = _("admin_promo.create.step", step=step + 1, total=len(STEPS)) + "\n" + _(f"admin_promo.ask.{name}")
    if error:
        text = _("admin_promo.invalid") + "\n\n" + text
    rows: list[list[Btn]] = []
    if name == "code":
        rows.append([input_button(_("admin_promo.btn.random"), "promo", "random")])
    elif name == "only_new":
        rows.append([input_button(_("common.yes"), "promo", "yes"), input_button(_("common.no"), "promo", "no")])
    rows.append([nav(_("admin.cancel"), PROMO_NAV)])
    return Screen(text, kb(rows))


@router.callback_query(PromoCb.filter(F.a == "new"), HasPermission(Permission.PROMO))
async def start_create(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    await state.set_state(PromoInput.create)
    await state.set_data({"step": 0, "values": {}})
    await render(callback, _prompt(_, 0))


def parse_step(name: str, text: str | None, values: dict[str, Any]) -> tuple[bool, Any]:
    clean = (text or "").strip()
    if name == "code":
        code = promo.normalize_code(clean)
        ok = 3 <= len(code) <= 64 and all(ch.isalnum() or ch in "_-" for ch in code)
        return ok, code
    if name == "bonus_kop":
        return (True, 0) if parse_int(clean) == 0 else (parse_amount_kop(clean) is not None, parse_amount_kop(clean))
    if name == "bonus_days":
        days = parse_int(clean, minimum=0, maximum=MAX_PERIOD_DAYS)
        ok = days is not None and (days > 0 or int(values.get("bonus_kop") or 0) > 0)
        return ok, days
    if name == "max_uses":
        uses = parse_int(clean, minimum=0, maximum=MAX_USES)
        return uses is not None, uses
    if name == "valid_days":
        valid = parse_int(clean, minimum=0, maximum=MAX_VALID_DAYS)
        return valid is not None, valid
    return False, None


async def _advance(
    event: Message | CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext, value: Any
) -> None:
    data = await state.get_data()
    step = int(data.get("step", 0))
    values: dict[str, Any] = dict(data.get("values", {}))
    values[STEPS[step]] = value
    if step + 1 < len(STEPS):
        await state.update_data(step=step + 1, values=values)
        await render(event, _prompt(_, step + 1))
        return
    await state.clear()
    actor = event.from_user.id if event.from_user else 0
    valid_days = int(values.get("valid_days") or 0)
    try:
        async with ctx.session_factory.begin() as session:
            code = await promo.create_promo(
                session,
                code=values.get("code"),
                bonus_kop=int(values.get("bonus_kop") or 0),
                bonus_days=int(values.get("bonus_days") or 0),
                max_uses=int(values.get("max_uses") or 0),
                only_new_users=bool(values.get("only_new")),
                expires_at=utcnow() + timedelta(days=valid_days) if valid_days else None,
                created_by=actor,
            )
            details = {**values, "code": code.code}
            await audit.log(session, actor, "promo.create", "promo", code.id, "bot", None, **details)
            promo_id = code.id
    except promo.PromoCodeExists:
        await render(event, await list_screen(ctx, _, note=_("admin_promo.exists")))
        return
    except ValueError:
        await render(event, await list_screen(ctx, _, note=_("admin_promo.invalid")))
        return
    await render(event, await card_screen(ctx, _, promo_id, _("admin_promo.created")))


@router.message(PromoInput.create, HasPermission(Permission.PROMO))
async def create_input(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    step = int(data.get("step", 0))
    if not 0 <= step < len(STEPS):
        await state.clear()
        return
    if STEPS[step] == "only_new":
        await render(message, _prompt(_, step, error=True))
        return
    ok, value = parse_step(STEPS[step], message.text, dict(data.get("values", {})))
    if not ok:
        await render(message, _prompt(_, step, error=True))
        return
    await _advance(message, ctx, _, state, value)


@router.callback_query(PromoInput.create, AdminInputCb.filter(F.action == "promo"), HasPermission(Permission.PROMO))
async def create_button(
    callback: CallbackQuery, callback_data: AdminInputCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    step = int(data.get("step", 0))
    name = STEPS[step] if 0 <= step < len(STEPS) else ""
    if name == "code" and callback_data.value == "random":
        await _advance(callback, ctx, _, state, None)
    elif name == "only_new" and callback_data.value in ("yes", "no"):
        await _advance(callback, ctx, _, state, callback_data.value == "yes")
    else:
        await answer_callback(callback, _("error.expired_button"))
