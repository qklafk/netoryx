"""Read-only evidence-based detection of an existing Netoryx installation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class InstallationPresence(StrEnum):
    ABSENT = "absent"
    PARTIAL = "partial"
    INTERRUPTED = "interrupted"
    PRESENT = "present"


@dataclass(frozen=True, slots=True)
class InstallationDetection:
    presence: InstallationPresence
    evidence: tuple[str, ...]


def detect_installation(directory: Path) -> InstallationDetection:
    directory = directory.expanduser().resolve(strict=False)
    evidence = tuple(
        name
        for name in (".env", "docker-compose.yml", "data/core.db", ".installer-journal.json", ".installer-state.json")
        if (directory / name).exists()
    )
    if not evidence:
        return InstallationDetection(InstallationPresence.ABSENT, ())
    if ".installer-journal.json" in evidence:
        return InstallationDetection(InstallationPresence.INTERRUPTED, evidence)
    if {".env", "docker-compose.yml", "data/core.db"}.issubset(evidence):
        return InstallationDetection(InstallationPresence.PRESENT, evidence)
    return InstallationDetection(InstallationPresence.PARTIAL, evidence)
