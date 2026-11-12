"""Keep the Xray template of every 3x-ui panel in line with the ad-blocking and YouTube exit settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.modules import JobSpec
from app.services import ad_blocking

if TYPE_CHECKING:
    from app.core.context import AppContext

JOB_ID = "ad_blocking.converge"
INTERVAL_MIN = 10


async def converge_job(ctx: AppContext) -> None:
    settings = ctx.settings
    await ad_blocking.converge(
        ctx.session_factory,
        ctx.panels,
        settings.adblock_enabled,
        ad_blocking.youtube_exit_from_settings(settings),
    )


JOBS = [JobSpec(id=JOB_ID, func=converge_job, kwargs={"minutes": INTERVAL_MIN}, lock_ttl_sec=900)]
