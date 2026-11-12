"""Shared helpers of the web admin API: permissions, the acting admin, audit, background work and errors."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.services import audit
from app.web.auth import AdminPrincipal, admin_user
from app.web.deps import get_ctx
from app.web.errors import ApiError

log = logging.getLogger(__name__)

SOURCE = "web"
_BACKGROUND: set[asyncio.Task[Any]] = set()


@dataclass(frozen=True, slots=True)
class Actor:
    """The admin performing a request: who, from where, with which permissions."""

    principal: AdminPrincipal
    ctx: Any  # AppContext

    @property
    def tg_id(self) -> int:
        return self.principal.tg_id

    @property
    def ip(self) -> str:
        return self.principal.ip

    def can(self, permission: Permission) -> bool:
        return self.principal.can(permission)

    def require(self, *permissions: Permission) -> None:
        if not all(self.principal.can(p) for p in permissions):
            raise ApiError(403, "forbidden", "Not enough permissions")

    async def audit(
        self,
        session: AsyncSession,
        action: str,
        target_type: str | None = None,
        target_id: int | str | None = None,
        **details: Any,
    ) -> None:
        """Write an audit record (source ``web`` with the client IP) in ``session``'s transaction."""
        await audit.log(session, self.tg_id, action, target_type, target_id, SOURCE, self.ip, **details)


def require(*permissions: Permission) -> Callable[..., Awaitable[Actor]]:
    """Dependency: the logged-in admin holding every one of ``permissions`` (403 otherwise)."""

    async def dependency(
        request: Request,
        principal: Annotated[AdminPrincipal, Depends(admin_user())],
        ctx: Annotated[Any, Depends(get_ctx)],
    ) -> Actor:
        actor = Actor(principal, ctx)
        actor.require(*permissions)
        return actor

    dependency.__name__ = "require_" + "_".join(str(p) for p in permissions) if permissions else "require_admin"
    return dependency


AnyAdmin = Annotated[Actor, Depends(require())]


def not_found(what: str = "Not found") -> ApiError:
    return ApiError(404, "not_found", what)


def bad_request(code: str, message: str) -> ApiError:
    return ApiError(400, code, message)


def conflict(code: str, message: str) -> ApiError:
    return ApiError(409, code, message)


def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
    """Run ``coro`` in the background (kept referenced; failures are logged)."""

    async def runner() -> None:
        try:
            await coro
        except Exception:
            log.exception("background task %s failed", name)

    task = asyncio.create_task(runner(), name=name)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


def kick_jobs(ctx: Any, *candidates: str) -> list[str]:
    """Start scheduled jobs now (by exact id, or ``prefix*``) if the scheduler runs in this process."""
    scheduler: Any = getattr(ctx, "scheduler", None)
    if scheduler is None:
        return []
    job_ids: list[str] = list(getattr(scheduler, "job_ids", []) or [])
    started: list[str] = []
    for job_id in job_ids:
        if not any(job_id == c or (c.endswith("*") and job_id.startswith(c[:-1])) for c in candidates):
            continue
        try:
            if scheduler.run_now(job_id):
                started.append(job_id)
        except Exception:
            log.exception("could not start job %s", job_id)
    return started


def kick_reconcile(ctx: Any) -> list[str]:
    """Push pending panel changes now instead of waiting for the next reconcile run."""
    scheduler = getattr(ctx, "scheduler", None)
    job_ids: list[str] = list(getattr(scheduler, "job_ids", []) or []) if scheduler is not None else []
    wanted = [j for j in job_ids if j.endswith("reconcile") and not j.startswith(("payments", "tg_proxy"))]
    return kick_jobs(ctx, *wanted) if wanted else []


async def _sync(ctx: Any, sub_id: int) -> None:
    from app.services import provisioning

    result = await provisioning.sync_subscription(ctx.session_factory, ctx.panels, ctx.settings, sub_id)
    if result.failed:
        log.info("subscription %s: %d panel accounts failed to sync: %s", sub_id, result.failed, result.errors[:3])


def schedule_sync(ctx: Any, sub_id: int | None) -> None:
    """Push one subscription to its panels in the background (call after the transaction is committed)."""
    if sub_id is not None and getattr(ctx, "panels", None) is not None:
        spawn(_sync(ctx, sub_id), f"sync-subscription-{sub_id}")


async def drain_background(wait_sec: float = 10.0) -> None:
    """Wait for background work started by admin requests (graceful shutdown, tests)."""
    while _BACKGROUND:
        pending = set(_BACKGROUND)
        _done, still = await asyncio.wait(pending, timeout=wait_sec)
        if still:
            log.warning("%d admin background tasks still running", len(still))
            return
