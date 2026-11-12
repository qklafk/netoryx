"""Cross-process ownership for a Core state database on a local filesystem."""

from __future__ import annotations

from pathlib import Path

from filelock import FileLock, Timeout


class OwnershipBusy(RuntimeError):
    pass


def lock_path(state_db: Path) -> Path:
    canonical = canonical_state_path(state_db)
    return canonical.with_suffix(canonical.suffix + ".runtime.lock")


def canonical_state_path(state_db: Path) -> Path:
    """One canonical local path prevents aliases from creating independent locks."""
    return state_db.expanduser().resolve(strict=False)


class CoreOwnership:
    def __init__(self, state_db: Path) -> None:
        self.path = lock_path(state_db)
        self._lock = FileLock(str(self.path))
        self.held = False

    def acquire(self, timeout: float = 0) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._lock.acquire(timeout=timeout)
        except Timeout as exc:
            raise OwnershipBusy(f"Core lifecycle is owned by another process: {self.path}") from exc
        self.held = True

    def release(self) -> None:
        if self.held:
            self._lock.release()
            self.held = False
