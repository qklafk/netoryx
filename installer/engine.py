"""Adapter-driven installation executor; it contains no module shell commands."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from filelock import FileLock, Timeout

from .journal import InstallationJournal, JournalStep, StepStatus, load_journal, save_journal
from .models import Plan

INSTALLER_VERSION = "1"


class ExecutionError(RuntimeError):
    pass


class InstallationBusy(ExecutionError):
    pass


@dataclass(frozen=True, slots=True)
class StepContract:
    identifier: str
    module_id: str | None = None
    rollback_supported: bool = False
    retry_safe: bool = True
    resources: tuple[str, ...] = ()


class DeploymentAdapter(Protocol):
    async def preflight(self) -> None: ...
    async def apply(self) -> None: ...
    async def healthcheck(self) -> None: ...
    async def rollback(self) -> None: ...


class AdapterRegistry:
    def __init__(self, adapters: dict[str, DeploymentAdapter] | None = None) -> None:
        self._adapters = adapters or {}

    def get(self, module_id: str) -> DeploymentAdapter | None:
        return self._adapters.get(module_id)


class ExecutionEngine:
    """Runs registered typed steps with a journal and exclusive install-directory lock."""

    def __init__(self, journal_path: Path, adapters: AdapterRegistry, *, install_directory: Path) -> None:
        self.journal_path = journal_path
        self.adapters = adapters
        self.install_directory = install_directory.expanduser().resolve(strict=False)
        self._lock = FileLock(str(self.install_directory / ".netoryx-installer.lock"))

    def fingerprint(self, plan: Plan, public_configuration: object | None = None) -> str:
        """Canonical, secret-free identity of exactly what the user approved."""
        payload = {
            "installer_version": INSTALLER_VERSION,
            "journal_schema": "1",
            "profile": plan.request.profile.value,
            "role": plan.request.role.value,
            "modules": [
                {"id": item.identifier, "version": item.version, "depends_on": sorted(item.depends_on)}
                for item in sorted(plan.modules, key=lambda item: item.identifier)
            ],
            "public_configuration": _public_configuration(public_configuration),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def create_journal(self, plan: Plan, steps: Sequence[StepContract]) -> InstallationJournal:
        return InstallationJournal(
            str(uuid.uuid4()),
            "1",
            plan.request.profile.value,
            plan.request.role.value,
            tuple(item.identifier for item in plan.modules),
            self.fingerprint(plan),
            [JournalStep(item.identifier) for item in steps],
        )

    async def execute(
        self, plan: Plan, steps: Sequence[StepContract], *, dry_run: bool = False, resume: bool = False
    ) -> InstallationJournal:
        if not plan.valid:
            raise ExecutionError("invalid installation plan: " + "; ".join(plan.errors))
        if dry_run:
            return self.create_journal(plan, steps)
        self.install_directory.mkdir(parents=True, exist_ok=True)
        try:
            self._lock.acquire(timeout=0)
        except Timeout as exc:
            raise InstallationBusy(f"another installer owns {self.install_directory}") from exc
        try:
            journal = self._journal_for(plan, steps, resume)
            for step in steps:
                entry = journal.step(step.identifier)
                if entry.status is StepStatus.SUCCEEDED:
                    continue
                if entry.status is StepStatus.INTERRUPTED and not step.retry_safe:
                    raise ExecutionError(f"step requires reconciliation before resume: {step.identifier}")
                adapter = self.adapters.get(step.module_id) if step.module_id else None
                if step.module_id and adapter is None:
                    journal.mark(step.identifier, StepStatus.UNAVAILABLE, f"no deployment adapter for {step.module_id}")
                    save_journal(self.journal_path, journal)
                    raise ExecutionError(journal.last_error or "deployment adapter unavailable")
                journal.mark(step.identifier, StepStatus.RUNNING)
                save_journal(self.journal_path, journal)
                try:
                    if adapter is not None:
                        await adapter.preflight()
                        await adapter.apply()
                        await adapter.healthcheck()
                except Exception as exc:
                    journal.mark(
                        step.identifier,
                        StepStatus.ROLLBACK_REQUIRED if step.rollback_supported else StepStatus.FAILED,
                        str(exc),
                    )
                    save_journal(self.journal_path, journal)
                    raise ExecutionError(f"step failed: {step.identifier}: {exc}") from exc
                journal.mark(step.identifier, StepStatus.SUCCEEDED)
                save_journal(self.journal_path, journal)
            return journal
        finally:
            self._lock.release()

    def _journal_for(self, plan: Plan, steps: Sequence[StepContract], resume: bool) -> InstallationJournal:
        fingerprint = self.fingerprint(plan)
        if self.journal_path.exists():
            if not resume:
                raise ExecutionError(f"existing installation journal: {self.journal_path}; use resume")
            journal = load_journal(self.journal_path)
            if journal.plan_fingerprint != fingerprint:
                raise ExecutionError("installation plan changed; create and confirm a new plan")
            journal.recover_interrupted()
            save_journal(self.journal_path, journal)
            return journal
        journal = self.create_journal(plan, steps)
        save_journal(self.journal_path, journal)
        return journal


def _public_configuration(configuration: object | None) -> object:
    """Only references/configuration may affect approval; values are never accepted here."""
    if configuration is None:
        return None
    data = getattr(configuration, "__dict__", None)
    if data is None:
        from dataclasses import asdict, is_dataclass

        data = asdict(configuration) if is_dataclass(configuration) else configuration  # type: ignore[arg-type]
    if not isinstance(data, dict):
        raise TypeError("public configuration must be serializable")
    payload: dict[str, Any] = dict(data)
    payload.pop("secret_values", None)
    return payload
