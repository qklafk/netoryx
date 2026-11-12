"""Cost sharing job: on the configured day of the month, remind every member of their share."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.modules import JobSpec
from app.modules.cost_sharing import service
from app.modules.cost_sharing.bot import month_name, remind
from app.services.notify import notify_admins
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

REMIND_HOUR = 12  # local time: a polite hour for money talk


async def monthly_job(ctx: AppContext) -> None:
    settings = ctx.settings
    config = service.config_from(settings)
    if not config.ready:
        return
    now = utcnow()
    if now.astimezone(settings.zone).day < config.day:
        return
    period = service.period_of(now, settings)
    record = await service.start_period(ctx.session_factory, settings, config, period, now)
    if record is None:
        return  # already sent this month, or nobody to ask
    sent = await remind(ctx, record, record.members)
    _ = ctx.i18n.translator(settings.default_language, settings.zone)
    text = _(
        "cost_sharing.owner_started",
        month=month_name(_, period),
        count=sent,
        share=_.money(record.share_kop),
        total=_.money(record.total_kop),
    )
    await notify_admins(ctx.bot, settings, text)
    log.info("cost sharing: %s reminders sent for %s", sent, period)


JOBS = [JobSpec(id="cost_sharing.monthly", func=monthly_job, trigger="cron", kwargs={"hour": REMIND_HOUR, "minute": 0})]
