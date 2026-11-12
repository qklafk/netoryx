"""Read-only local system inspection used by the planner."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import socket
from ctypes import Structure, byref, c_ulong, c_ulonglong, sizeof
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .models import MachineFacts, PortRequirement


class CheckStatus(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class Severity(StrEnum):
    REQUIRED = "required"
    RECOMMENDED = "recommended"
    OPTIONAL = "optional"


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    identifier: str
    status: CheckStatus
    severity: Severity
    actual: str
    expected: str
    message: str


def ubuntu_preflight(facts: MachineFacts, *, requires_docker: bool = False) -> tuple[PreflightCheck, ...]:
    checks = [
        PreflightCheck(
            "system.ubuntu-24.04",
            CheckStatus.PASSED
            if (facts.operating_system, facts.os_version) == ("ubuntu", "24.04")
            else CheckStatus.FAILED,
            Severity.REQUIRED,
            f"{facts.operating_system} {facts.os_version}",
            "Ubuntu 24.04",
            "first supported deployment platform",
        ),
        PreflightCheck(
            "system.cpu",
            CheckStatus.PASSED if facts.cpu_cores >= 2 else CheckStatus.FAILED,
            Severity.REQUIRED,
            str(facts.cpu_cores),
            "at least 2 cores",
            "CPU capacity",
        ),
        PreflightCheck(
            "system.ram",
            CheckStatus.PASSED if facts.ram_mb >= 2048 else CheckStatus.FAILED,
            Severity.REQUIRED,
            f"{facts.ram_mb} MB",
            "at least 2048 MB",
            "memory capacity",
        ),
        PreflightCheck(
            "system.disk",
            CheckStatus.PASSED if facts.disk_mb >= 60_000 else CheckStatus.FAILED,
            Severity.REQUIRED,
            f"{facts.disk_mb} MB",
            "at least 60000 MB",
            "free disk capacity",
        ),
    ]
    docker = shutil.which("docker")
    checks.append(
        PreflightCheck(
            "system.docker",
            CheckStatus.PASSED if docker else (CheckStatus.FAILED if requires_docker else CheckStatus.WARNING),
            Severity.REQUIRED if requires_docker else Severity.RECOMMENDED,
            docker or "not installed",
            "Docker Engine",
            "required only by a deployment adapter that uses Docker",
        )
    )
    checks.extend(
        (
            PreflightCheck(
                "system.architecture",
                CheckStatus.PASSED
                if platform.machine().lower() in {"x86_64", "amd64", "aarch64", "arm64"}
                else CheckStatus.FAILED,
                Severity.REQUIRED,
                platform.machine(),
                "x86_64 or arm64",
                "supported CPU architecture",
            ),
            PreflightCheck(
                "system.apt",
                CheckStatus.PASSED if shutil.which("apt-get") else CheckStatus.UNAVAILABLE,
                Severity.REQUIRED,
                shutil.which("apt-get") or "missing",
                "apt-get",
                "Ubuntu package manager",
            ),
            PreflightCheck(
                "system.systemd",
                CheckStatus.PASSED if Path("/run/systemd/system").exists() else CheckStatus.UNAVAILABLE,
                Severity.RECOMMENDED,
                "present" if Path("/run/systemd/system").exists() else "missing",
                "systemd",
                "service manager",
            ),
            PreflightCheck(
                "system.root",
                CheckStatus.PASSED if (hasattr(os, "geteuid") and os.geteuid() == 0) else CheckStatus.WARNING,
                Severity.REQUIRED,
                "root" if hasattr(os, "geteuid") and os.geteuid() == 0 else "not root",
                "root or sudo",
                "required before a future system adapter applies changes",
            ),
            PreflightCheck(
                "system.docker-compose",
                CheckStatus.PASSED if shutil.which("docker") else CheckStatus.UNAVAILABLE,
                Severity.RECOMMENDED,
                "not checked" if docker else "Docker missing",
                "docker compose plugin",
                "only required by Docker adapters",
            ),
        )
    )
    return tuple(checks)


def collect_machine_facts(
    path: Path | None = None, *, required_ports: set[PortRequirement] | None = None
) -> MachineFacts:
    target = path or Path.cwd()
    system = platform.system().lower()
    os_name, version = _linux_distribution() if system == "linux" else (system, platform.release())
    ram_mb = _memory_mb()
    disk_mb = shutil.disk_usage(target).free // (1024 * 1024)
    return MachineFacts(
        operating_system=os_name,
        os_version=version,
        cpu_cores=os.cpu_count() or 1,
        ram_mb=ram_mb,
        disk_mb=disk_mb,
        occupied_ports=_occupied_ports(required_ports or set()),
    )


def _linux_distribution() -> tuple[str, str | None]:
    release = Path("/etc/os-release")
    if not release.is_file():
        return "linux", platform.release()
    values: dict[str, str] = {}
    for line in release.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip('"')
    return values.get("ID", "linux").lower(), values.get("VERSION_ID")


def _memory_mb() -> int:
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    if os.name == "nt":

        class MemoryStatus(Structure):
            _fields_ = [
                ("length", c_ulong),
                ("memory_load", c_ulong),
                ("total_physical", c_ulonglong),
                ("available_physical", c_ulonglong),
                ("total_page_file", c_ulonglong),
                ("available_page_file", c_ulonglong),
                ("total_virtual", c_ulonglong),
                ("available_virtual", c_ulonglong),
                ("available_extended_virtual", c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = sizeof(status)
        windll = getattr(ctypes, "windll")  # noqa: B009 - only exists on Windows, keeps type checks portable
        if windll.kernel32.GlobalMemoryStatusEx(byref(status)):
            return int(status.total_physical // (1024 * 1024))
    return 0


def _occupied_ports(required_ports: set[PortRequirement]) -> frozenset[PortRequirement]:
    unavailable: set[PortRequirement] = set()
    for requirement in required_ports:
        kind = socket.SOCK_STREAM if requirement.protocol == "tcp" else socket.SOCK_DGRAM
        with socket.socket(socket.AF_INET, kind) as probe:
            try:
                probe.bind(("0.0.0.0", requirement.port))
            except OSError:
                unavailable.add(requirement)
    return frozenset(unavailable)
