"""Persistent lifecycle operations for trusted plugins."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .contracts import HealthReport, ModuleStatus, PluginContext, ServiceRegistry
from .registry import PluginRegistry
from .store import SqliteModuleStore


class UnsupportedOperation(RuntimeError):
    pass


class _ModuleData:
    def __init__(self, store: SqliteModuleStore, module_id: str) -> None:
        self.store = store
        self.module_id = module_id

    def get(self, key: str) -> str | None:
        return self.store.get(self.module_id, key)

    def set(self, key: str, value: str) -> None:
        self.store.set(self.module_id, key, value)

    def delete_all(self) -> None:
        self.store.delete_data(self.module_id)


class LifecycleManager:
    def __init__(
        self, registry: PluginRegistry, store: SqliteModuleStore, services: ServiceRegistry | None = None
    ) -> None:
        self.registry = registry
        self.store = store
        self.services = services or ServiceRegistry()
        self._locks: dict[str, Any] = {}
        self._graph_lock = asyncio.Lock()

    def states(self) -> list[Any]:
        return self.store.states()

    async def install(self, module_id: str) -> None:
        async with self._graph_lock:
            self._validate_install(module_id)
            state = self.store.state(module_id)
            if state is not None and state.status in (
                ModuleStatus.INSTALLED,
                ModuleStatus.ENABLED,
                ModuleStatus.DISABLED,
            ):
                return
            await self._operate(module_id, "install", ModuleStatus.INSTALLED, lambda plugin, ctx: plugin.install(ctx))

    async def configure(self, module_id: str, values: dict[str, Any]) -> None:
        self._require_installed(module_id)
        await self._operate(module_id, "configure", None, lambda plugin, ctx: plugin.configure(ctx, values))

    async def enable(self, module_id: str) -> None:
        async with self._graph_lock:
            self._require_installed(module_id)
            self._validate_enable(module_id)
            await self._operate(module_id, "enable", ModuleStatus.ENABLED, lambda plugin, ctx: plugin.enable(ctx))

    async def disable(self, module_id: str) -> None:
        async with self._graph_lock:
            self._require_installed(module_id)
            self._validate_no_enabled_dependents(module_id, "disable")
            await self._operate(module_id, "disable", ModuleStatus.DISABLED, lambda plugin, ctx: plugin.disable(ctx))

    async def healthcheck(self, module_id: str) -> HealthReport:
        self._require_installed(module_id)
        plugin, context = self._plugin(module_id)
        try:
            result = await plugin.healthcheck(context)
        except NotImplementedError as exc:
            raise UnsupportedOperation(str(exc)) from exc
        self.store.operation(module_id, "healthcheck", "ok" if result.healthy else "failed", result.detail)
        return result

    async def update(self, module_id: str, target_version: str) -> None:
        self._require_installed(module_id)
        if not target_version.strip():
            raise ValueError("target version is required")
        await self._operate(module_id, "update", None, lambda plugin, ctx: plugin.update(ctx, target_version))

    async def backup(self, module_id: str) -> object:
        self._require_installed(module_id)
        plugin, context = self._plugin(module_id)
        operation = getattr(plugin, "backup", None)
        if operation is None:
            raise UnsupportedOperation(f"backup is not supported by {module_id}")
        try:
            result = await operation(context)
        except NotImplementedError as exc:
            self.store.operation(module_id, "backup", "unsupported", str(exc))
            raise UnsupportedOperation(str(exc)) from exc
        except Exception as exc:
            self.store.operation(module_id, "backup", "failed", str(exc))
            raise
        self.store.operation(module_id, "backup", "ok")
        return result

    async def uninstall(self, module_id: str, *, purge_data: bool = False) -> None:
        async with self._graph_lock:
            self._require_installed(module_id)
            self._validate_no_enabled_dependents(module_id, "uninstall")
            await self._operate(
                module_id,
                "uninstall",
                ModuleStatus.UNINSTALLED,
                lambda plugin, ctx: plugin.uninstall(ctx, purge_data=purge_data),
            )

    async def reconcile_runtime(self) -> None:
        """Apply persisted desired state to runtime-only plugin capabilities after a restart."""
        for state in self.store.states():
            if state.status not in (ModuleStatus.INSTALLED, ModuleStatus.ENABLED, ModuleStatus.DISABLED):
                continue
            plugin, context = self._plugin(state.module_id)
            try:
                if state.status is ModuleStatus.ENABLED:
                    await plugin.enable(context)
                else:
                    await plugin.disable(context)
            except Exception as exc:
                self.store.set_state(state.module_id, plugin.manifest.version, ModuleStatus.FAILED, str(exc))
                self.store.operation(state.module_id, "runtime_reconcile", "failed", str(exc))
            else:
                self.store.operation(state.module_id, "runtime_reconcile", "ok")

    async def _operate(
        self,
        module_id: str,
        operation: str,
        success_status: ModuleStatus | None,
        action: Callable[[Any, PluginContext], Awaitable[None]],
    ) -> None:
        lock = self._locks.setdefault(module_id, asyncio.Lock())
        async with lock:
            plugin, context = self._plugin(module_id)
            try:
                await action(plugin, context)
            except NotImplementedError as exc:
                self.store.operation(module_id, operation, "unsupported", str(exc))
                raise UnsupportedOperation(str(exc)) from exc
            except Exception as exc:
                self.store.set_state(module_id, plugin.manifest.version, ModuleStatus.FAILED, str(exc))
                self.store.operation(module_id, operation, "failed", str(exc))
                raise
            if success_status is not None:
                self.store.set_state(module_id, plugin.manifest.version, success_status)
            self.store.operation(module_id, operation, "ok")

    def preview(self, module_id: str, operation: str) -> dict[str, Any]:
        manifest = self.registry.get(module_id).manifest
        dependents = [item.identifier for item in self.registry.available() if module_id in item.depends_on]
        reasons: list[str] = []
        try:
            if operation == "enable":
                self._validate_enable(module_id)
            elif operation in {"disable", "uninstall"}:
                self._validate_no_enabled_dependents(module_id, operation)
            elif operation == "install":
                self._validate_install(module_id)
        except RuntimeError as exc:
            reasons.append(str(exc))
        return {
            "module": module_id,
            "operation": operation,
            "dependencies": list(manifest.depends_on),
            "conflicts": list(manifest.conflicts),
            "dependents": dependents,
            "allowed": not reasons,
            "reasons": reasons,
        }

    def _validate_install(self, module_id: str) -> None:
        manifest = self.registry.get(module_id).manifest
        conflicts = [item for item in manifest.conflicts if self._is_active(item)]
        if conflicts:
            raise RuntimeError(f"conflicting plugins are active: {', '.join(conflicts)}")

    def _validate_enable(self, module_id: str) -> None:
        manifest = self.registry.get(module_id).manifest
        missing = [item for item in manifest.depends_on if not self._is_enabled(item)]
        if missing:
            raise RuntimeError(f"required plugins are not enabled: {', '.join(missing)}")
        conflicts = [item for item in manifest.conflicts if self._is_active(item)]
        if conflicts:
            raise RuntimeError(f"conflicting plugins are active: {', '.join(conflicts)}")

    def _validate_no_enabled_dependents(self, module_id: str, operation: str) -> None:
        dependents = [
            item.identifier
            for item in self.registry.available()
            if module_id in item.depends_on and self._is_enabled(item.identifier)
        ]
        if dependents:
            raise RuntimeError(f"cannot {operation}; enabled plugins depend on it: {', '.join(dependents)}")

    def _is_active(self, module_id: str) -> bool:
        state = self.store.state(module_id)
        return state is not None and state.status in (
            ModuleStatus.INSTALLED,
            ModuleStatus.ENABLED,
            ModuleStatus.DISABLED,
        )

    def _is_enabled(self, module_id: str) -> bool:
        state = self.store.state(module_id)
        return state is not None and state.status is ModuleStatus.ENABLED

    def _plugin(self, module_id: str) -> tuple[Any, PluginContext]:
        plugin = self.registry.get(module_id)
        context = PluginContext(
            module_id=module_id,
            data=_ModuleData(self.store, module_id),
            granted_permissions=frozenset(plugin.manifest.permissions),
            services=self.services,
        )
        return plugin, context

    def _require_installed(self, module_id: str) -> None:
        state = self.store.state(module_id)
        if state is None or state.status in (ModuleStatus.AVAILABLE, ModuleStatus.UNINSTALLED):
            raise RuntimeError(f"plugin is not installed: {module_id}")
