"""Minimal Core runtime with no application, Telegram or business-module dependencies."""

from __future__ import annotations

from pathlib import Path

from .contracts import ServiceRegistry
from .lifecycle import LifecycleManager
from .ownership import canonical_state_path
from .registry import PluginRegistry, builtin_plugin_registry
from .store import SqliteModuleStore


class CoreRuntime:
    def __init__(
        self,
        state_path: Path,
        registry: PluginRegistry | None = None,
        services: ServiceRegistry | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        self.state_path = canonical_state_path(state_path)
        self.store = SqliteModuleStore(self.state_path, read_only=read_only)
        self.registry = registry or builtin_plugin_registry()
        self.lifecycle = LifecycleManager(self.registry, self.store, services)

    def close(self) -> None:
        self.store.close()

    async def reconcile_runtime(self) -> None:
        await self.lifecycle.reconcile_runtime()

    def status(self) -> dict[str, object]:
        return {
            "available": [manifest.identifier for manifest in self.registry.available()],
            "installed": [
                {"id": state.module_id, "version": state.version, "status": state.status.value}
                for state in self.lifecycle.states()
            ],
        }
