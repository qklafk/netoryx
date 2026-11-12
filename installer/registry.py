"""Loading and validation of declarative installation manifests."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .models import DeploymentRole, ModuleManifest, PortRequirement, ProductProfile, ResourceRequirement


class ManifestError(ValueError):
    """A manifest is syntactically valid TOML but semantically invalid."""


class ModuleRegistry:
    def __init__(self, manifests: dict[str, ModuleManifest]) -> None:
        self._manifests = manifests

    @classmethod
    def from_directory(cls, directory: Path) -> ModuleRegistry:
        manifests: dict[str, ModuleManifest] = {}
        for path in sorted(directory.glob("*.toml")):
            with path.open("rb") as source:
                data = tomllib.load(source)
            manifest = _parse_manifest(data, path)
            if manifest.identifier in manifests:
                raise ManifestError(f"duplicate module identifier: {manifest.identifier}")
            manifests[manifest.identifier] = manifest
        registry = cls(manifests)
        registry.validate_references()
        return registry

    def get(self, identifier: str) -> ModuleManifest | None:
        return self._manifests.get(identifier)

    def all(self) -> tuple[ModuleManifest, ...]:
        return tuple(self._manifests.values())

    def validate_references(self) -> None:
        for manifest in self._manifests.values():
            for dependency in manifest.depends_on:
                if dependency not in self._manifests:
                    raise ManifestError(f"{manifest.identifier}: unknown dependency {dependency}")
            for conflict in manifest.conflicts:
                if conflict not in self._manifests:
                    raise ManifestError(f"{manifest.identifier}: unknown conflict {conflict}")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(identifier: str) -> None:
            if identifier in visiting:
                raise ManifestError(f"cyclic dependency involving {identifier}")
            if identifier in visited:
                return
            visiting.add(identifier)
            for dependency in self._manifests[identifier].depends_on:
                visit(dependency)
            visiting.remove(identifier)
            visited.add(identifier)

        for identifier in self._manifests:
            visit(identifier)


def builtin_registry() -> ModuleRegistry:
    return ModuleRegistry.from_directory(Path(__file__).with_name("manifests"))


def _enum_set(
    values: object, enum_type: type[DeploymentRole] | type[ProductProfile], path: Path, field: str
) -> frozenset:
    if not isinstance(values, list) or not values:
        raise ManifestError(f"{path.name}: {field} must be a non-empty array")
    try:
        return frozenset(enum_type(str(value)) for value in values)
    except ValueError as exc:
        raise ManifestError(f"{path.name}: invalid {field}") from exc


def _parse_manifest(data: dict[str, Any], path: Path) -> ModuleManifest:
    required = ("id", "version", "title", "description", "roles", "profiles")
    missing = [key for key in required if key not in data]
    if missing:
        raise ManifestError(f"{path.name}: missing {', '.join(missing)}")
    try:
        resources: Any = data.get("resources", {})
        if not isinstance(resources, dict):
            raise TypeError
        ports: Any = data.get("ports", [])
        if not isinstance(ports, list):
            raise TypeError
        parsed_ports = tuple(
            PortRequirement(port=int(item["port"]), protocol=str(item.get("protocol", "tcp")).lower())
            for item in ports
            if isinstance(item, dict)
        )
        if len(parsed_ports) != len(ports) or any(not 1 <= item.port <= 65535 for item in parsed_ports):
            raise ValueError
        return ModuleManifest(
            identifier=str(data["id"]),
            version=int(data["version"]),
            title=str(data["title"]),
            description=str(data["description"]),
            roles=_enum_set(data["roles"], DeploymentRole, path, "roles"),
            profiles=_enum_set(data["profiles"], ProductProfile, path, "profiles"),
            depends_on=tuple(str(value) for value in data.get("depends_on", [])),
            conflicts=tuple(str(value) for value in data.get("conflicts", [])),
            ports=parsed_ports,
            resources=ResourceRequirement(
                ram_mb=max(0, int(resources.get("ram_mb", 0))),
                disk_mb=max(0, int(resources.get("disk_mb", 0))),
                cpu_cores=max(0, int(resources.get("cpu_cores", 0))),
            ),
            requires_https=bool(data.get("requires_https", False)),
            requires_bot=bool(data.get("requires_bot", False)),
            manifest_schema=int(data.get("manifest_schema", 1)),
            sdk_compatibility=str(data.get("sdk_compatibility", ">=1,<2")),
            core_compatibility=str(data.get("core_compatibility", ">=1,<2")),
            permissions=tuple(str(value) for value in data.get("permissions", [])),
            entrypoint=str(data["entrypoint"]) if data.get("entrypoint") else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError(f"{path.name}: invalid manifest fields") from exc
