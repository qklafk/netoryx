"""Outgoing webhook jobs: deliver queued events and drop old delivery history."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.modules import JobSpec
from app.services import webhooks
from app.services.notifications import AdminEvent, notify

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

DELIVER_EVERY_SEC = 15
MAX_BATCHES_PER_RUN = 10


def report_disabled(ctx: AppContext, disabled: list[webhooks.DisabledEndpoint]) -> None:
    """Tell administrators that endpoints were switched off after repeated failures."""
    lang = ctx.settings.default_language
    for item in disabled:
        notify(
            ctx,
            AdminEvent.ALERTS,
            ctx.i18n.t("webhooks.disabled", lang, id=item.id, host=ctx.i18n.esc(item.host), count=item.failures),
        )


async def deliver_job(ctx: AppContext) -> None:
    """Send due deliveries; keeps going while full batches come back (bounded per run)."""
    settings = ctx.settings
    for _ in range(MAX_BATCHES_PER_RUN):
        run = await webhooks.deliver(
            ctx.session_factory,
            ctx.secret_box,
            dev_mode=settings.dev_mode,
            auto_disable_after=settings.webhooks_auto_disable_after,
        )
        if run.disabled:
            report_disabled(ctx, run.disabled)
        if run.sent + run.failed + run.dead < webhooks.BATCH_SIZE:
            return


async def cleanup_job(ctx: AppContext) -> None:
    removed = await webhooks.purge(ctx.session_factory, ctx.settings.webhooks_retention_days)
    if removed:
        log.info("webhooks: removed %d old deliveries", removed)


JOBS = [
    JobSpec(id="webhooks.deliver", func=deliver_job, kwargs={"seconds": DELIVER_EVERY_SEC}, lock_ttl_sec=600),
    JobSpec(id="webhooks.cleanup", func=cleanup_job, trigger="cron", kwargs={"hour": 4, "minute": 10}),
]
