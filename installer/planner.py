"""Pure planning and validation; this module does not modify a server."""

from __future__ import annotations

from .models import InstallRequest, MachineFacts, ModuleManifest, Plan, PortRequirement, ResourceRequirement
from .registry import ModuleRegistry


def build_plan(registry: ModuleRegistry, request: InstallRequest, facts: MachineFacts | None = None) -> Plan:
    plan = Plan(request=request)
    selected: dict[str, ModuleManifest] = {}

    def add(identifier: str, requested_by: str | None = None) -> None:
        if identifier in selected:
            return
        manifest = registry.get(identifier)
        if manifest is None:
            origin = f" required by {requested_by}" if requested_by else ""
            plan.errors.append(f"unknown module: {identifier}{origin}")
            return
        selected[identifier] = manifest
        for dependency in manifest.depends_on:
            add(dependency, identifier)

    for identifier in request.modules:
        add(identifier)

    for manifest in selected.values():
        if request.role not in manifest.roles:
            plan.errors.append(f"{manifest.identifier} is not available for role {request.role.value}")
        if request.profile not in manifest.profiles:
            plan.errors.append(f"{manifest.identifier} is not compatible with profile {request.profile.value}")
        if manifest.requires_https and not request.public_https:
            plan.errors.append(f"{manifest.identifier} requires a public HTTPS endpoint")
        if manifest.requires_bot and "interface.telegram-bot" not in selected:
            plan.errors.append(f"{manifest.identifier} requires interface.telegram-bot")
        for conflict in manifest.conflicts:
            if conflict in selected:
                plan.errors.append(f"module conflict: {manifest.identifier} and {conflict}")

    port_owners: dict[PortRequirement, list[str]] = {}
    for manifest in selected.values():
        for port in manifest.ports:
            port_owners.setdefault(port, []).append(manifest.identifier)
    for port, owners in port_owners.items():
        if len(owners) > 1:
            plan.errors.append(f"module port conflict: {port.protocol}/{port.port} requested by {', '.join(owners)}")

    plan.modules = sorted(selected.values(), key=lambda item: item.identifier)
    plan.required_resources = ResourceRequirement(
        ram_mb=sum(item.resources.ram_mb for item in plan.modules),
        disk_mb=sum(item.resources.disk_mb for item in plan.modules),
        cpu_cores=max((item.resources.cpu_cores for item in plan.modules), default=0),
    )
    if facts is not None:
        _validate_machine(plan, facts)
    return plan


def _validate_machine(plan: Plan, facts: MachineFacts) -> None:
    required = plan.required_resources
    if facts.ram_mb < required.ram_mb:
        plan.errors.append(f"insufficient RAM: need {required.ram_mb} MB, found {facts.ram_mb} MB")
    if facts.disk_mb < required.disk_mb:
        plan.errors.append(f"insufficient disk: need {required.disk_mb} MB, found {facts.disk_mb} MB")
    if facts.cpu_cores < required.cpu_cores:
        plan.errors.append(f"insufficient CPU: need {required.cpu_cores}, found {facts.cpu_cores}")
    requested_ports = {port for manifest in plan.modules for port in manifest.ports}
    for port in sorted(requested_ports & facts.occupied_ports, key=lambda item: (item.port, item.protocol)):
        plan.errors.append(f"port conflict: {port.protocol}/{port.port} is already occupied")
    if facts.operating_system != "ubuntu":
        plan.warnings.append(
            f"detected {facts.operating_system}; the first supported installation target is Ubuntu 24.04"
        )
