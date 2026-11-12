"""Live progress of a running broadcast: one admin message edited as the sender advances.

The message is remembered in ``kv`` (``bcast:msg:<id>``), so the sender (possibly another process) can update it.
A broadcast started by the schedule gets a fresh progress message in its author's chat.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from app.core.ui import Screen
from app.db.models import Broadcast, BroadcastStatus, User
from app.db.session import read_session
from app.keyboards import Button, kb
from app.modules.broadcasts.callbacks import BcCb
from app.services import kv
from app.services.broadcasts import Progress

if TYPE_CHECKING:
    from app.core.context import AppContext
    from app.core.i18n import Translator

log = logging.getLogger(__name__)

JOB_ID = "broadcasts.runner"
MESSAGE_KEY = "bcast:msg:{id}"
EDIT_INTERVAL_SEC = 3.0
BAR_WIDTH = 12


@dataclass(frozen=True, slots=True)
class Snapshot:
    broadcast_id: int
    status: BroadcastStatus
    total: int
    sent: int
    failed: int
    blocked: int

    @property
    def processed(self) -> int:
        return self.sent + self.failed + self.blocked


def snapshot(broadcast: Broadcast) -> Snapshot:
    return Snapshot(
        broadcast.id, broadcast.status, broadcast.total, broadcast.sent, broadcast.failed, broadcast.blocked
    )


def from_progress(item: Progress) -> Snapshot:
    return Snapshot(item.broadcast_id, item.status, item.total, item.sent, item.failed, item.blocked)


def bar(done: int, total: int, width: int = BAR_WIDTH) -> tuple[str, int]:
    """``("▰▰▰▱▱▱", 50)`` for half done."""
    percent = 100 if total <= 0 else min(100, done * 100 // total)
    filled = width * percent // 100
    return "▰" * filled + "▱" * (width - filled), percent


def progress_screen(_: Translator, item: Snapshot) -> Screen:
    line, percent = bar(item.processed, item.total)
    key = {
        BroadcastStatus.RUNNING: "broadcasts.progress.running",
        BroadcastStatus.DONE: "broadcasts.progress.done",
        BroadcastStatus.CANCELED: "broadcasts.progress.canceled",
    }.get(item.status, "broadcasts.progress.running")
    text = _(
        key,
        id=item.broadcast_id,
        bar=line,
        percent=percent,
        total=_.number(item.total),
        processed=_.number(item.processed),
        sent=_.number(item.sent),
        blocked=_.number(item.blocked),
        failed=_.number(item.failed),
    )
    return Screen(text, progress_kb(_, item))


def progress_kb(_: Translator, item: Snapshot) -> InlineKeyboardMarkup:
    if item.status == BroadcastStatus.RUNNING:
        return kb(
            [
                [
                    Button.cb(_("nav.refresh"), BcCb(a="prog", id=item.broadcast_id)),
                    Button.cb(_("broadcasts.btn.stop"), BcCb(a="stop", id=item.broadcast_id), style="danger"),
                ]
            ]
        )
    return kb([[Button.cb(_("broadcasts.btn.to_list"), BcCb(a="home"))]])


async def remember_message(ctx: AppContext, broadcast_id: int, chat_id: int, message_id: int, lang: str) -> None:
    payload = json.dumps({"chat": chat_id, "msg": message_id, "lang": lang})
    async with ctx.session_factory.begin() as session:
        await kv.set(session, MESSAGE_KEY.format(id=broadcast_id), payload)


async def _load_message(ctx: AppContext, broadcast_id: int) -> dict[str, Any] | None:
    async with read_session(ctx.session_factory) as session:
        raw = await kv.get(session, MESSAGE_KEY.format(id=broadcast_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("chat") and data.get("msg") else None


async def forget_message(ctx: AppContext, broadcast_id: int) -> None:
    async with ctx.session_factory.begin() as session:
        await kv.delete(session, MESSAGE_KEY.format(id=broadcast_id))


def kick_runner(ctx: AppContext) -> bool:
    """Start the sender job right away when this process runs background jobs (else the worker picks it up)."""
    scheduler = getattr(ctx, "scheduler", None)
    if scheduler is None or JOB_ID not in getattr(scheduler, "job_ids", ()):
        return False
    try:
        return bool(scheduler.run_now(JOB_ID))
    except Exception:
        log.exception("could not start the broadcast sender")
        return False


class Reporter:
    """``on_progress`` callback of :func:`app.services.broadcasts.run`: edits the admin's progress message."""

    def __init__(self, ctx: AppContext, broadcast_id: int, *, interval: float = EDIT_INTERVAL_SEC) -> None:
        self._ctx = ctx
        self._id = broadcast_id
        self._interval = interval
        self._last = 0.0
        self._target: dict[str, Any] | None = None
        self.lock_renewer: Callable[[], Awaitable[None]] | None = (
            None  # optional async callable, run on every report (keeps the job lock alive)
        )

    async def prepare(self, created_by: int) -> None:
        """Find the progress message, or post one to the author (a scheduled broadcast just started)."""
        self._target = await _load_message(self._ctx, self._id)
        if self._target is not None:
            return
        lang = await _author_language(self._ctx, created_by)
        _ = self._ctx.i18n.translator(lang, self._ctx.settings.zone)
        async with read_session(self._ctx.session_factory) as session:
            broadcast = await session.get(Broadcast, self._id)
        if broadcast is None:
            return
        screen = progress_screen(_, snapshot(broadcast))
        try:
            sent = await self._ctx.bot.send_message(created_by, screen.text, reply_markup=screen.kb)
        except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
            log.info("broadcast %s: cannot post progress to %s: %s", self._id, created_by, exc)
            return
        self._target = {"chat": created_by, "msg": sent.message_id, "lang": lang}
        await remember_message(self._ctx, self._id, created_by, sent.message_id, lang)

    async def __call__(self, item: Progress) -> None:
        if self.lock_renewer is not None:
            await self.lock_renewer()
        final = item.status != BroadcastStatus.RUNNING
        now = time.monotonic()
        if not final and now - self._last < self._interval:
            return
        self._last = now
        await self.show(from_progress(item))
        if final:
            await forget_message(self._ctx, self._id)

    async def show(self, item: Snapshot) -> None:
        if self._target is None:
            return
        _ = self._ctx.i18n.translator(str(self._target.get("lang") or "ru"), self._ctx.settings.zone)
        screen = progress_screen(_, item)
        try:
            await self._ctx.bot.edit_message_text(
                text=screen.text,
                chat_id=int(self._target["chat"]),
                message_id=int(self._target["msg"]),
                reply_markup=screen.kb,
            )
        except TelegramRetryAfter:
            self._last = time.monotonic() + self._interval  # back off; the next report will try again
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            if "not modified" not in str(exc).lower():
                log.debug("broadcast %s: progress edit failed: %s", self._id, exc)


async def _author_language(ctx: AppContext, user_id: int) -> str:
    async with read_session(ctx.session_factory) as session:
        user = await session.get(User, user_id)
    return ctx.i18n.normalize(user.language if user else None)
