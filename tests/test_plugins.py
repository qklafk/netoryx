from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from app import __main__ as cli
from app.config import Settings
from app.core.modules import FeatureModule
from app.core.plugins import UnsupportedOperation
from app.core.plugins.backups import BACKUP_SERVICE, BackupOperations
from app.core.plugins.bridge import bridge_feature_modules
from app.core.plugins.contracts import Capability, ModuleStatus, ServiceRegistry
from app.core.plugins.registry import PluginFactory, PluginRegistry, builtin_plugin_registry
from app.core.plugins.runtime import CoreRuntime
from app.core.plugins.scheduler import SCHEDULER_CONTRIBUTIONS
from app.db.models import Base
from app.db.session import create_engine
from installer.models import DeploymentRole, InstallRequest, ModuleManifest, ProductProfile
from installer.planner import build_plan
from installer.registry import ManifestError, ModuleRegistry


async def test_demo_plugin_full_lifecycle_persists_state(tmp_path: Path) -> None:
    state_path = tmp_path / "core.db"
    core = CoreRuntime(state_path)
    try:
        assert {item.identifier for item in core.registry.available()} == {"infrastructure.backups", "system.demo"}
        await core.lifecycle.install("system.demo")
        await core.lifecycle.configure("system.demo", {"label": "test"})
        await core.lifecycle.enable("system.demo")
        assert (await core.lifecycle.healthcheck("system.demo")).healthy
        await core.lifecycle.disable("system.demo")
        assert not (await core.lifecycle.healthcheck("system.demo")).healthy
        await core.lifecycle.enable("system.demo")
        await core.lifecycle.uninstall("system.demo")
        state = core.store.state("system.demo")
        assert state is not None and state.status is ModuleStatus.UNINSTALLED
        assert core.store.get("system.demo", "label") == "test"
    finally:
        core.close()

    restarted = CoreRuntime(state_path)
    try:
        state = restarted.store.state("system.demo")
        assert state is not None and state.status is ModuleStatus.UNINSTALLED
        assert restarted.store.get("system.demo", "label") == "test"
    finally:
        restarted.close()


async def test_failed_configuration_is_recorded(tmp_path: Path) -> None:
    core = CoreRuntime(tmp_path / "core.db")
    try:
        await core.lifecycle.install("system.demo")
        with pytest.raises(ValueError, match="label"):
            await core.lifecycle.configure("system.demo", {})
        state = core.store.state("system.demo")
        assert state is not None and state.status is ModuleStatus.FAILED
    finally:
        core.close()


async def test_unsupported_update_is_not_reported_as_success(tmp_path: Path) -> None:
    core = CoreRuntime(tmp_path / "core.db")
    try:
        await core.lifecycle.install("system.demo")
        with pytest.raises(UnsupportedOperation, match="not supported"):
            await core.lifecycle.update("system.demo", "2.0.0")
    finally:
        core.close()


async def test_backup_plugin_uses_existing_engine_and_preserves_files(tmp_path: Path) -> None:
    database = tmp_path / "app.db"
    engine = create_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = Settings(
        _env_file=None, database_url=f"sqlite+aiosqlite:///{database}", backup_dir=str(tmp_path / "backups")
    )

    class Provider:
        def current(self) -> Settings:
            return settings

    services = ServiceRegistry()
    services.register(Capability(BACKUP_SERVICE, "test"), BackupOperations(engine, Provider()))
    core = CoreRuntime(tmp_path / "core.db", services=services)
    try:
        await core.lifecycle.install(BACKUP_SERVICE)
        await core.lifecycle.configure(BACKUP_SERVICE, {})
        await core.lifecycle.enable(BACKUP_SERVICE)
        info = await core.lifecycle.backup(BACKUP_SERVICE)
        assert info.path.is_file()
        await core.lifecycle.disable(BACKUP_SERVICE)
        await core.lifecycle.uninstall(BACKUP_SERVICE)
        assert info.path.is_file()
    finally:
        core.close()
        await engine.dispose()


async def test_backup_lifecycle_restores_scheduler_contribution_after_restart(tmp_path: Path) -> None:
    class SchedulerContributions:
        def __init__(self) -> None:
            self.activations: list[tuple[str, tuple[object, ...]]] = []
            self.deactivations: list[str] = []

        async def activate(self, owner: str, specs: tuple[object, ...]) -> None:
            self.activations.append((owner, specs))

        async def deactivate(self, owner: str, job_ids: object = ()) -> None:
            self.deactivations.append(owner)

    class Provider:
        def current(self) -> Settings:
            return Settings(_env_file=None)

    state_path = tmp_path / "core.db"
    scheduler = SchedulerContributions()
    services = ServiceRegistry()
    services.register(Capability(BACKUP_SERVICE, "test"), BackupOperations(object(), Provider()))
    services.register(Capability(SCHEDULER_CONTRIBUTIONS, "test"), scheduler)
    core = CoreRuntime(state_path, services=services)
    try:
        await core.lifecycle.install(BACKUP_SERVICE)
        await core.lifecycle.enable(BACKUP_SERVICE)
        await core.lifecycle.enable(BACKUP_SERVICE)
        assert len(scheduler.activations) == 2
        assert all(item[0] == BACKUP_SERVICE and len(item[1]) == 1 for item in scheduler.activations)
    finally:
        core.close()

    restored_scheduler = SchedulerContributions()
    restored_services = ServiceRegistry()
    restored_services.register(Capability(BACKUP_SERVICE, "test"), BackupOperations(object(), Provider()))
    restored_services.register(Capability(SCHEDULER_CONTRIBUTIONS, "test"), restored_scheduler)
    restarted = CoreRuntime(state_path, services=restored_services)
    try:
        await restarted.reconcile_runtime()
        assert len(restored_scheduler.activations) == 1
        await restarted.lifecycle.disable(BACKUP_SERVICE)
        assert restored_scheduler.deactivations == [BACKUP_SERVICE]
        assert restarted.store.state(BACKUP_SERVICE).status is ModuleStatus.DISABLED
    finally:
        restarted.close()


async def test_backup_scheduler_registration_failure_is_persisted(tmp_path: Path) -> None:
    class FailingScheduler:
        async def activate(self, owner: str, specs: tuple[object, ...]) -> None:
            raise RuntimeError("scheduler is unavailable")

    class Provider:
        def current(self) -> Settings:
            return Settings(_env_file=None)

    services = ServiceRegistry()
    services.register(Capability(BACKUP_SERVICE, "test"), BackupOperations(object(), Provider()))
    services.register(Capability(SCHEDULER_CONTRIBUTIONS, "test"), FailingScheduler())
    core = CoreRuntime(tmp_path / "core.db", services=services)
    try:
        await core.lifecycle.install(BACKUP_SERVICE)
        with pytest.raises(RuntimeError, match="scheduler is unavailable"):
            await core.lifecycle.enable(BACKUP_SERVICE)
        assert core.store.state(BACKUP_SERVICE).status is ModuleStatus.FAILED
    finally:
        core.close()


def test_manifest_missing_and_cyclic_dependencies_are_rejected() -> None:
    one = manifest("one", depends_on=("missing",))
    with pytest.raises(ManifestError, match="unknown dependency"):
        ModuleRegistry({"one": one}).validate_references()
    first = manifest("first", depends_on=("second",))
    second = manifest("second", depends_on=("first",))
    with pytest.raises(ManifestError, match="cyclic dependency"):
        ModuleRegistry({"first": first, "second": second}).validate_references()


def test_duplicate_ids_and_conflicts_are_rejected(tmp_path: Path) -> None:
    content = (
        'id = "duplicate"\nversion = 1\ntitle = "Test"\ndescription = "Test"\n'
        'roles = ["all-in-one"]\nprofiles = ["personal"]\n'
    )
    (tmp_path / "first.toml").write_text(content, encoding="utf-8")
    (tmp_path / "second.toml").write_text(content, encoding="utf-8")
    with pytest.raises(ManifestError, match="duplicate"):
        ModuleRegistry.from_directory(tmp_path)
    first = manifest("first", conflicts=("second",))
    second = manifest("second")
    plan = build_plan(
        ModuleRegistry({"first": first, "second": second}),
        InstallRequest(ProductProfile.PERSONAL, DeploymentRole.ALL_IN_ONE, ("first", "second")),
    )
    assert not plan.valid and any("module conflict" in error for error in plan.errors)


def test_incompatible_sdk_core_and_legacy_bridge() -> None:
    incompatible = manifest("incompatible", sdk_compatibility=">=2,<3")
    with pytest.raises(ValueError, match="incompatible"):
        PluginRegistry(
            ModuleRegistry({"incompatible": incompatible}),
            {"incompatible": cast(PluginFactory, lambda _: None)},
        )
    incompatible_core = manifest("incompatible-core", core_compatibility=">=2,<3")
    with pytest.raises(ValueError, match="Core"):
        PluginRegistry(
            ModuleRegistry({"incompatible-core": incompatible_core}),
            {"incompatible-core": cast(PluginFactory, lambda _: None)},
        )
    feature = FeatureModule(name="legacy", title_ru="Легаси", title_en="Legacy")
    bridge = bridge_feature_modules([feature])
    assert bridge["legacy"].identifier == "legacy.legacy"


def test_core_settings_do_not_require_a_bot_token() -> None:
    settings = Settings(_env_file=None)
    assert settings.bot_token is None


def test_core_cli_does_not_load_application_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    state_path = tmp_path / "absent" / "core.db"
    assert cli.main(["core", "status", "--state-db", str(state_path)]) == 0
    assert not state_path.parent.exists()


def test_read_only_core_does_not_modify_existing_state(tmp_path: Path) -> None:
    state_path = tmp_path / "core.db"
    writable = CoreRuntime(state_path)
    try:
        writable.store.set_state("system.demo", 1, ModuleStatus.DISABLED)
    finally:
        writable.close()
    before = state_path.read_bytes()

    readonly = CoreRuntime(state_path, read_only=True)
    try:
        state = readonly.store.state("system.demo")
        assert state is not None and state.status is ModuleStatus.DISABLED
        assert readonly.status()["installed"]
        with pytest.raises(RuntimeError, match="read-only"):
            readonly.store.set_state("system.demo", 1, ModuleStatus.ENABLED)
    finally:
        readonly.close()

    assert state_path.read_bytes() == before


def test_builtin_registry_is_trusted_and_repeatable() -> None:
    first = builtin_plugin_registry()
    second = builtin_plugin_registry()
    assert (
        {item.identifier for item in first.available()}
        == {item.identifier for item in second.available()}
        == {
            "infrastructure.backups",
            "system.demo",
        }
    )


def manifest(
    identifier: str,
    *,
    depends_on: tuple[str, ...] = (),
    conflicts: tuple[str, ...] = (),
    sdk_compatibility: str = ">=1,<2",
    core_compatibility: str = ">=1,<2",
) -> ModuleManifest:
    return ModuleManifest(
        identifier=identifier,
        version=1,
        title=identifier,
        description=identifier,
        roles=frozenset({DeploymentRole.ALL_IN_ONE}),
        profiles=frozenset({ProductProfile.PERSONAL}),
        depends_on=depends_on,
        conflicts=conflicts,
        sdk_compatibility=sdk_compatibility,
        core_compatibility=core_compatibility,
    )
