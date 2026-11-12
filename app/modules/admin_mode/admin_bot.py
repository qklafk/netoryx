"""Product mode wizard: personal VPN <-> shop, with a readiness checklist, a preview of every change, notices to the
affected users and a one-click rollback."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from aiogram import F
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton
from sqlalchemy import select

from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.permissions import HasPermission, Permission
from app.core.ui import Screen, answer_callback, render
from app.db.models import User
from app.db.session import read_session
from app.keyboards import Button, back, kb
from app.modules.admin.common import admin_router
from app.modules.subscription.stars_renewal import cancel_stars_renewal
from app.services import mode_switch
from app.services.mode_switch import MemberPolicy, Mode, ModeSwitchError, NotReady, SwitchPlan
from app.services.notify import safe_send
from app.utils.time import utcnow

log = logging.getLogger(__name__)

router = admin_router("mode_admin")
router.callback_query.filter(HasPermission(Permission.SETTINGS_SECRETS))

GRACE_PRESETS = (7, 14, 30)
NOTICE_INTERVAL_SEC = 0.05
_background: set[asyncio.Task[Any]] = set()


class ModeCb(CallbackData, prefix="amode"):
    """``a`` = action; ``v`` = member policy for the shop switch: ``keep`` or ``conv<days>``."""

    a: str
    v: str = ""


def _cb(action: str, value: str = "") -> ModeCb:
    return ModeCb(a=action, v=value)


def _policy(value: str) -> tuple[MemberPolicy, int | None]:
    if value.startswith("conv") and value[4:].isdigit():
        return MemberPolicy.CONVERT, int(value[4:])
    return MemberPolicy.KEEP, None


async def _preview_shop(ctx: AppContext, value: str) -> SwitchPlan:
    policy, days = _policy(value)
    grace = utcnow() + timedelta(days=days) if days else None
    async with read_session(ctx.session_factory) as session:
        return await mode_switch.preview_to_shop(session, ctx.settings, default_policy=policy, grace_until=grace)


async def _preview_personal(ctx: AppContext) -> SwitchPlan:
    async with read_session(ctx.session_factory) as session:
        return await mode_switch.preview_to_personal(session, ctx.settings)


def _checklist(_: Translator, plan: SwitchPlan) -> str:
    lines = []
    for item in plan.readiness.items:
        icon = "✅" if item.ok else ("❌" if item.required else "▫️")
        lines.append(f"{icon} {_(f'admin_mode.check.{item.key}')}")
    return "\n".join(lines)


# --- screens ----------------------------------------------------------------------------------------------------------
async def home_screen(ctx: AppContext, _: Translator, note: str | None = None) -> Screen:
    current = ctx.settings.app_mode
    last = await mode_switch.last_switch(ctx.session_factory)
    text = _("admin_mode.home", mode=_(f"admin_mode.mode.{current}"), about=_(f"admin_mode.about.{current}"))
    if note:
        text = note + "\n\n" + text
    target = Mode.SHOP if current == Mode.PERSONAL else Mode.PERSONAL
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_(f"admin_mode.btn.to_{target.value}"), _cb(f"to_{target.value}"), style="primary")],
    ]
    if last:
        when = _.datetime(datetime.fromisoformat(str(last["at"]))) if last.get("at") else "—"
        rows.append([Button.cb(_("admin_mode.btn.rollback", when=when), _cb("rb"))])
    rows.append([back("admin", _)])
    return Screen(text, kb(rows))


@router.callback_query(ModeCb.filter(F.a == "home"))
async def open_home(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await home_screen(ctx, _))


# --- shop -> personal -------------------------------------------------------------------------------------------------
@router.callback_query(ModeCb.filter(F.a == "to_personal"))
async def preview_personal(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    if ctx.settings.app_mode == Mode.PERSONAL:
        await render(callback, await home_screen(ctx, _))
        return
    plan = await _preview_personal(ctx)
    providers = [key for key in plan.settings_changes if key != "app_mode"]
    text = _(
        "admin_mode.preview_personal",
        providers=", ".join(_.esc(p.removesuffix("_enabled")) for p in providers) or _("admin_mode.none"),
        renew=len(plan.auto_renew_off),
    )
    if plan.readiness.ready:
        apply_button = Button.cb(_("admin_mode.btn.apply"), _cb("go_personal"), style="danger")
    else:
        pending = next((i.detail for i in plan.readiness.items if i.key == "pending_payments"), "0")
        text += "\n\n" + _("admin_mode.pending_payments", count=pending)
        apply_button = Button.cb(_("admin_mode.btn.force"), _cb("go_personal", "force"), style="danger")
    rows: list[Sequence[InlineKeyboardButton | None]] = [[apply_button], [Button.cb(_("nav.back"), _cb("home"))]]
    await render(callback, Screen(text, kb(rows)))


@router.callback_query(ModeCb.filter(F.a == "go_personal"))
async def apply_personal(callback: CallbackQuery, callback_data: ModeCb, ctx: AppContext, _: Translator) -> None:
    if ctx.settings.app_mode == Mode.PERSONAL:
        await render(callback, await home_screen(ctx, _))
        return
    await answer_callback(callback, _("common.loading"))
    plan = await _preview_personal(ctx)
    try:
        result = await mode_switch.apply(
            ctx.session_factory,
            ctx.settings,
            plan,
            callback.from_user.id,
            set_setting=_setter(ctx, callback.from_user.id),
            cancel_stars=_stars_canceler(ctx),
            force=callback_data.v == "force",
        )
    except NotReady:
        await preview_personal(callback, ctx, _)
        return
    _notify_later(ctx, result.notify_user_ids, "admin_mode.notice.renew_off", {})
    note = _("admin_mode.done_personal", renew=result.auto_renew_disabled)
    if result.settings_failed:
        note += "\n" + _("admin_mode.settings_failed", keys=_.esc(", ".join(result.settings_failed)))
    await render(callback, await home_screen(ctx, _, note))


# --- personal -> shop -------------------------------------------------------------------------------------------------
@router.callback_query(ModeCb.filter(F.a == "to_shop"))
async def readiness(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    if ctx.settings.app_mode == Mode.SHOP:
        await render(callback, await home_screen(ctx, _))
        return
    plan = await _preview_shop(ctx, "keep")
    text = _("admin_mode.readiness", checklist=_checklist(_, plan), members=len(plan.members))
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_mode.btn.keep_free", count=len(plan.members)), _cb("prev_shop", "keep"))],
        [Button.cb(_("admin_mode.btn.convert"), _cb("grace"))] if plan.members else [],
        [Button.cb(_("nav.back"), _cb("home"))],
    ]
    await render(callback, Screen(text, kb(rows)))


@router.callback_query(ModeCb.filter(F.a == "grace"))
async def choose_grace(callback: CallbackQuery, _: Translator) -> None:
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_.plural(days, "plural.days"), _cb("prev_shop", f"conv{days}")) for days in GRACE_PRESETS],
        [Button.cb(_("nav.back"), _cb("to_shop"))],
    ]
    await render(callback, Screen(_("admin_mode.grace"), kb(rows)))


@router.callback_query(ModeCb.filter(F.a == "prev_shop"))
async def preview_shop(callback: CallbackQuery, callback_data: ModeCb, ctx: AppContext, _: Translator) -> None:
    try:
        plan = await _preview_shop(ctx, callback_data.v)
    except ModeSwitchError:
        await answer_callback(callback, _("error.expired_button"), alert=True)
        return
    converted = [m for m in plan.members if m.policy == MemberPolicy.CONVERT]
    policy, days = _policy(callback_data.v)
    if policy == MemberPolicy.CONVERT and days:
        members = _("admin_mode.members_convert", count=len(converted), date=_.date(utcnow() + timedelta(days=days)))
    else:
        members = _("admin_mode.members_keep", count=len(plan.members))
    text = _("admin_mode.preview_shop", members=members, checklist=_checklist(_, plan))
    rows: list[Sequence[InlineKeyboardButton | None]] = []
    if plan.readiness.ready:
        rows.append([Button.cb(_("admin_mode.btn.apply"), _cb("go_shop", callback_data.v), style="success")])
    else:
        text += "\n\n" + _("admin_mode.not_ready")
        rows.append([Button.cb(_("admin_mode.btn.force"), _cb("force_shop", callback_data.v), style="danger")])
    rows.append([Button.cb(_("nav.back"), _cb("to_shop"))])
    await render(callback, Screen(text, kb(rows)))


@router.callback_query(ModeCb.filter(F.a.in_({"go_shop", "force_shop"})))
async def apply_shop(callback: CallbackQuery, callback_data: ModeCb, ctx: AppContext, _: Translator) -> None:
    if ctx.settings.app_mode == Mode.SHOP:
        await render(callback, await home_screen(ctx, _))
        return
    await answer_callback(callback, _("common.loading"))
    try:
        plan = await _preview_shop(ctx, callback_data.v)
        result = await mode_switch.apply(
            ctx.session_factory,
            ctx.settings,
            plan,
            callback.from_user.id,
            set_setting=_setter(ctx, callback.from_user.id),
            force=callback_data.a == "force_shop",
        )
    except NotReady:
        await render(callback, await home_screen(ctx, _, _("admin_mode.not_ready")))
        return
    except ModeSwitchError:
        await render(callback, await home_screen(ctx, _, _("error.expired_button")))
        return
    dates = {m.user_id: m.expires_after for m in plan.members if m.policy == MemberPolicy.CONVERT}
    _notify_later(ctx, result.notify_user_ids, "admin_mode.notice.converted", dates)
    note = _("admin_mode.done_shop", converted=result.converted)
    if result.settings_failed:
        note += "\n" + _("admin_mode.settings_failed", keys=_.esc(", ".join(result.settings_failed)))
    await render(callback, await home_screen(ctx, _, note))


# --- rollback ---------------------------------------------------------------------------------------------------------
@router.callback_query(ModeCb.filter(F.a == "rb"))
async def ask_rollback(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    last = await mode_switch.last_switch(ctx.session_factory)
    if not last:
        await render(callback, await home_screen(ctx, _))
        return
    target = str(last.get("target") or "")
    text = _("admin_mode.rollback", mode=_(f"admin_mode.mode.{target}") if target in ("shop", "personal") else "?")
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_mode.btn.rollback_yes"), _cb("rby"), style="danger")],
        [Button.cb(_("nav.back"), _cb("home"))],
    ]
    await render(callback, Screen(text, kb(rows)))


@router.callback_query(ModeCb.filter(F.a == "rby"))
async def rollback(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    done = await mode_switch.rollback(
        ctx.session_factory, callback.from_user.id, set_setting=_setter(ctx, callback.from_user.id)
    )
    note = _("admin_mode.rolled_back" if done else "admin_mode.nothing_to_rollback")
    await render(callback, await home_screen(ctx, _, note))


# --- helpers ----------------------------------------------------------------------------------------------------------
def _stars_canceler(ctx: AppContext) -> mode_switch.StarsCanceler:
    async def cancel(user_id: int) -> bool:
        return await cancel_stars_renewal(ctx.bot, ctx.session_factory, user_id)

    return cancel


def _setter(ctx: AppContext, actor_id: int) -> mode_switch.SettingSetter:
    async def set_setting(key: str, value: Any) -> None:
        await ctx.settings_provider.set(key, value, actor_id)

    return set_setting


def _notify_later(ctx: AppContext, user_ids: list[int], key: str, dates: dict[int, datetime]) -> None:
    if not user_ids:
        return
    task = asyncio.create_task(notify_users(ctx, user_ids, key, dates))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def notify_users(ctx: AppContext, user_ids: list[int], key: str, dates: dict[int, datetime]) -> int:
    """Tell users about the change in their language; returns how many got the notice."""
    sent = 0
    settings = ctx.settings
    for start in range(0, len(user_ids), 500):
        chunk = user_ids[start : start + 500]
        async with read_session(ctx.session_factory) as session:
            rows = await session.execute(
                select(User.tg_id, User.language).where(
                    User.tg_id.in_(chunk), User.bot_blocked.is_(False), User.is_banned.is_(False)
                )
            )
            languages = {tg_id: language for tg_id, language in rows}
        for user_id in chunk:
            if user_id not in languages:
                continue
            _ = ctx.i18n.translator(languages[user_id], settings.zone)
            when = dates.get(user_id)
            text = _(key, brand=_.esc(settings.brand_name), date=_.date(when) if when else "")
            if await safe_send(ctx.bot, user_id, text, session_factory=ctx.session_factory):
                sent += 1
            await asyncio.sleep(NOTICE_INTERVAL_SEC)
    log.info("mode switch notices sent: %d of %d", sent, len(user_ids))
    return sent
