"""Application core: runtime context, settings, feature modules, permissions and locks."""

import tomllib
from importlib import metadata
from pathlib import Path


def _read_version() -> str:
    """Version from ``pyproject.toml`` (source tree / image), else the installed package metadata."""
    try:
        with (Path(__file__).resolve().parents[2] / "pyproject.toml").open("rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return metadata.version("vpn-bot")
    except metadata.PackageNotFoundError:
        return "unknown"


__version__ = _read_version()
