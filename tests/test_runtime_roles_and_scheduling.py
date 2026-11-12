from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.modules import JobSpec
from app.core.plugins import Capability, CoreRuntime, ServiceRegistry
from app.core.plugins.backups import BACKUP_SERVICE, BackupOperations
from app.core.plugins.scheduler import SCHEDULER_CONTRIBUTIONS
from app.db.session import SessionFactory
from tests.test_runtime_scheduler import make, module


async def test_disabled_backup_plugin_keeps_legacy_job_suppressed_after_restart(
    session_factory: SessionFactory, tmp_path: Path
) -> None:
    async def legacy(_: Any) -> None:
        return None

    spec = JobSpec("housekeeping.backup", legacy, kwargs={"hours": 24})
    _, scheduler = await make(session_factory, [module("housekeeping", [spec])], use_locks=False)

    class Provider:
        def current(self) -> Any:
            return None

    class Bridge:
        async def activate(self, owner: str, specs: Any, *a: Any) -> None:
            await scheduler.activate_contribution(owner, tuple(specs))

        async def deactivate(self, owner: str, job_ids: Any = ()) -> None:
            await scheduler.deactivate_contribution(owner, job_ids=tuple(job_ids))

    state = tmp_path / "core.db"
    services = ServiceRegistry()
    services.register(Capability(BACKUP_SERVICE, "t"), BackupOperations(object(), Provider()))
    services.register(Capability(SCHEDULER_CONTRIBUTIONS, "t"), Bridge())
    core = CoreRuntime(state, services=services)
    try:
        await core.lifecycle.install(BACKUP_SERVICE)
        await core.lifecycle.enable(BACKUP_SERVICE)
        await core.lifecycle.disable(BACKUP_SERVICE)
    finally:
        core.close()

    # A fresh process: the scheduler has no in-memory contribution state.
    _, scheduler = await make(session_factory, [module("housekeeping", [spec])], use_locks=False)
    services = ServiceRegistry()
    services.register(Capability(BACKUP_SERVICE, "t"), BackupOperations(object(), Provider()))
    services.register(Capability(SCHEDULER_CONTRIBUTIONS, "t"), Bridge())
    restarted = CoreRuntime(state, services=services)
    try:
        await restarted.reconcile_runtime()
        await scheduler.start()
        assert not scheduler.is_scheduled("housekeeping.backup")
    finally:
        restarted.close()
        await scheduler.shutdown()


async def test_non_job_roles_start_while_another_process_owns_core_state(tmp_path: Path) -> None:
    from app.core.plugins.ownership import CoreOwnership
    from app.runtime import Runtime
    from tests.test_runtime_boot import FakeTelegram, factories, settings_for

    holder = CoreOwnership(tmp_path / "core.db")
    holder.acquire()
    bot = Runtime(settings_for(tmp_path, role="bot"), factories=factories(FakeTelegram(), []))
    try:
        await bot.start()
    finally:
        await bot.stop()
        holder.release()


async def test_core_adapter_waits_for_the_health_endpoint(tmp_path: Path) -> None:
    from installer.commands import Command, CommandError, CommandResult
    from installer.ubuntu_adapter import UbuntuCoreDeploymentAdapter

    calls: list[str] = []

    class Runner:
        async def run(self, command: Command) -> CommandResult:
            if command.argv[0] == "curl":
                calls.append("curl")
                if len(calls) < 3:
                    raise CommandError("connection refused")
                return CommandResult(command.argv, 0, "", "")
            return CommandResult(command.argv, 0, "app\n", "")

    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    adapter = UbuntuCoreDeploymentAdapter(compose, runner=Runner(), health_delay_sec=0)  # type: ignore[arg-type]
    await adapter.healthcheck()
    assert len(calls) == 3
