"""Keep the servers in line with the Telegram-only settings, and tell the owners about the link once."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.modules import JobSpec
from app.core.ui import Screen, send_screen
from app.db.models import User
from app.db.session import read_session
from app.services import tg_only
from app.services.notifications import AdminEvent, notify

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

RECONCILE_INTERVAL_MIN = 15


async def reconcile_job(ctx: AppContext) -> None:
    report = await tg_only.reconcile(ctx.session_factory, ctx.panels, ctx.settings)
    if report.failed:
        log.info("Telegram-only: %d panel(s) not in sync", len(report.failed))
    await notify_owners(ctx)
    await alert_on_traffic(ctx)


async def alert_on_traffic(ctx: AppContext) -> None:
    """Tell the admins when the shared client moved an unusual amount of traffic in a day (once a day at most)."""
    moved = await tg_only.check_daily_traffic(ctx.session_factory, ctx.panels, ctx.settings)
    if moved is None:
        return
    limit = ctx.settings.tg_only_daily_alert_gb
    text = ctx.i18n.t(
        "tg_only.traffic_alert", ctx.settings.default_language, gb=f"{moved / tg_only.GIB:.1f}", limit=limit
    )
    notify(ctx, AdminEvent.ALERTS, text, throttle_key="tgonly-traffic", throttle_sec=6 * 3600.0)


async def notify_owners(ctx: AppContext) -> None:
    """Send the link to each owner who has not got it yet (after the first server became ready)."""
    owners = await tg_only.pending_owner_notices(ctx.session_factory, ctx.settings)
    if not owners:
        return
    view = await tg_only.admin_view(ctx.session_factory, ctx.settings)
    for owner in owners:
        async with read_session(ctx.session_factory) as session:
            user = await session.get(User, owner)
        lang = ctx.i18n.normalize(user.language if user is not None else None)
        text = ctx.i18n.t("tg_only.first_notice", lang, page=ctx.i18n.esc(view.page), url=ctx.i18n.esc(view.url))
        try:
            await send_screen(ctx.bot, owner, Screen(text))
        except Exception as exc:  # the owner may not have started the bot yet: try again on the next run
            log.info("Telegram-only: could not send the link to %s: %s", owner, type(exc).__name__)
            continue
        await tg_only.mark_notified(ctx.session_factory, owner)


JOBS = [
    JobSpec(
        id="tg_only.reconcile",
        func=reconcile_job,
        kwargs={"minutes": RECONCILE_INTERVAL_MIN},
        lock_ttl_sec=900,
        run_on_start=True,
    )
]
