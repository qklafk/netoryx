"""Building blocks of the middlewares: rate limiters, caches, update helpers, error helpers, filters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram.types import Chat

from app.core.filters import IsPrivate, RoleAtLeast
from app.db.models import AdminRole, Invite
from app.middlewares.access import invite_payload, invite_usable
from app.middlewares.common import TTLCache, command_of, is_open_command, is_payment_update, strip_html
from app.middlewares.errors import error_signature, mask_secrets
from app.middlewares.replay import StartReplayer, synthetic_start
from app.middlewares.throttling import MemoryRateLimiter, RedisRateLimiter, make_limiter
from tests.test_middlewares_support import (
    callback_update,
    message_update,
    pre_checkout_update,
    successful_payment_update,
    tg_user,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- memory token bucket ----------------------------------------------------------------------------------------------
def test_memory_bucket_allows_burst_then_refills() -> None:
    clock = Clock()
    limiter = MemoryRateLimiter(clock=clock)
    decisions = [limiter.hit_sync(1, 3, 6.0) for _ in range(5)]
    assert [d.allowed for d in decisions] == [True, True, True, False, False]
    assert [d.warn for d in decisions] == [False, False, False, True, False]
    clock.now += 2.0  # 3 tokens per 6 s -> one token back
    assert limiter.hit_sync(1, 3, 6.0).allowed
    assert not limiter.hit_sync(1, 3, 6.0).allowed
    assert limiter.hit_sync(2, 3, 6.0).allowed  # buckets are per user


def test_memory_bucket_warns_again_in_the_next_period() -> None:
    clock = Clock()
    limiter = MemoryRateLimiter(clock=clock)
    limiter.hit_sync(1, 1, 10.0)
    assert limiter.hit_sync(1, 1, 10.0).warn
    clock.now += 5.0
    assert not limiter.hit_sync(1, 1, 10.0).warn
    clock.now += 6.0  # refilled one token
    assert limiter.hit_sync(1, 1, 10.0).allowed
    assert limiter.hit_sync(1, 1, 10.0).warn


def test_memory_buckets_are_pruned() -> None:
    clock = Clock()
    limiter = MemoryRateLimiter(max_buckets=10, clock=clock)
    for uid in range(10):
        limiter.hit_sync(uid, 5, 10.0)
    clock.now += 20.0
    limiter.hit_sync(100, 5, 10.0)  # full: idle buckets are dropped
    assert len(limiter) == 1
    for uid in range(30):  # a burst of distinct active users never grows past the cap
        limiter.hit_sync(1000 + uid, 5, 10.0)
    assert len(limiter) <= 10


class FakeRedis:
    def __init__(self, allowed: list[int] | None = None, fail: bool = False) -> None:
        self.allowed = allowed or []
        self.fail = fail
        self.warn_keys: set[str] = set()
        self.script_calls: list[dict[str, Any]] = []

    def register_script(self, source: str) -> Any:
        assert "redis.call('TIME')" in source

        async def run(keys: list[str], args: list[Any]) -> int:
            if self.fail:
                raise ConnectionError("redis down")
            self.script_calls.append({"keys": keys, "args": args})
            return self.allowed.pop(0)

        return run

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if key in self.warn_keys:
            return None
        self.warn_keys.add(key)
        return True


async def test_redis_limiter_uses_the_script_and_warns_once() -> None:
    redis = FakeRedis(allowed=[1, 0, 0])
    limiter = RedisRateLimiter(redis)
    assert (await limiter.hit(7, 5, 10.0)).allowed
    first, second = await limiter.hit(7, 5, 10.0), await limiter.hit(7, 5, 10.0)
    assert (first.allowed, first.warn, second.warn) == (False, True, False)
    assert redis.script_calls[0] == {"keys": ["throttle:7"], "args": [5, 0.5, 11]}


async def test_redis_limiter_falls_back_to_memory() -> None:
    limiter = RedisRateLimiter(FakeRedis(fail=True))
    assert [(await limiter.hit(7, 1, 10.0)).allowed for _ in range(2)] == [True, False]


def test_make_limiter_picks_backend() -> None:
    assert isinstance(make_limiter(None), MemoryRateLimiter)
    assert isinstance(make_limiter(FakeRedis()), RedisRateLimiter)


# --- TTL cache --------------------------------------------------------------------------------------------------------
def test_ttl_cache_expiry_and_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr("app.middlewares.common.time.monotonic", clock)
    cache: TTLCache[int, str] = TTLCache(max_size=3)
    cache.set(1, "a", 10)
    cache.set(2, "b", 20)
    assert cache.get(1) == "a" and 1 in cache
    clock.now += 15
    assert cache.get(1) is None and 1 not in cache and cache.get(2) == "b"
    for key in range(10, 15):
        cache.set(key, "x", 100)
    assert len(cache) <= 3
    assert cache.pop(14) == "x" and cache.pop(14) is None


# --- update helpers ---------------------------------------------------------------------------------------------------
def test_update_classification() -> None:
    assert is_payment_update(pre_checkout_update(1)) and is_payment_update(successful_payment_update(1))
    assert not is_payment_update(message_update(1)) and not is_payment_update(callback_update(1, "x"))
    assert command_of(message_update(1, "/Start@test_bot ref_X ")) == ("start", "ref_X")
    assert command_of(message_update(1, "hello")) is None
    assert is_open_command(message_update(1, "/terms")) and not is_open_command(message_update(1, "/start"))
    assert strip_html("<b>a &amp; b</b>") == "a & b"


def test_invite_helpers() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert invite_payload(message_update(1, "/start inv_ABC")) == "inv_ABC"
    assert invite_payload(message_update(1, "/start ref_ABC")) is None
    assert invite_payload(message_update(1, "inv_ABC")) is None
    ok = Invite(code="A", created_by=1, max_uses=2, uses=1, is_active=True, expires_at=None)
    assert invite_usable(ok, now)
    assert not invite_usable(Invite(code="B", created_by=1, max_uses=1, uses=1, is_active=True), now)
    assert invite_usable(Invite(code="C", created_by=1, max_uses=0, uses=99, is_active=True), now)
    assert not invite_usable(Invite(code="D", created_by=1, max_uses=0, uses=0, is_active=False), now)
    expired = Invite(code="E", created_by=1, max_uses=0, uses=0, is_active=True, expires_at=now - timedelta(1))
    assert not invite_usable(expired, now)


def test_replayer_keeps_only_start_updates() -> None:
    replayer = StartReplayer()
    replayer.remember(1, message_update(1, "hello"))
    assert not replayer.has_pending(1)
    replayer.remember(1, message_update(1, "/start promo_X"))
    assert replayer.has_pending(1)
    start = synthetic_start(tg_user(5), 5, 77)
    assert start.update_id == 77 and start.message is not None and start.message.text == "/start"


# --- errors -----------------------------------------------------------------------------------------------------------
def test_error_signature_and_masking() -> None:
    try:
        raise KeyError("x")
    except KeyError as exc:
        signature = error_signature(exc)
    assert signature.startswith("KeyError@") and "test_middlewares_units.py:" in signature
    assert error_signature(ValueError()) == "ValueError"
    text = "token 123456789:AAEhBP0av28Al0uB0cdF2xwqw9EA1aaaaaa url https://admin:hunter2@panel.example.com/x"
    masked = mask_secrets(text)
    assert "AAEh" not in masked and "hunter2" not in masked and "panel.example.com/x" in masked


# --- filters ----------------------------------------------------------------------------------------------------------
async def test_role_and_private_filters() -> None:
    support = RoleAtLeast(AdminRole.SUPPORT)
    assert await support(object(), admin_role=AdminRole.ADMIN)
    assert await support(object(), admin_role=AdminRole.SUPPORT)
    assert not await support(object(), admin_role=AdminRole.VIEWER)
    assert not await support(object(), admin_role=None)
    assert "support" in str(support)
    private = IsPrivate()
    assert await private(object(), event_chat=Chat(id=1, type="private"))
    assert await private(object(), event_chat=None)
    assert not await private(object(), event_chat=Chat(id=-1, type="supergroup"))
