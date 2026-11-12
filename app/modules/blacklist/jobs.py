"""Blacklist job: periodic download of the configured lists."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.core.modules import JobSpec
from app.modules.blacklist.service import refresh_and_apply

if TYPE_CHECKING:
    from app.config import Settings
    from app.core.context import AppContext

log = logging.getLogger(__name__)


async def refresh_job(ctx: AppContext) -> None:
    refreshed, applied = await refresh_and_apply(ctx)
    log.info(
        "blacklist: %d IDs, %d sources updated, %d failed; %d users banned, %d unbanned, %d newly flagged",
        refreshed.total_ids,
        refreshed.updated,
        refreshed.failed,
        len(applied.banned),
        len(applied.unbanned),
        len(applied.new_flagged),
    )


def _schedule(settings: Settings) -> dict[str, Any]:
    return {"minutes": max(5, settings.blacklist_refresh_minutes)}


def _enabled(settings: Settings) -> bool:
    return settings.blacklist_enabled


JOBS = [
    JobSpec(
        id="blacklist.refresh",
        func=refresh_job,
        trigger="interval",
        schedule=_schedule,
        enabled=_enabled,
        lock_ttl_sec=900,
        run_on_start=True,
    )
]
