"""Anti-flood: a token bucket per user (``THROTTLE_RATE_LIMIT`` updates per ``THROTTLE_PERIOD_SEC``).

Buckets live in Redis when it is configured (shared by every bot process), otherwise in memory with pruning of idle
buckets. Administrators and payment confirmations are never throttled. A throttled user is warned at most once per
period; further updates are dropped silently (callback spinners are still stopped).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.middlewares.common import answer_silently, from_user, is_admin, is_payment_update, respond, translator_of

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

MAX_BUCKETS = 50_000
PRUNE_EVERY = 1000  # memory buckets: prune idle ones every N checks
REDIS_PREFIX = "throttle:"
REDIS_RETRY_SEC = 30.0  # after a Redis error use memory buckets for this long

# KEYS[1] = bucket, ARGV = capacity, refill per second, ttl seconds. Returns 1 if a token was taken.
_TOKEN_BUCKET_LUA = """
local cap = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local b = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(b[1])
local ts = tonumber(b[2])
if tokens == nil or ts == nil then
  tokens = cap
  ts = now
end
tokens = math.min(cap, tokens + math.max(0, now - ts) * rate)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', KEYS[1], ttl)
return allowed
"""


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    warn: bool = False  # first refusal in this period: tell the user once


class RateLimiter(Protocol):
    async def hit(self, key: int, capacity: int, period: float) -> Decision: ...


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float
    warned_until: float = 0.0


class MemoryRateLimiter:
    """Token buckets in process memory; idle buckets are pruned."""

    def __init__(self, max_buckets: int = MAX_BUCKETS, clock: Callable[[], float] = time.monotonic) -> None:
        self._buckets: dict[int, _Bucket] = {}
        self._max = max_buckets
        self._clock = clock
        self._checks = 0

    def __len__(self) -> int:
        return len(self._buckets)

    async def hit(self, key: int, capacity: int, period: float) -> Decision:
        return self.hit_sync(key, capacity, period)

    def hit_sync(self, key: int, capacity: int, period: float) -> Decision:
        now = self._clock()
        self._checks += 1
        if self._checks % PRUNE_EVERY == 0 or len(self._buckets) >= self._max:
            self.prune(period, now)
        rate = capacity / period
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = _Bucket(tokens=float(capacity), updated=now)
        else:
            bucket.tokens = min(float(capacity), bucket.tokens + (now - bucket.updated) * rate)
            bucket.updated = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return Decision(True)
        if now >= bucket.warned_until:
            bucket.warned_until = now + period
            return Decision(False, warn=True)
        return Decision(False)

    def prune(self, period: float, now: float | None = None) -> None:
        """Forget buckets idle long enough to be full again (they behave exactly like new ones)."""
        now = self._clock() if now is None else now
        self._buckets = {k: b for k, b in self._buckets.items() if now - b.updated < period or b.warned_until > now}
        if len(self._buckets) >= self._max:  # a flood of distinct users: drop the least recently used
            keep = sorted(self._buckets.items(), key=lambda kv: kv[1].updated)[len(self._buckets) - self._max // 2 :]
            self._buckets = dict(keep)


class RedisRateLimiter:
    """Token buckets in Redis (atomic Lua script); falls back to memory while Redis is unavailable."""

    def __init__(self, redis: Any, fallback: MemoryRateLimiter | None = None) -> None:
        self._redis = redis
        self._fallback = fallback or MemoryRateLimiter()
        self._script: Any = None
        self._down_until = 0.0

    async def hit(self, key: int, capacity: int, period: float) -> Decision:
        if time.monotonic() < self._down_until:
            return self._fallback.hit_sync(key, capacity, period)
        try:
            if self._script is None:
                self._script = self._redis.register_script(_TOKEN_BUCKET_LUA)
            ttl = max(1, int(period) + 1)
            allowed = await self._script(keys=[f"{REDIS_PREFIX}{key}"], args=[capacity, capacity / period, ttl])
            if int(allowed) == 1:
                return Decision(True)
            first = await self._redis.set(f"{REDIS_PREFIX}warn:{key}", "1", nx=True, ex=ttl)
            return Decision(False, warn=bool(first))
        except Exception as exc:
            if time.monotonic() >= self._down_until:
                log.warning("throttling: Redis unavailable (%s), using in-memory buckets", exc)
            self._down_until = time.monotonic() + REDIS_RETRY_SEC
            return self._fallback.hit_sync(key, capacity, period)


def make_limiter(redis: Any | None) -> RateLimiter:
    return RedisRateLimiter(redis) if redis is not None else MemoryRateLimiter()


class ThrottlingMiddleware(BaseMiddleware):
    """Outer ``update`` middleware; must run after :class:`~app.middlewares.user.UserMiddleware`."""

    def __init__(self, ctx: AppContext, limiter: RateLimiter | None = None) -> None:
        self._ctx = ctx
        self._limiter = limiter or make_limiter(getattr(ctx, "redis", None))

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update) or is_payment_update(event) or is_admin(data):
            return await handler(event, data)
        user = from_user(data)
        if user is None:
            return await handler(event, data)
        settings = self._ctx.settings
        capacity, period = settings.throttle_rate_limit, float(settings.throttle_period_sec)
        if capacity <= 0 or period <= 0:
            return await handler(event, data)
        decision = await self._limiter.hit(user.id, capacity, period)
        if decision.allowed:
            return await handler(event, data)
        bot = data["bot"]
        if decision.warn:
            _ = translator_of(data, self._ctx)
            await respond(event, bot, _("access.throttled"))
        else:
            await answer_silently(event, bot)
        return None
