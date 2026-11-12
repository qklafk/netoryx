"""System jobs: scheduled maintenance windows and the notice that the bot works again."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.modules import JobSpec
from app.db.models import User
from app.db.session import read_session
from app.modules.system.texts import t
from app.services.notify import notify_admins, safe_send

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

NOTIFY_BATCH = 500
SEND_INTERVAL_SEC = 0.04  # ~25 messages per second, under Telegram's broadcast limit


def _local(ctx: AppContext, value: datetime) -> str:
    return value.astimezone(ctx.settings.zone).strftime("%d.%m.%Y %H:%M")


async def maintenance_tick(ctx: AppContext) -> None:
    """Announce planned windows to admins, drop finished ones and notify users when maintenance is over."""
    service = ctx.maintenance
    settings = ctx.settings
    lang = settings.default_language
    now = datetime.now(UTC)
    state = await service.state(fresh=True)
    for window in state.windows:
        if not window.announced and window.announce_minutes > 0 and window.announce_at() <= now < window.starts_at:
            text = t(
                ctx,
                "system.maintenance.window_soon",
                lang,
                starts=_local(ctx, window.starts_at),
                ends=_local(ctx, window.ends_at),
            )
            await notify_admins(ctx.bot, settings, text)
            await service.mark_window(window.id, announced=True)
        if not window.started and window.is_active(now):
            text = t(ctx, "system.maintenance.window_started", lang, ends=_local(ctx, window.ends_at))
            await notify_admins(ctx.bot, settings, text)
            await service.mark_window(window.id, started=True)
    for window in await service.prune_windows(now):
        if window.started:
            await notify_admins(ctx.bot, settings, t(ctx, "system.maintenance.window_finished", lang))
    state = await service.state(fresh=True)
    if not state.is_active(now):
        await notify_users_back(ctx)


async def notify_users_back(ctx: AppContext) -> int:
    """Tell users who hit maintenance that the bot works again; returns how many were notified."""
    service = ctx.maintenance
    sent = 0
    while True:
        ids = await service.pop_hits(NOTIFY_BATCH)
        if not ids:
            return sent
        settings = ctx.settings
        if not settings.maintenance_notify_on_finish:
            continue
        async with read_session(ctx.session_factory) as session:
            rows = await session.execute(
                select(User.tg_id, User.language).where(
                    User.tg_id.in_(ids), User.bot_blocked.is_(False), User.is_banned.is_(False)
                )
            )
            languages = {tg_id: language for tg_id, language in rows}
        for user_id in ids:
            language = languages.get(user_id)
            if language is None:
                continue
            text = t(ctx, "system.maintenance.finished", language, brand=settings.brand_name)
            if await safe_send(ctx.bot, user_id, text, session_factory=ctx.session_factory):
                sent += 1
            await asyncio.sleep(SEND_INTERVAL_SEC)


JOBS = [
    JobSpec(id="system.maintenance", func=maintenance_tick, trigger="interval", kwargs={"seconds": 30}),
]
