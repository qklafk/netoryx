"""Atomic persistence for resumable installation planning state."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import InstallRequest


@dataclass(frozen=True, slots=True)
class InstallState:
    request: InstallRequest
    status: str
    created_at: str
    updated_at: str
    completed_steps: tuple[str, ...] = ()

    @classmethod
    def planned(cls, request: InstallRequest) -> InstallState:
        now = datetime.now(UTC).isoformat()
        return cls(request=request, status="planned", created_at=now, updated_at=now)


def save_state(path: Path, state: InstallState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(state)
    payload["request"]["profile"] = state.request.profile.value
    payload["request"]["role"] = state.request.role.value
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(payload, target, indent=2, sort_keys=True)
            target.write("\n")
        if os.name != "nt":
            os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
