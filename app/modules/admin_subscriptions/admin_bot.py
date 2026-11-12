"""Subscriptions section of the bot admin: overview, expiring soon, bulk compensation and bulk re-sync."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine
from typing import Any

from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.core.context import AppContext
from app.core.filters import HasPermission, Permission
from app.core.i18n import Translator
from app.core.ui import Screen, answer_callback, render
from app.db.models import User
from app.db.session import read_session
from app.keyboards import Btn, Button, Nav, kb, nav
from app.keyboards.common import confirm_kb
from app.modules.admin.common import (
    PAGE_SIZE,
    admin_input_group,
    admin_router,
    first_time,
    new_nonce,
    parse_int,
    to_admin,
    user_short,
)
from app.modules.admin_subscriptions import ops
from app.utils.time import MAX_PERIOD_DAYS

log = logging.getLogger(__name__)

router = admin_router("admin_subscriptions")

SUBS_NAV = "adm_subs"
EXPIRING_DAYS = 7
PROGRESS_INTERVAL_SEC = 2.0
LOCK_NAME = "bulk-compensation"
LOCK_TTL_SEC = 300.0  # extended after every batch, so a crashed holder blocks a resume for minutes, not hours
SHUTDOWN_WAIT_SEC = 5.0

# Bulk jobs run after the handler returns; references are kept so they are not garbage-collected.
TASKS: set[asyncio.Task[None]] = set()


class SubsCb(CallbackData, prefix="adms"):
    a: str
    arg: str = ""


@admin_input_group
class SubsInput(StatesGroup):
    days = State()


def _back(_: Translator) -> list[Btn]:
    return [nav(_("nav.back"), SUBS_NAV), to_admin(_)]


def _pending_note(_: Translator, run: ops.CompensationRun) -> str:
    return _(
        "admin_subscriptions.compensation_pending",
        days=_.plural(run.days, "plural.days"),
        done=_.number(run.done),
        total=_.number(max(run.total, run.done)),
    )


async def overview_screen(ctx: AppContext, _: Translator, note: str | None = None) -> Screen:
    data = await ops.overview(ctx.session_factory)
    text = _(
        "admin_subscriptions.overview",
        active=_.number(data.active),
        trial=_.number(data.trial),
        complimentary=_.number(data.complimentary),
        d3=_.number(data.expiring_3d),
        d7=_.number(data.expiring_7d),
        expired=_.number(data.expired_30d),
        disabled=_.number(data.disabled),
    )
    pending = await ops.pending_compensation(ctx.session_factory)
    if pending is not None:
        text = f"{_pending_note(_, pending)}\n\n{text}"
        compensation = Button.cb(_("admin_subscriptions.btn.resume"), SubsCb(a="comp_resume", arg=new_nonce()))
    else:
        compensation = Button.cb(_("admin_subscriptions.btn.compensate"), SubsCb(a="comp"))
    if note:
        text = f"{note}\n\n{text}"
    rows = [
        [Button.cb(_("admin_subscriptions.btn.expiring"), SubsCb(a="exp", arg="0"))],
        [compensation],
        [Button.cb(_("admin_subscriptions.btn.resync"), SubsCb(a="sync"))],
        [nav(_("nav.refresh"), SUBS_NAV), to_admin(_)],
    ]
    return Screen(text, kb(rows))


@router.callback_query(Nav.filter(F.to == SUBS_NAV), HasPermission(Permission.SUBS_BULK))
async def show_overview(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await overview_screen(ctx, _))


@router.callback_query(SubsCb.filter(F.a == "exp"), HasPermission(Permission.SUBS_BULK))
async def show_expiring(callback: CallbackQuery, callback_data: SubsCb, ctx: AppContext, _: Translator) -> None:
    page = max(0, parse_int(callback_data.arg) or 0)
    items = await ops.expiring(ctx.session_factory, EXPIRING_DAYS, offset=page * PAGE_SIZE, limit=PAGE_SIZE + 1)
    rows: list[list[Btn]] = [
        [Button.cb(f"{_.date(sub.expires_at)} · {user_short(user)}"[:64], Nav(to="adm_user", arg=str(user.tg_id)))]
        for sub, user in items[:PAGE_SIZE]
    ]
    pager = [
        Button.cb("◀️", SubsCb(a="exp", arg=str(page - 1))) if page > 0 else None,
        Button.cb("▶️", SubsCb(a="exp", arg=str(page + 1))) if len(items) > PAGE_SIZE else None,
    ]
    rows.append([b for b in pager if b is not None])
    rows.append(_back(_))
    text = _("admin_subscriptions.expiring", days=_.plural(EXPIRING_DAYS, "plural.days"), page=page + 1)
    if not items:
        text += "\n\n" + _("admin_subscriptions.expiring_empty")
    await render(callback, Screen(text, kb(rows)))


# --- compensation ----------------------------------------------------------------------------------------------------


@router.callback_query(SubsCb.filter(F.a == "comp"), HasPermission(Permission.SUBS_BULK))
async def ask_days(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    if await ops.pending_compensation(ctx.session_factory) is not None:
        await render(callback, await overview_screen(ctx, _))  # finish the previous one first
        return
    await state.set_state(SubsInput.days)
    await render(callback, Screen(_("admin_subscriptions.ask_days"), kb([_back(_)])))


@router.message(SubsInput.days, HasPermission(Permission.SUBS_BULK))
async def days_input(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    days = parse_int(message.text, minimum=1, maximum=MAX_PERIOD_DAYS)
    if days is None:
        await render(message, Screen(_("admin.invalid_number"), kb([_back(_)])))
        return
    await state.clear()
    count = len(await ops.active_ids(ctx.session_factory))
    text = _(
        "admin_subscriptions.confirm_compensation",
        days=_.plural(days, "plural.days"),
        count=_.number(count),
    )
    yes = SubsCb(a="comp_ok", arg=f"{days}.{new_nonce()}")
    await render(message, Screen(text, confirm_kb(_, yes, Nav(to=SUBS_NAV))))


@router.callback_query(SubsCb.filter(F.a == "comp_ok"), HasPermission(Permission.SUBS_BULK))
async def start_compensation(callback: CallbackQuery, callback_data: SubsCb, ctx: AppContext, _: Translator) -> None:
    value, _sep, nonce = callback_data.arg.partition(".")
    days = parse_int(value, minimum=1, maximum=MAX_PERIOD_DAYS)
    if days is None or not nonce or callback.message is None:
        await answer_callback(callback, _("error.expired_button"))
        return
    if not first_time(f"compensate:{nonce}"):
        await answer_callback(callback, _("admin_subscriptions.already_running"))
        return
    sent = await render(callback, Screen(_("admin_subscriptions.progress", done=0, total="…")))
    chat_id = callback.message.chat.id
    message_id = sent.message_id if sent is not None else None
    _spawn(_compensation_job(ctx, _, days, callback.from_user.id, chat_id, message_id))


@router.callback_query(SubsCb.filter(F.a == "comp_resume"), HasPermission(Permission.SUBS_BULK))
async def resume_compensation(callback: CallbackQuery, callback_data: SubsCb, ctx: AppContext, _: Translator) -> None:
    if callback.message is None or not callback_data.arg or not first_time(f"compensate:{callback_data.arg}"):
        await answer_callback(callback, _("admin_subscriptions.already_running"))
        return
    sent = await render(callback, Screen(_("admin_subscriptions.progress", done="…", total="…")))
    message_id = sent.message_id if sent is not None else None
    _spawn(_compensation_job(ctx, _, None, callback.from_user.id, callback.message.chat.id, message_id))


async def _compensation_job(
    ctx: AppContext, _: Translator, days: int | None, actor_id: int, chat_id: int, message_id: int | None
) -> None:
    """Start a compensation of ``days`` (``None``: continue the unfinished one) and report to the admin's chat."""
    last = 0.0
    locks = getattr(ctx, "locks", None)

    async def progress(done: int, total: int) -> None:
        nonlocal last
        if locks is not None:
            await locks.extend(LOCK_NAME, LOCK_TTL_SEC)
        now = time.monotonic()
        if message_id is not None and now - last >= PROGRESS_INTERVAL_SEC:
            last = now
            await _edit(ctx.bot, chat_id, message_id, Screen(_("admin_subscriptions.progress", done=done, total=total)))

    async def run() -> int:
        if days is not None:
            await ops.start_compensation(ctx.session_factory, days, actor_id)
        return await ops.resume_compensation(ctx.session_factory, ctx.settings, progress=progress)

    async def busy() -> None:
        await _edit(
            ctx.bot, chat_id, message_id, await overview_screen(ctx, _, _("admin_subscriptions.already_running"))
        )

    try:
        if locks is not None:
            async with locks.job_lock(LOCK_NAME, LOCK_TTL_SEC) as acquired:
                if not acquired:
                    await busy()
                    return
                done = await run()
        else:
            done = await run()
    except ops.CompensationPending:
        await _edit(ctx.bot, chat_id, message_id, await overview_screen(ctx, _))  # shows the unfinished one
        return
    except Exception:
        log.exception("bulk compensation failed")
        await _edit(ctx.bot, chat_id, message_id, await overview_screen(ctx, _, _("admin_subscriptions.failed")))
        return
    note: str | None = None
    if days is not None:
        note = _("admin_subscriptions.compensated", days=_.plural(days, "plural.days"), count=_.number(done))
    elif done:
        note = _("admin_subscriptions.compensation_resumed", count=_.number(done))
    await _edit(ctx.bot, chat_id, message_id, await overview_screen(ctx, _, note))


async def resume_on_startup(ctx: AppContext) -> None:
    """A compensation interrupted by a restart is finished in the background; its admin gets the report."""
    try:
        run = await ops.pending_compensation(ctx.session_factory)
        if run is None:
            return
        async with read_session(ctx.session_factory) as session:
            lang = await session.scalar(select(User.language).where(User.tg_id == run.actor_id))
    except Exception:
        log.exception("cannot check for an unfinished compensation")
        return
    _ = ctx.i18n.translator(lang, ctx.settings.zone)
    log.info("resuming the unfinished compensation %s (%d of %d done)", run.run_id, run.done, run.total)
    _spawn(_compensation_job(ctx, _, None, run.actor_id, run.actor_id, None))


async def stop_jobs(_ctx: AppContext) -> None:
    """On shutdown: let bulk jobs finish for a moment, then cancel them (an interrupted batch is rolled back and
    the compensation resumes from the last committed one on the next start)."""
    pending = set(TASKS)
    if not pending:
        return
    _done, still = await asyncio.wait(pending, timeout=SHUTDOWN_WAIT_SEC)
    for task in still:
        task.cancel()
    if still:
        log.warning("interrupting %d bulk subscription jobs on shutdown", len(still))
        await asyncio.wait(still, timeout=SHUTDOWN_WAIT_SEC)


# --- re-sync ---------------------------------------------------------------------------------------------------------


@router.callback_query(SubsCb.filter(F.a == "sync"), HasPermission(Permission.SUBS_BULK))
async def ask_resync(callback: CallbackQuery, _: Translator) -> None:
    yes = SubsCb(a="sync_ok", arg=new_nonce())
    await render(callback, Screen(_("admin_subscriptions.confirm_resync"), confirm_kb(_, yes, Nav(to=SUBS_NAV))))


@router.callback_query(SubsCb.filter(F.a == "sync_ok"), HasPermission(Permission.SUBS_BULK))
async def start_resync(callback: CallbackQuery, callback_data: SubsCb, ctx: AppContext, _: Translator) -> None:
    if callback.message is None or not first_time(f"resync:{callback_data.arg}"):
        await answer_callback(callback, _("admin_subscriptions.already_running"))
        return
    sent = await render(callback, Screen(_("admin_subscriptions.resyncing")))
    message_id = sent.message_id if sent is not None else None
    _spawn(_resync_job(ctx, _, callback.from_user.id, callback.message.chat.id, message_id))


async def _resync_job(ctx: AppContext, _: Translator, actor_id: int, chat_id: int, message_id: int | None) -> None:
    try:
        marked, report = await ops.resync_all(ctx.session_factory, ctx.panels, ctx.settings, actor_id)
    except Exception:
        log.exception("bulk resync failed")
        note = _("admin_subscriptions.failed")
    else:
        note = _(
            "admin_subscriptions.resynced",
            marked=_.number(marked),
            synced=_.number(report.synced),
            failed=_.number(report.failed),
            down=_.number(len(report.panels_down)),
        )
    await _edit(ctx.bot, chat_id, message_id, await overview_screen(ctx, _, note))


# --- helpers ---------------------------------------------------------------------------------------------------------


def _spawn(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


async def _edit(bot: Bot, chat_id: int, message_id: int | None, screen: Screen) -> None:
    """Update the progress message (or send a new one if it cannot be edited)."""
    try:
        if message_id is not None:
            await bot.edit_message_text(
                text=screen.text, chat_id=chat_id, message_id=message_id, reply_markup=screen.kb, parse_mode="HTML"
            )
            return
    except TelegramBadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        log.debug("progress edit failed: %s", exc)
    try:
        await bot.send_message(chat_id, screen.text, reply_markup=screen.kb, parse_mode="HTML")
    except Exception as exc:
        log.warning("cannot report bulk progress to %s: %s", chat_id, exc)
