"""SQLite transaction modes: writers take the write lock up front, read-only sessions do not."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db.models import Base, KeyValue
from app.db.session import SessionFactory, create_engine, create_session_factory, read_session

WRITER_HOLD_SEC = 1.5


@pytest.fixture
async def file_factory(tmp_path: Path) -> AsyncIterator[SessionFactory]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'locks.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def _hold_write_lock(factory: SessionFactory, started: asyncio.Event) -> None:
    async with factory.begin() as s:
        s.add(KeyValue(key="held", value="1"))
        await s.flush()
        started.set()
        await asyncio.sleep(WRITER_HOLD_SEC)


async def _timed(coro_factory: object) -> float:
    started = time.perf_counter()
    await coro_factory()  # type: ignore[operator]
    return time.perf_counter() - started


async def test_read_session_does_not_queue_behind_a_writer(file_factory: SessionFactory) -> None:
    async with file_factory.begin() as s:
        s.add(KeyValue(key="committed", value="1"))
    started = asyncio.Event()
    writer = asyncio.create_task(_hold_write_lock(file_factory, started))
    await started.wait()

    async def read() -> None:
        async with read_session(file_factory) as s:
            # WAL snapshot: sees committed rows, not the writer's uncommitted one.
            assert await s.scalar(select(func.count()).select_from(KeyValue)) == 1

    elapsed = await _timed(read)
    await writer
    assert elapsed < WRITER_HOLD_SEC / 2, f"read waited {elapsed:.2f}s for the writer"


async def test_regular_sessions_still_take_the_write_lock_up_front(file_factory: SessionFactory) -> None:
    started = asyncio.Event()
    writer = asyncio.create_task(_hold_write_lock(file_factory, started))
    await started.wait()

    async def write() -> None:
        async with file_factory.begin() as s:
            await s.scalar(select(func.count()).select_from(KeyValue))
            s.add(KeyValue(key="second", value="2"))

    elapsed = await _timed(write)
    await writer
    assert elapsed >= WRITER_HOLD_SEC / 2  # serialized behind the writer instead of failing on upgrade
    async with read_session(file_factory) as s:
        assert await s.scalar(select(func.count()).select_from(KeyValue)) == 2


async def test_read_session_shares_the_pool_and_sees_new_commits(file_factory: SessionFactory) -> None:
    async with read_session(file_factory) as s:
        assert await s.scalar(select(func.count()).select_from(KeyValue)) == 0
    async with file_factory.begin() as s:
        s.add(KeyValue(key="k", value="v"))
    async with read_session(file_factory) as s:
        assert (await s.get(KeyValue, "k")) is not None
    assert read_session(file_factory).bind.pool is file_factory.kw["bind"].pool  # type: ignore[union-attr]
