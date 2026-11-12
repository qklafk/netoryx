"""A small bridge through which plugins contribute jobs to the application scheduler."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

SCHEDULER_CONTRIBUTIONS = "core.scheduler.contributions"


class SchedulerContributionService(Protocol):
    async def activate(self, owner: str, specs: Iterable[Any]) -> None: ...

    async def deactivate(self, owner: str, job_ids: Iterable[str] = ()) -> None: ...

    async def status(self, owner: str) -> dict[str, Any]: ...


class SchedulerContributions:
    """Plugin-facing scheduler contract; it intentionally owns no scheduler itself."""

    def __init__(self) -> None:
        self._scheduler: Any | None = None

    def bind(self, scheduler: Any) -> None:
        if self._scheduler is not None and self._scheduler is not scheduler:
            raise RuntimeError("scheduler contributions are already bound")
        self._scheduler = scheduler

    async def activate(self, owner: str, specs: Iterable[Any]) -> None:
        scheduler = self._require_scheduler()
        await scheduler.activate_contribution(owner, tuple(specs))

    async def deactivate(self, owner: str, job_ids: Iterable[str] = ()) -> None:
        scheduler = self._require_scheduler()
        await scheduler.deactivate_contribution(owner, tuple(job_ids))

    async def status(self, owner: str) -> dict[str, Any]:
        if self._scheduler is None:
            return {"bound": False, "owner": owner, "jobs": []}
        return await self._scheduler.contribution_status(owner)

    def _require_scheduler(self) -> Any:
        if self._scheduler is None:
            raise RuntimeError("scheduler contribution service is unavailable")
        return self._scheduler
