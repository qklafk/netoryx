"""Named locks with expiry, shared between processes (Redis ``SET NX`` or a ``kv`` row).

Used to make sure a background job runs in one process at a time when several replicas or process roles are
deployed. A lock expires after ``ttl`` seconds, so a crashed holder never blocks the others for long; a long job
keeps its lock with :meth:`LockManager.extend`. :meth:`LockManager.job_lock` also excludes other tasks of the same
process (the owner token is per process, so the backends alone cannot tell them apart).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import delete, select

from app.db.models import KeyValue
from app.services import kv

if TYPE_CHECKING:
    from app.db.session import SessionFactory

log = logging.getLogger(__name__)

KEY_PREFIX = "lock:"
MAX_NAME_LENGTH = kv.MAX_KEY_LENGTH - len(KEY_PREFIX)

# Deletes the key only if it still holds our token (atomic compare-and-delete).
_REDIS_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

# Extends the key if it holds our token, or takes it if it is free; 1 when we hold it afterwards.
_REDIS_EXTEND = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
if redis.call('set', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
    return 1
end
return 0
"""


def _check_name(name: str) -> None:
    if not name or len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"lock name must be 1..{MAX_NAME_LENGTH} characters")


def default_owner() -> str:
    """A token unique to this process (host, pid and a random part)."""
    return f"{socket.gethostname()[:20]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class LockBackend(Protocol):
    async def acquire(self, name: str, owner: str, ttl: float) -> bool: ...

    async def extend(self, name: str, owner: str, ttl: float) -> bool: ...

    async def release(self, name: str, owner: str) -> None: ...


class KvLockBackend:
    """Locks stored as ``kv`` rows ``lock:<name>`` = ``{"owner": ..., "until": <unix time>}``."""

    def __init__(self, session_factory: SessionFactory, clock: Any = time.time) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def acquire(self, name: str, owner: str, ttl: float) -> bool:
        key = KEY_PREFIX + name
        now = self._clock()
        value = json.dumps({"owner": owner, "until": now + ttl})
        async with self._session_factory.begin() as session:
            # Insert first: a locking read of a missing row takes a gap lock on MySQL and two such readers
            # inserting afterwards deadlock.
            if await kv.add(session, key, value):
                return True
            row = await session.scalar(select(KeyValue).where(KeyValue.key == key).with_for_update())
            if row is None:  # released between the insert attempt and the read; try again next time
                return False
            holder = _parse(row.value)
            if holder.get("owner") != owner and float(holder.get("until", 0)) > now:
                return False
            row.value = value
            return True

    async def extend(self, name: str, owner: str, ttl: float) -> bool:
        # ``acquire`` already renews a row held by the same owner.
        return await self.acquire(name, owner, ttl)

    async def release(self, name: str, owner: str) -> None:
        key = KEY_PREFIX + name
        async with self._session_factory.begin() as session:
            row = await session.scalar(select(KeyValue).where(KeyValue.key == key).with_for_update())
            if row is not None and _parse(row.value).get("owner") == owner:
                await session.execute(
                    delete(KeyValue).where(KeyValue.key == key).execution_options(synchronize_session=False)
                )


class RedisLockBackend:
    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def acquire(self, name: str, owner: str, ttl: float) -> bool:
        ok = await self._redis.set(KEY_PREFIX + name, owner, nx=True, px=_ms(ttl))
        return bool(ok)

    async def extend(self, name: str, owner: str, ttl: float) -> bool:
        return bool(await self._redis.eval(_REDIS_EXTEND, 1, KEY_PREFIX + name, owner, _ms(ttl)))

    async def release(self, name: str, owner: str) -> None:
        await self._redis.eval(_REDIS_RELEASE, 1, KEY_PREFIX + name, owner)


def _ms(ttl: float) -> int:
    return max(1, int(ttl * 1000))


def _parse(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class LockManager:
    """Hands out named locks; Redis when configured, otherwise the database."""

    def __init__(
        self,
        session_factory: SessionFactory,
        redis: Any | None = None,
        *,
        owner: str | None = None,
        backend: LockBackend | None = None,
    ) -> None:
        self.owner = owner or default_owner()
        if backend is not None:
            self._backend: LockBackend = backend
        elif redis is not None:
            self._backend = RedisLockBackend(redis)
        else:
            self._backend = KvLockBackend(session_factory)
        self._held: set[str] = set()  # names inside a job_lock of this process

    async def acquire(self, name: str, ttl: float) -> bool:
        """Take the lock (False if another process, or a job_lock of this process, holds it)."""
        _check_name(name)
        if ttl <= 0:
            raise ValueError("lock ttl must be positive")
        if name in self._held:
            return False
        return await self._backend.acquire(name, self.owner, ttl)

    async def extend(self, name: str, ttl: float) -> bool:
        """Push the expiry of a lock this process holds to ``ttl`` from now (or take it again if it is free).

        False when another process holds it: the caller has lost the lock and should stop.
        """
        _check_name(name)
        if ttl <= 0:
            raise ValueError("lock ttl must be positive")
        return await self._backend.extend(name, self.owner, ttl)

    async def release(self, name: str) -> None:
        _check_name(name)
        try:
            await self._backend.release(name, self.owner)
        except Exception:  # the lock expires on its own; never mask the caller's outcome
            log.exception("failed to release lock %s", name)

    @asynccontextmanager
    async def job_lock(self, name: str, ttl: float) -> AsyncIterator[bool]:
        """``async with locks.job_lock("x", 600) as acquired:`` - skip the work when ``acquired`` is False.

        Exclusive between processes and between tasks of this process.
        """
        _check_name(name)
        if name in self._held:
            yield False
            return
        self._held.add(name)  # before the first await, so a concurrent task of this process sees it
        try:
            acquired = await self._backend_acquire(name, ttl)
        except BaseException:
            self._held.discard(name)
            raise
        if not acquired:
            self._held.discard(name)
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    await self.release(name)
                finally:
                    self._held.discard(name)

    async def _backend_acquire(self, name: str, ttl: float) -> bool:
        if ttl <= 0:
            raise ValueError("lock ttl must be positive")
        return await self._backend.acquire(name, self.owner, ttl)


_default: LockManager | None = None


def set_default_manager(manager: LockManager | None) -> None:
    global _default
    _default = manager


@asynccontextmanager
async def job_lock(name: str, ttl: float, *, manager: LockManager | None = None) -> AsyncIterator[bool]:
    """Module-level shortcut using the process-wide manager configured at startup."""
    target = manager or _default
    if target is None:
        raise RuntimeError("lock manager is not configured")
    async with target.job_lock(name, ttl) as acquired:
        yield acquired
