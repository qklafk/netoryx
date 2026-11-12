"""Permissions and cross-process locks."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.locks import KvLockBackend, LockManager, job_lock, set_default_manager
from app.core.permissions import (
    ROLE_PERMISSIONS,
    HasPermission,
    IsAdmin,
    Permission,
    has_permission,
    permissions_of,
    resolve_role,
)
from app.db.models import Admin, AdminRole
from app.db.session import SessionFactory
from app.services import kv
from tests.conftest import make_settings


# ---------------------------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------------------------
def test_role_permissions() -> None:
    assert ROLE_PERMISSIONS[AdminRole.OWNER] == frozenset(Permission)
    admin = ROLE_PERMISSIONS[AdminRole.ADMIN]
    assert Permission.SETTINGS in admin and Permission.PANELS in admin
    assert Permission.ADMINS not in admin and Permission.BACKUP_RESTORE not in admin
    assert Permission.SETTINGS_SECRETS not in admin
    assert has_permission(AdminRole.SUPPORT, Permission.TICKETS)
    assert not has_permission(AdminRole.SUPPORT, Permission.USERS_MONEY)
    assert not has_permission(AdminRole.VIEWER, Permission.USERS_EDIT)
    assert has_permission(AdminRole.VIEWER, Permission.VIEW_DASHBOARD)
    assert not has_permission(None, Permission.VIEW_DASHBOARD)
    assert permissions_of(None) == frozenset()
    assert ROLE_PERMISSIONS[AdminRole.VIEWER] < ROLE_PERMISSIONS[AdminRole.SUPPORT] < admin


async def test_resolve_role(session_factory: SessionFactory) -> None:
    settings = make_settings(admin_ids=[1])
    async with session_factory.begin() as s:
        s.add(Admin(tg_id=2, role=AdminRole.SUPPORT))
        s.add(Admin(tg_id=1, role=AdminRole.VIEWER))  # env owners always stay owners
    async with session_factory() as s:
        assert await resolve_role(s, settings, 1) is AdminRole.OWNER
        assert await resolve_role(s, settings, 2) is AdminRole.SUPPORT
        assert await resolve_role(s, settings, 3) is None


async def test_permission_filters() -> None:
    f = HasPermission(Permission.USERS_VIEW, Permission.TICKETS)
    assert await f(object(), admin_role=AdminRole.SUPPORT)
    assert not await f(object(), admin_role=AdminRole.VIEWER)
    assert not await f(object())
    assert "users_view" in str(f)
    with pytest.raises(ValueError):
        HasPermission()
    assert await IsAdmin()(object(), admin_role=AdminRole.VIEWER)
    assert not await IsAdmin()(object(), admin_role=None)


# ---------------------------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_kv_lock_exclusive_until_expiry(session_factory: SessionFactory) -> None:
    clock = Clock()
    a = LockManager(session_factory, owner="a", backend=KvLockBackend(session_factory, clock))
    b = LockManager(session_factory, owner="b", backend=KvLockBackend(session_factory, clock))
    assert await a.acquire("job", 60)
    assert await a.acquire("job", 60)  # a plain acquire by the holder renews it
    clock.now += 50
    assert await a.extend("job", 60)  # the holder extends it
    assert not await b.extend("job", 60)
    clock.now += 50
    assert not await b.acquire("job", 60)
    await b.release("job")  # not the holder: no effect
    assert not await b.acquire("job", 60)
    clock.now += 61
    assert await b.acquire("job", 60)  # expired holder
    await b.release("job")
    async with session_factory() as s:
        assert await kv.get(s, "lock:job") is None
    assert await a.acquire("job", 60)


async def test_job_lock_context(session_factory: SessionFactory) -> None:
    a = LockManager(session_factory, owner="a")
    b = LockManager(session_factory, owner="b")
    async with a.job_lock("x", 30) as got_a:
        assert got_a
        async with b.job_lock("x", 30) as got_b:
            assert not got_b
    async with b.job_lock("x", 30) as got_b:
        assert got_b
    with pytest.raises(ValueError):
        await a.acquire("", 30)
    with pytest.raises(ValueError):
        await a.acquire("x" * 100, 30)
    with pytest.raises(ValueError):
        await a.acquire("x", 0)


async def test_corrupted_lock_row_is_taken_over(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        await kv.set(s, "lock:broken", "garbage")
    assert await LockManager(session_factory).acquire("broken", 10)


async def test_module_level_job_lock(session_factory: SessionFactory) -> None:
    set_default_manager(None)
    with pytest.raises(RuntimeError):
        async with job_lock("x", 10):
            pass
    manager = LockManager(session_factory)
    set_default_manager(manager)
    try:
        async with job_lock("x", 10) as acquired:
            assert acquired
    finally:
        set_default_manager(None)


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, px: int | None = None) -> bool | None:
        if nx and key in self.data:
            return None
        self.data[key] = value
        if px is not None:
            self.ttl[key] = px
        return True

    async def eval(self, script: str, numkeys: int, key: str, owner: str, *args: Any) -> int:
        assert numkeys == 1
        if "pexpire" in script:  # owner-checked extend, or take the lock if it is free
            if self.data.get(key) == owner:
                self.ttl[key] = int(args[0])
                return 1
            return 1 if await self.set(key, owner, nx=True, px=int(args[0])) else 0
        assert "redis.call('del'" in script
        if self.data.get(key) == owner:
            del self.data[key]
            return 1
        return 0


async def test_redis_lock_backend(session_factory: SessionFactory) -> None:
    redis: Any = FakeRedis()
    a = LockManager(session_factory, redis, owner="a")
    b = LockManager(session_factory, redis, owner="b")
    assert await a.acquire("job", 1.5)
    assert redis.ttl["lock:job"] == 1500
    assert not await b.acquire("job", 10)
    await b.release("job")
    assert "lock:job" in redis.data
    await a.release("job")
    assert await b.acquire("job", 10)


async def test_redis_lock_is_extended_by_its_holder_only(session_factory: SessionFactory) -> None:
    redis: Any = FakeRedis()
    a = LockManager(session_factory, redis, owner="a")
    b = LockManager(session_factory, redis, owner="b")
    assert await a.acquire("job", 900)
    assert await a.extend("job", 900) and redis.ttl["lock:job"] == 900_000
    assert not await b.extend("job", 900)
    assert redis.data["lock:job"] == "a"
    del redis.data["lock:job"]  # expired meanwhile: the holder takes it again
    assert await a.extend("job", 60) and redis.data["lock:job"] == "a"


async def test_job_lock_excludes_other_tasks_of_the_same_process(session_factory: SessionFactory) -> None:
    manager = LockManager(session_factory, owner="me")
    async with manager.job_lock("bulk", 60) as first:
        async with manager.job_lock("bulk", 60) as second:
            assert first and not second
        assert not await manager.acquire("bulk", 60)  # still held after the inner block
    async with manager.job_lock("bulk", 60) as again:
        assert again

    entered = asyncio.Event()
    release = asyncio.Event()

    async def worker() -> bool:
        async with manager.job_lock("bulk", 60) as acquired:
            if acquired:
                entered.set()
                await release.wait()
            return acquired

    results = asyncio.gather(worker(), worker())
    await entered.wait()
    release.set()
    assert sorted(await results) == [False, True]


async def test_release_errors_are_swallowed(session_factory: SessionFactory) -> None:
    class Broken:
        async def acquire(self, name: str, owner: str, ttl: float) -> bool:
            return True

        async def extend(self, name: str, owner: str, ttl: float) -> bool:
            return True

        async def release(self, name: str, owner: str) -> None:
            raise ConnectionError("down")

    manager = LockManager(session_factory, backend=Broken())
    async with manager.job_lock("x", 5) as acquired:
        assert acquired
