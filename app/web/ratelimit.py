"""Fixed-window rate limiting: in-memory (single process) or Redis (shared between replicas)."""

from __future__ import annotations

import logging
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RateResult:
    allowed: bool
    remaining: int
    retry_after: int  # seconds until the window resets (0 when allowed)


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_sec: int) -> RateResult:
        """Count one event for ``key``; ``allowed`` is False once ``limit`` events happened in the window."""
        ...

    async def reset(self, key: str) -> None:
        """Forget the counter of ``key``."""
        ...


class MemoryRateLimiter:
    """Per-process fixed-window counters with bounded memory (oldest keys are evicted first)."""

    def __init__(self, max_keys: int = 100_000, clock: Callable[[], float] = time.monotonic) -> None:
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, int]] = OrderedDict()  # key -> (window end, count)

    def __len__(self) -> int:
        return len(self._buckets)

    async def hit(self, key: str, limit: int, window_sec: int) -> RateResult:
        now = self._clock()
        ends, count = self._buckets.get(key, (0.0, 0))
        if ends <= now:
            ends, count = now + window_sec, 0
        count += 1
        self._buckets[key] = (ends, count)
        self._buckets.move_to_end(key)
        self._prune(now)
        if count > limit:
            return RateResult(False, 0, max(1, math.ceil(ends - now)))
        return RateResult(True, limit - count, 0)

    async def reset(self, key: str) -> None:
        self._buckets.pop(key, None)

    def _prune(self, now: float) -> None:
        # Drop expired windows from the cold end, then enforce the size bound.
        while self._buckets:
            oldest_key, (ends, _) = next(iter(self._buckets.items()))
            if ends > now and len(self._buckets) <= self._max_keys:
                break
            del self._buckets[oldest_key]


class RedisRateLimiter:
    """Fixed-window counters in Redis (``INCR`` + ``EXPIRE``, works on Redis 5+).

    While Redis is unavailable or rejects the commands, counting falls back to a per-process
    :class:`MemoryRateLimiter` (limits stay enforced per replica) and Redis is retried every ``retry_sec``.
    """

    def __init__(
        self,
        redis: Any,
        prefix: str = "rl:",
        *,
        fallback: MemoryRateLimiter | None = None,
        retry_sec: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._fallback = fallback or MemoryRateLimiter()
        self._retry_sec = retry_sec
        self._clock = clock
        self._down_until = 0.0

    async def hit(self, key: str, limit: int, window_sec: int) -> RateResult:
        if self._clock() < self._down_until:
            return await self._fallback.hit(key, limit, window_sec)
        name = self._prefix + key
        try:
            pipe = self._redis.pipeline(transaction=True)
            pipe.incr(name)
            pipe.ttl(name)
            count, ttl = await pipe.execute()
            count, ttl = int(count), int(ttl) if ttl is not None else -1
            if ttl < 0:
                # A new window (or a counter left without a TTL by an interrupted request): start the clock now.
                await self._redis.expire(name, window_sec)
                ttl = window_sec
        except Exception as exc:
            self._down_until = self._clock() + self._retry_sec
            log.warning(
                "rate limiter: redis failed (%s: %s); counting in memory for %d s",
                type(exc).__name__,
                str(exc)[:200],
                int(self._retry_sec),
            )
            return await self._fallback.hit(key, limit, window_sec)
        if count > limit:
            return RateResult(False, 0, max(1, ttl))
        return RateResult(True, limit - count, 0)

    async def reset(self, key: str) -> None:
        await self._fallback.reset(key)
        try:
            await self._redis.delete(self._prefix + key)
        except Exception as exc:
            log.warning("rate limiter: redis reset failed (%s)", type(exc).__name__)


def build_rate_limiter(redis: Any | None) -> RateLimiter:
    """Redis-backed limiter when a client is configured, else an in-memory one."""
    return RedisRateLimiter(redis) if redis is not None else MemoryRateLimiter()
