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
from app.services import tls_watch
from app.services.notifications import AdminEvent, notify
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


def tls_alert_text(ctx: AppContext, alert: tls_watch.Alert) -> str:
    """Admin message for a certificate alert."""
    lang = ctx.settings.default_language
    esc = ctx.i18n.esc
    host = esc(alert.host)
    expires = _local(ctx, alert.expires_at) if alert.expires_at else "?"
    days = alert.days_left if alert.days_left is not None else 0
    error = esc(alert.error or "?")[:300]
    if alert.kind == "recovered":
        return t(ctx, "system.tls.recovered", lang, host=host, expires=expires)
    if alert.kind == "unreachable":
        return t(ctx, "system.tls.unreachable", lang, host=host, error=error)
    if alert.kind == "warn":
        return t(ctx, "system.tls.warn", lang, host=host, days=days, expires=expires)
    if alert.days_left is not None and alert.days_left < 0:
        state = t(ctx, "system.tls.state_expired", lang, expires=expires)
    elif alert.error:
        state = t(ctx, "system.tls.state_invalid", lang, error=error)
    else:
        state = t(ctx, "system.tls.state_days", lang, days=days, expires=expires)
    return t(ctx, "system.tls.urgent", lang, host=host, state=state)


async def tls_watch_job(ctx: AppContext) -> None:
    """Check the public certificates and tell the admins about expiring or broken ones."""
    alerts = await tls_watch.run_check(ctx.settings, ctx.session_factory)
    for alert in alerts:
        notify(ctx, AdminEvent.ALERTS, tls_alert_text(ctx, alert))


JOBS = [
    JobSpec(id="system.maintenance", func=maintenance_tick, trigger="interval", kwargs={"seconds": 30}),
    JobSpec(
        id="system.tls_watch",
        func=tls_watch_job,
        trigger="interval",
        kwargs={"hours": 12},
        enabled=tls_watch.watch_enabled,
        lock_ttl_sec=300,
        run_on_start=True,
    ),
]
