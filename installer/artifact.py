"""Non-secret reviewed plan artifact used to bind plan to a later apply."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .engine import INSTALLER_VERSION, ExecutionEngine
from .models import Plan

PLAN_SCHEMA_VERSION = "1"


@dataclass(frozen=True, slots=True)
class PlanArtifact:
    schema_version: str
    installer_version: str
    created_at: str
    profile: str
    role: str
    modules: tuple[dict[str, object], ...]
    fingerprint: str


def create_artifact(plan: Plan, engine: ExecutionEngine) -> PlanArtifact:
    return PlanArtifact(
        PLAN_SCHEMA_VERSION,
        INSTALLER_VERSION,
        datetime.now(UTC).isoformat(),
        plan.request.profile.value,
        plan.request.role.value,
        tuple(
            {
                "id": item.identifier,
                "version": item.version,
                "depends_on": sorted(item.depends_on),
            }
            for item in plan.modules
        ),
        engine.fingerprint(plan),
    )


def save_artifact(path: Path, artifact: PlanArtifact, *, overwrite: bool = False) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"plan artifact exists: {path}")
    payload = asdict(artifact)
    _reject_secret_fields(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(payload, target, indent=2, sort_keys=True)
            target.write("\n")
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_artifact(path: Path) -> PlanArtifact:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        artifact = PlanArtifact(
            str(raw["schema_version"]),
            str(raw["installer_version"]),
            str(raw["created_at"]),
            str(raw["profile"]),
            str(raw["role"]),
            tuple(raw["modules"]),
            str(raw["fingerprint"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid plan artifact: {path}") from exc
    if artifact.schema_version != PLAN_SCHEMA_VERSION or artifact.installer_version != INSTALLER_VERSION:
        raise ValueError("unsupported plan artifact version")
    return artifact


def _reject_secret_fields(value: object) -> None:
    if isinstance(value, dict):
        if any(any(marker in key.lower() for marker in ("secret", "token", "password", "private")) for key in value):
            raise ValueError("secret fields are forbidden in plan artifacts")
        for nested in value.values():
            _reject_secret_fields(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_secret_fields(nested)
