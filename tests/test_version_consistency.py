"""The product version is declared in pyproject.toml and web/package.json and must agree."""

import json
import re
import tomllib
from pathlib import Path

from app.core import __version__

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    return str(tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])


def _canonical(version: str) -> str:
    """Normalise ``0.9.0-beta.0`` (semver) and ``0.9.0b0`` (PEP 440) to one form."""
    return re.sub(r"[-.]?(alpha|a)[-.]?(\d*)", r"a\2", re.sub(r"[-.]?(beta|b)[-.]?(\d*)", r"b\2", version)).replace(
        "b.", "b"
    )


def test_web_package_version_matches_pyproject() -> None:
    web = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))["version"]
    assert _canonical(web) == _canonical(_pyproject_version())


def test_web_lockfile_version_matches_package() -> None:
    package = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))["version"]
    lock = json.loads((ROOT / "web" / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["version"] == package
    assert lock["packages"][""]["version"] == package


def test_runtime_version_is_pyproject_version() -> None:
    assert __version__ == _pyproject_version()
