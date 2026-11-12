"""The running version and the "new release available" check (GitHub releases API, cached)."""

from __future__ import annotations

import logging
import re
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from importlib import metadata
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

PACKAGE = "vpn-bot"
PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"
RELEASES_URL = "https://api.github.com/repos/{repo}/releases/latest"
CACHE_TTL_SEC = 6 * 3600
FAILURE_TTL_SEC = 15 * 60
REQUEST_TIMEOUT_SEC = 10.0
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_NUMBERS = re.compile(r"\d+")


@cache
def current_version() -> str:
    """Version of this installation (package metadata, else ``pyproject.toml``)."""
    try:
        return metadata.version(PACKAGE)
    except metadata.PackageNotFoundError:
        pass
    try:
        with PYPROJECT.open("rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "unknown"


def version_key(version: str) -> tuple[int, ...]:
    """``"v1.10.2"`` -> ``(1, 10, 2)`` for comparisons (pre-release suffixes are ignored)."""
    core = version.strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    return tuple(int(n) for n in _NUMBERS.findall(core))


def is_newer(latest: str, current: str) -> bool:
    latest_key, current_key = version_key(latest), version_key(current)
    return bool(latest_key) and bool(current_key) and latest_key > current_key


@dataclass(frozen=True, slots=True)
class Release:
    version: str | None
    url: str | None
    checked_at: datetime
    error: str | None = None


_cache: dict[str, tuple[float, Release]] = {}


def clear_cache() -> None:
    _cache.clear()


async def latest_release(http: Any, repo: str | None, *, force: bool = False) -> Release | None:
    """The latest published release of ``owner/name`` (cached); None when no repository is configured."""
    if not repo or not _REPO_RE.match(repo):
        return None
    now = time.monotonic()
    cached = _cache.get(repo)
    if cached is not None and not force:
        expires, release = cached
        if now < expires:
            return release
    release = await _fetch(http, repo)
    _cache[repo] = (now + (FAILURE_TTL_SEC if release.error else CACHE_TTL_SEC), release)
    return release


async def _fetch(http: Any, repo: str) -> Release:
    checked = datetime.now(UTC)
    if http is None:
        return Release(None, None, checked, "no HTTP client")
    try:
        async with http.get(
            RELEASES_URL.format(repo=repo),
            headers={"Accept": "application/vnd.github+json", "User-Agent": f"{PACKAGE}/{current_version()}"},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC),
        ) as resp:
            if resp.status == 404:
                return Release(None, None, checked, "no releases published yet")
            if resp.status != 200:
                return Release(None, None, checked, f"GitHub answered {resp.status}")
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        log.info("release check failed: %s", exc)
        return Release(None, None, checked, f"{type(exc).__name__}: {exc}"[:200])
    if not isinstance(data, dict):
        return Release(None, None, checked, "unexpected answer")
    tag = data.get("tag_name") or data.get("name")
    url = data.get("html_url")
    return Release(str(tag) if tag else None, str(url) if url else None, checked)
