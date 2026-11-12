"""Plugin adapter for the existing backup engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from installer.models import ModuleManifest

from .contracts import HealthReport, PluginContext
from .scheduler import SCHEDULER_CONTRIBUTIONS, SchedulerContributionService

BACKUP_SERVICE = "infrastructure.backups"
BACKUP_JOB_IDS = ("housekeeping.backup",)


@dataclass(slots=True)
class BackupOperations:
    engine: Any
    settings_provider: Any
    runtime_context: Any | None = None

    async def create(self) -> Any:
        from app.services import backups

        settings = self.settings_provider.current()
        return await backups.create(
            self.engine, backups.directory_from(settings), password=backups.password_from(settings)
        )

    async def scheduled(self) -> None:
        if self.runtime_context is None:
            raise RuntimeError("backup runtime context is unavailable")
        from app.modules.housekeeping.jobs import backup_job

        await backup_job(self.runtime_context)


class BackupsPlugin:
    def __init__(self, manifest: ModuleManifest) -> None:
        self.manifest = manifest

    async def install(self, context: PluginContext) -> None:
        context.data.set("installed", "true")

    async def configure(self, context: PluginContext, values: dict[str, Any]) -> None:
        if values:
            raise ValueError("backup settings remain managed by the existing settings provider")
        context.data.set("configured", "true")

    async def enable(self, context: PluginContext) -> None:
        if context.services.service(BACKUP_SERVICE) is None:
            raise RuntimeError("backup service is unavailable in this Core runtime")
        scheduler = context.services.service(SCHEDULER_CONTRIBUTIONS)
        if scheduler is not None:
            await cast(SchedulerContributionService, scheduler).activate(self.manifest.identifier, self._jobs(context))
        context.data.set("enabled", "true")

    async def disable(self, context: PluginContext) -> None:
        scheduler = context.services.service(SCHEDULER_CONTRIBUTIONS)
        if scheduler is not None:
            await cast(SchedulerContributionService, scheduler).deactivate(self.manifest.identifier, BACKUP_JOB_IDS)
        context.data.set("enabled", "false")

    async def healthcheck(self, context: PluginContext) -> HealthReport:
        available = context.services.service(BACKUP_SERVICE) is not None
        enabled = context.data.get("enabled") == "true"
        return HealthReport(available and enabled, "ready" if available and enabled else "disabled or unavailable")

    async def backup(self, context: PluginContext) -> Any:
        if context.data.get("enabled") != "true":
            raise RuntimeError("backup plugin is disabled")
        operations = context.services.service(BACKUP_SERVICE)
        if not isinstance(operations, BackupOperations):
            raise RuntimeError("backup service is unavailable in this Core runtime")
        return await operations.create()

    async def update(self, context: PluginContext, target_version: str) -> None:
        raise NotImplementedError(f"update to {target_version} is not supported")

    async def uninstall(self, context: PluginContext, *, purge_data: bool) -> None:
        await self.disable(context)
        if purge_data:
            context.data.delete_all()

    def _jobs(self, context: PluginContext) -> tuple[Any, ...]:
        operations = context.services.service(BACKUP_SERVICE)
        if not isinstance(operations, BackupOperations):
            raise RuntimeError("backup service is unavailable in this Core runtime")
        from app.core.modules import JobSpec

        def schedule(settings: Any) -> dict[str, Any]:
            return {"hour": settings.backup_time.hour, "minute": settings.backup_time.minute}

        async def run(_: Any) -> None:
            await operations.scheduled()

        return (
            JobSpec(
                id="housekeeping.backup",
                func=run,
                trigger="cron",
                schedule=schedule,
                enabled=lambda settings: settings.backup_enabled,
                lock_ttl_sec=3600,
            ),
        )
