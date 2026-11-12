"""Background sender: starts scheduled broadcasts and resumes running ones (also after a restart)."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from app.core.modules import JobSpec
from app.db.models import Broadcast, BroadcastStatus
from app.db.session import read_session
from app.modules.broadcasts import progress
from app.services import broadcasts
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

RUN_INTERVAL_SEC = 15
LOCK_TTL_SEC = 900  # renewed while sending, so a long broadcast keeps its lock
LOCK_RENEW_EVERY_SEC = 60.0


class LockRenewer:
    """Extends the scheduler's lock of this job while a long broadcast is being sent.

    When another process holds the lock (it expired and was taken over), sending stops at the current checkpoint
    (:class:`broadcasts.Interrupted`), so the same users never get the message from two processes.
    """

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._last = time.monotonic()
        self.lost = False

    async def __call__(self) -> None:
        from app.scheduler import LOCK_PREFIX

        if self.lost:
            raise broadcasts.Interrupted("the broadcast sender lock is held by another process")
        locks = getattr(self._ctx, "locks", None)
        if locks is None or time.monotonic() - self._last < LOCK_RENEW_EVERY_SEC:
            return
        self._last = time.monotonic()
        try:
            held = await locks.extend(LOCK_PREFIX + progress.JOB_ID, LOCK_TTL_SEC)
        except Exception:  # storage hiccup: the lock is still valid for a while, try again next time
            log.exception("could not renew the broadcast sender lock")
            return
        if not held:
            self.lost = True
            log.warning("broadcast sender lock was taken over by another process; pausing")
            raise broadcasts.Interrupted("the broadcast sender lock is held by another process")


async def run_broadcasts(ctx: AppContext) -> None:
    """Send every due broadcast, one at a time, at ``BROADCAST_RATE_PER_SEC``."""
    now = utcnow()
    async with read_session(ctx.session_factory) as session:
        due = await broadcasts.due_ids(session, now)
    renewer = LockRenewer(ctx)
    for broadcast_id in due:
        if renewer.lost:
            return
        async with read_session(ctx.session_factory) as session:
            broadcast = await session.get(Broadcast, broadcast_id)
            created_by = broadcast.created_by if broadcast is not None else None
        if created_by is None:
            continue
        reporter = progress.Reporter(ctx, broadcast_id)
        reporter.lock_renewer = renewer
        await reporter.prepare(created_by)
        result = await broadcasts.run(
            ctx, broadcast_id, rate_per_sec=float(ctx.settings.broadcast_rate_per_sec), on_progress=reporter
        )
        if result is not None and result.status != BroadcastStatus.RUNNING:
            await reporter.show(progress.from_progress(result))
            await progress.forget_message(ctx, broadcast_id)


JOBS = [
    JobSpec(
        id=progress.JOB_ID,
        func=run_broadcasts,
        trigger="interval",
        kwargs={"seconds": RUN_INTERVAL_SEC},
        lock_ttl_sec=LOCK_TTL_SEC,
    ),
]
