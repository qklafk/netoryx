from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from app.core.plugins.ownership import CoreOwnership, OwnershipBusy, lock_path


def _hold_ownership(
    path: str, ready: multiprocessing.synchronize.Event, release: multiprocessing.synchronize.Event
) -> None:
    ownership = CoreOwnership(Path(path))
    ownership.acquire()
    ready.set()
    release.wait(10)
    ownership.release()


def test_canonical_paths_share_one_exclusive_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    relative = Path("state") / ".." / "core.db"
    absolute = tmp_path / "core.db"
    assert lock_path(relative) == lock_path(absolute)
    first = CoreOwnership(relative)
    second = CoreOwnership(absolute)
    first.acquire()
    try:
        with pytest.raises(OwnershipBusy):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_ownership_is_exclusive_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    state_path = tmp_path / "core.db"
    process = context.Process(target=_hold_ownership, args=(str(state_path), ready, release))
    process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(OwnershipBusy):
            CoreOwnership(state_path).acquire()
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0
