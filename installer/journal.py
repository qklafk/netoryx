"""Durable, secret-free installation journal and checkpoints."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


class JournalError(RuntimeError):
    """A journal cannot safely be read or continued."""


JOURNAL_SCHEMA_VERSION = "1"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"
    ROLLBACK_REQUIRED = "rollback_required"
    ROLLED_BACK = "rolled_back"
    UNAVAILABLE = "unavailable"


@dataclass(slots=True)
class JournalStep:
    identifier: str
    status: StepStatus = StepStatus.PENDING
    error: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass(slots=True)
class InstallationJournal:
    installation_id: str
    installation_version: str
    profile: str
    role: str
    modules: tuple[str, ...]
    plan_fingerprint: str
    steps: list[JournalStep]
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_error: str | None = None
    recovery_status: str = "not-required"
    schema_version: str = JOURNAL_SCHEMA_VERSION

    def step(self, identifier: str) -> JournalStep:
        for step in self.steps:
            if step.identifier == identifier:
                return step
        raise JournalError(f"unknown journal step: {identifier}")

    def mark(self, identifier: str, status: StepStatus, error: str | None = None) -> None:
        step = self.step(identifier)
        step.status, step.error = status, error
        step.updated_at = datetime.now(UTC).isoformat()
        self.updated_at = step.updated_at
        self.last_error = error
        if status is StepStatus.RUNNING:
            self.recovery_status = "required"
        elif status is StepStatus.SUCCEEDED and all(item.status is StepStatus.SUCCEEDED for item in self.steps):
            self.recovery_status = "complete"

    def recover_interrupted(self) -> tuple[JournalStep, ...]:
        """Never assume a process that died while running completed its work."""
        interrupted = tuple(item for item in self.steps if item.status is StepStatus.RUNNING)
        for item in interrupted:
            item.status = StepStatus.INTERRUPTED
            item.updated_at = datetime.now(UTC).isoformat()
        if interrupted:
            self.recovery_status = "required"
            self.updated_at = datetime.now(UTC).isoformat()
        return interrupted


def save_journal(path: Path, journal: InstallationJournal) -> None:
    """Atomically save a validated, intentionally secret-free journal."""
    payload = asdict(journal)
    _reject_secrets(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as destination:
            json.dump(payload, destination, indent=2, sort_keys=True)
            destination.write("\n")
        if os.name != "nt":
            os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def load_journal(path: Path) -> InstallationJournal:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("root is not an object")
        steps = [
            JournalStep(item["identifier"], StepStatus(item["status"]), item.get("error"), item["updated_at"])
            for item in raw["steps"]
        ]
        journal = InstallationJournal(
            installation_id=str(raw["installation_id"]),
            installation_version=str(raw["installation_version"]),
            profile=str(raw["profile"]),
            role=str(raw["role"]),
            modules=tuple(str(item) for item in raw["modules"]),
            plan_fingerprint=str(raw["plan_fingerprint"]),
            steps=steps,
            started_at=str(raw["started_at"]),
            updated_at=str(raw["updated_at"]),
            last_error=raw.get("last_error"),
            recovery_status=str(raw.get("recovery_status", "required")),
            schema_version=str(raw.get("schema_version", JOURNAL_SCHEMA_VERSION)),
        )
        if journal.schema_version != JOURNAL_SCHEMA_VERSION:
            raise JournalError(f"unsupported installation journal schema: {journal.schema_version}")
        return journal
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise JournalError(f"installation journal is corrupted: {path}") from exc


def _reject_secrets(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if any(marker in str(key).lower() for marker in ("secret", "password", "token", "private_key")):
                raise JournalError(f"journal field is not permitted: {key}")
            _reject_secrets(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_secrets(nested)
