"""Registry for trusted built-in plugins and their shared installer manifests."""

from __future__ import annotations

from collections.abc import Callable

from installer.models import ModuleManifest
from installer.registry import ModuleRegistry, builtin_registry

from .contracts import CORE_VERSION, SDK_VERSION, PlatformPlugin

PluginFactory = Callable[[ModuleManifest], PlatformPlugin]


class PluginRegistry:
    def __init__(self, manifests: ModuleRegistry, factories: dict[str, PluginFactory]) -> None:
        self.manifests = manifests
        self.factories = factories
        self._validate()

    def _validate(self) -> None:
        for identifier in self.factories:
            manifest = self.manifests.get(identifier)
            if manifest is None:
                raise ValueError(f"plugin factory has no manifest: {identifier}")
            if not _compatible(manifest.sdk_compatibility, SDK_VERSION):
                raise ValueError(f"{identifier} is incompatible with SDK {SDK_VERSION}")
            if not _compatible(manifest.core_compatibility, CORE_VERSION):
                raise ValueError(f"{identifier} is incompatible with Core {CORE_VERSION}")
            if manifest.manifest_schema != 1:
                raise ValueError(f"{identifier} uses unsupported manifest schema {manifest.manifest_schema}")

    def available(self) -> tuple[ModuleManifest, ...]:
        return tuple(item for item in self.manifests.all() if item.identifier in self.factories)

    def get(self, identifier: str) -> PlatformPlugin:
        manifest = self.manifests.get(identifier)
        factory = self.factories.get(identifier)
        if manifest is None or factory is None:
            raise LookupError(f"trusted plugin is unavailable: {identifier}")
        return factory(manifest)


def _compatible(spec: str, version: str) -> bool:
    major = int(version.split(".", 1)[0])
    for requirement in (item.strip() for item in spec.split(",") if item.strip()):
        if requirement.startswith(">=") and major < int(requirement[2:].split(".", 1)[0]):
            return False
        if requirement.startswith("<") and major >= int(requirement[1:].split(".", 1)[0]):
            return False
    return True


def builtin_plugin_registry() -> PluginRegistry:
    from .backups import BackupsPlugin
    from .builtin import DemoPlugin

    return PluginRegistry(builtin_registry(), {"system.demo": DemoPlugin, "infrastructure.backups": BackupsPlugin})
