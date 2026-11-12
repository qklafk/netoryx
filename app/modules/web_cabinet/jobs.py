"""Housekeeping of the web cabinet: expired sessions, login links and used widget payloads."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.modules import JobSpec
from app.services import cabinet

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)


async def purge(ctx: AppContext) -> None:
    sessions = await cabinet.purge_sessions(ctx.session_factory)
    tokens = await cabinet.purge_expired_tokens(ctx.session_factory)
    if sessions or tokens:
        log.info("cabinet cleanup: %d sessions, %d tokens removed", sessions, tokens)


JOBS = [JobSpec(id="cabinet_purge", func=purge, trigger="interval", kwargs={"hours": 6}, lock_ttl_sec=300)]
