"""Stable, dependency-light contracts for trusted built-in plugins."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from installer.models import ModuleManifest

SDK_VERSION = "1.0.0"
CORE_VERSION = "1.0.0"


class ModuleStatus(StrEnum):
    AVAILABLE = "available"
    INSTALLED = "installed"
    ENABLED = "enabled"
    DISABLED = "disabled"
    FAILED = "failed"
    UNINSTALLED = "uninstalled"


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    provider: str


@dataclass(frozen=True, slots=True)
class HealthReport:
    healthy: bool
    detail: str


class ModuleData(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete_all(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PluginContext:
    module_id: str
    data: ModuleData
    granted_permissions: frozenset[str]
    services: ServiceRegistry

    def require(self, permission: str) -> None:
        if permission not in self.granted_permissions:
            raise PermissionError(f"{self.module_id} does not have permission {permission}")


class PlatformPlugin(Protocol):
    manifest: ModuleManifest

    async def install(self, context: PluginContext) -> None: ...

    async def configure(self, context: PluginContext, values: dict[str, Any]) -> None: ...

    async def enable(self, context: PluginContext) -> None: ...

    async def disable(self, context: PluginContext) -> None: ...

    async def healthcheck(self, context: PluginContext) -> HealthReport: ...

    async def update(self, context: PluginContext, target_version: str) -> None: ...

    async def uninstall(self, context: PluginContext, *, purge_data: bool) -> None: ...


class ServiceRegistry:
    """Capability registry. It intentionally exposes no arbitrary application runtime."""

    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}
        self._services: dict[str, object] = {}

    def register(self, capability: Capability, service: object | None = None) -> None:
        if capability.name in self._capabilities:
            raise ValueError(f"capability already registered: {capability.name}")
        self._capabilities[capability.name] = capability
        if service is not None:
            self._services[capability.name] = service

    def get(self, name: str) -> Capability | None:
        return self._capabilities.get(name)

    def service(self, name: str) -> object | None:
        return self._services.get(name)
