"""Public install settings and local secret references are deliberately separate."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from getpass import getpass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class InstallationConfiguration:
    domain: str | None = None
    installation_directory: str = "/opt/netoryx"
    database_url: str | None = None
    module_settings: dict[str, dict[str, str]] = field(default_factory=dict)
    secret_references: dict[str, str] = field(default_factory=dict)


class LocalSecretProvider:
    """A minimal provider: values never leave the private 0600 JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
            raise ValueError("invalid secret store")
        return data

    def write_missing(self, values: dict[str, str]) -> None:
        current = self.read()
        current.update({key: value for key, value in values.items() if key not in current})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                json.dump(current, target, sort_keys=True)
                target.write("\n")
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def replace(self, key: str, value: str) -> None:
        """Explicit overwrite path; normal creation intentionally cannot replace values."""
        current = self.read()
        current[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                json.dump(current, target, sort_keys=True)
                target.write("\n")
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def prompt_secret(key: str, *, replace: bool = False, provider: LocalSecretProvider) -> None:
    """Hidden interactive creation; unsuitable terminals fail rather than echoing a secret."""
    if key in provider.read() and not replace:
        raise ValueError(f"secret already exists: {key}; use explicit replace")
    first = getpass(f"Enter {key}: ")
    confirmation = getpass(f"Confirm {key}: ")
    if not first or first != confirmation:
        raise ValueError("secret confirmation failed")
    if replace:
        provider.replace(key, first)
    else:
        provider.write_missing({key: first})


def save_public_configuration(path: Path, configuration: InstallationConfiguration) -> None:
    payload = asdict(configuration)
    for value in payload["module_settings"].values():
        if any(any(marker in key.lower() for marker in ("secret", "password", "token", "key")) for key in value):
            raise ValueError("secret values belong in LocalSecretProvider")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
