"""Domain types used by the installation planner."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ProductProfile(StrEnum):
    PERSONAL = "personal"
    COMMERCIAL = "commercial"


class DeploymentRole(StrEnum):
    ALL_IN_ONE = "all-in-one"
    CONTROL_PLANE = "control-plane"
    VPN_NODE = "vpn-node"


class InstallMode(StrEnum):
    QUICK = "quick"
    ADVANCED = "advanced"


@dataclass(frozen=True, slots=True)
class ResourceRequirement:
    ram_mb: int = 0
    disk_mb: int = 0
    cpu_cores: int = 0


@dataclass(frozen=True, slots=True)
class PortRequirement:
    port: int
    protocol: str = "tcp"


@dataclass(frozen=True, slots=True)
class ModuleManifest:
    identifier: str
    version: int
    title: str
    description: str
    roles: frozenset[DeploymentRole]
    profiles: frozenset[ProductProfile]
    depends_on: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    ports: tuple[PortRequirement, ...] = ()
    resources: ResourceRequirement = ResourceRequirement()
    requires_https: bool = False
    requires_bot: bool = False
    manifest_schema: int = 1
    sdk_compatibility: str = ">=1,<2"
    core_compatibility: str = ">=1,<2"
    permissions: tuple[str, ...] = ()
    entrypoint: str | None = None


@dataclass(frozen=True, slots=True)
class InstallRequest:
    profile: ProductProfile
    role: DeploymentRole
    modules: tuple[str, ...]
    public_https: bool = False


@dataclass(frozen=True, slots=True)
class MachineFacts:
    operating_system: str
    os_version: str | None
    cpu_cores: int
    ram_mb: int
    disk_mb: int
    occupied_ports: frozenset[PortRequirement] = frozenset()


@dataclass(slots=True)
class Plan:
    request: InstallRequest
    modules: list[ModuleManifest] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    required_resources: ResourceRequirement = ResourceRequirement()

    @property
    def valid(self) -> bool:
        return not self.errors
