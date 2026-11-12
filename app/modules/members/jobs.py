"""Members job: finish removals that waited for an unreachable server."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.modules import JobSpec
from app.modules.members.service import finalize_removals

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)


async def finalize_job(ctx: AppContext) -> None:
    done = await finalize_removals(ctx.session_factory)
    if done:
        log.info("members: %d pending removals completed", done)


JOBS = [JobSpec(id="members.finalize", func=finalize_job, trigger="interval", kwargs={"minutes": 10})]
