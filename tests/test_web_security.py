from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.modules import FeatureModule
from app.db.session import SessionFactory
from app.web.errors import ApiError
from app.web.masking import mask_path, mask_query, mask_url
from app.web.ratelimit import MemoryRateLimiter, RedisRateLimiter, build_rate_limiter
from app.web.security import RateRule, resolve_client_ip, trusted_networks
from tests.conftest import make_settings
from tests.test_web_support import AsgiClient, make_ctx, web_app

TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"


# ---------------------------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/sub/short", "/sub/***"),
        ("/sub/abc/wl", "/sub/***/wl"),
        ("/open/happ/tok", "/open/happ/***"),
        ("/gift/CODE123", "/gift/***"),
        ("/dev/pay/1234-5678", "/dev/pay/***"),
        (f"/api/app/x/{TOKEN}", "/api/app/x/***"),
        ("/api/admin/users/42", "/api/admin/users/42"),
        ("/healthz", "/healthz"),
        ("/", "/"),
        ("", ""),
        ("/sub", "/sub"),
    ],
)
def test_mask_path(path: str, expected: str) -> None:
    assert mask_path(path) == expected


def test_mask_query_and_url() -> None:
    assert mask_query("") == ""
    assert mask_query("page=2&token=abc") == "page=2&token=***"
    assert mask_query(f"x={TOKEN}") == "x=***"
    assert mask_query("initData=a%3Db&lang=ru") == "initData=***&lang=ru"
    assert mask_url("/sub/abc", "format=json") == "/sub/***?format=json"
    assert mask_url("/healthz") == "/healthz"


# ---------------------------------------------------------------------------------------------
# Client IP
# ---------------------------------------------------------------------------------------------
NETS = trusted_networks(make_settings(trusted_proxies="127.0.0.1,10.0.0.0/8,::1"))


def test_client_ip_untrusted_peer_ignores_headers() -> None:
    assert resolve_client_ip("203.0.113.9", "1.1.1.1", "2.2.2.2", NETS) == "203.0.113.9"


def test_client_ip_trusted_chain_takes_rightmost_untrusted() -> None:
    assert resolve_client_ip("127.0.0.1", "6.6.6.6, 198.51.100.7, 10.0.0.3", None, NETS) == "198.51.100.7"


def test_client_ip_all_trusted_returns_leftmost() -> None:
    assert resolve_client_ip("10.1.1.1", "10.0.0.5, 10.0.0.3", None, NETS) == "10.0.0.5"


def test_client_ip_invalid_entry_stops_walk() -> None:
    assert resolve_client_ip("127.0.0.1", "198.51.100.1, garbage, 10.0.0.3", None, NETS) == "10.0.0.3"
    assert resolve_client_ip("127.0.0.1", "garbage", None, NETS) == "127.0.0.1"


def test_client_ip_real_ip_ports_and_ipv6() -> None:
    assert resolve_client_ip("127.0.0.1", None, "198.51.100.2", NETS) == "198.51.100.2"
    assert resolve_client_ip("127.0.0.1", "198.51.100.3:4444", None, NETS) == "198.51.100.3"
    assert resolve_client_ip("::1", "[2001:db8::1]:443", None, NETS) == "2001:db8::1"
    assert resolve_client_ip("::ffff:127.0.0.1", "198.51.100.4", None, NETS) == "198.51.100.4"
    assert resolve_client_ip(None, None, None, NETS) == "unknown"
    assert resolve_client_ip("not-an-ip", None, None, NETS) == "not-an-ip"


def test_client_ip_conflicting_proxy_headers_are_not_trusted() -> None:
    # A proxy that sets only X-Real-IP passes a client-sent X-Forwarded-For through unchanged.
    assert resolve_client_ip("127.0.0.1", "6.6.6.6", "198.51.100.2", NETS) == "127.0.0.1"
    assert resolve_client_ip("127.0.0.1", "198.51.100.2", "198.51.100.2", NETS) == "198.51.100.2"
    assert resolve_client_ip("127.0.0.1", "6.6.6.6, 198.51.100.2", "198.51.100.2", NETS) == "198.51.100.2"
    # An X-Real-IP naming a trusted proxy (set by an inner proxy) carries no client information.
    assert resolve_client_ip("127.0.0.1", "198.51.100.3", "10.0.0.2", NETS) == "198.51.100.3"
    assert resolve_client_ip("127.0.0.1", None, "10.0.0.2", NETS) == "127.0.0.1"


def test_invalid_trusted_proxy_entries_are_skipped() -> None:
    nets = trusted_networks(make_settings(trusted_proxies="bogus,127.0.0.1"))
    assert len(nets) == 1


# ---------------------------------------------------------------------------------------------
# Rate limiters
# ---------------------------------------------------------------------------------------------
async def test_memory_rate_limiter_window() -> None:
    now = [100.0]
    limiter = MemoryRateLimiter(clock=lambda: now[0])
    results = [await limiter.hit("k", 2, 60) for _ in range(3)]
    assert [r.allowed for r in results] == [True, True, False]
    assert results[0].remaining == 1
    assert results[2].retry_after == 60
    now[0] += 30
    assert (await limiter.hit("k", 2, 60)).retry_after == 30
    now[0] += 31
    assert (await limiter.hit("k", 2, 60)).allowed
    await limiter.reset("k")
    assert len(limiter) == 0


async def test_memory_rate_limiter_is_bounded() -> None:
    now = [0.0]
    limiter = MemoryRateLimiter(max_keys=10, clock=lambda: now[0])
    for i in range(50):
        await limiter.hit(f"k{i}", 5, 60)
    assert len(limiter) == 10
    now[0] += 61
    await limiter.hit("fresh", 5, 60)
    assert len(limiter) == 1


class _ResponseError(Exception):
    """Stands in for ``redis.exceptions.ResponseError``."""


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self.redis = redis
        self.ops: list[tuple[str, Any]] = []

    def incr(self, name: str) -> None:
        self.ops.append(("incr", name))

    def expire(self, name: str, ttl: int, nx: bool = False) -> None:
        self.ops.append(("expire", (name, ttl, nx)))

    def ttl(self, name: str) -> None:
        self.ops.append(("ttl", name))

    async def execute(self) -> list[Any]:
        if self.redis.down:
            raise ConnectionError("down")
        if self.redis.version < 7 and any(op == "expire" and arg[2] for op, arg in self.ops):
            # Redis 6 rejects the queued command and the whole MULTI/EXEC block is discarded.
            raise _ResponseError("EXECABORT Transaction discarded because of previous errors.")
        out: list[Any] = []
        for op, arg in self.ops:
            if op == "incr":
                self.redis.data[arg] = self.redis.data.get(arg, 0) + 1
                out.append(self.redis.data[arg])
            elif op == "expire":
                self.redis.ttls.setdefault(arg[0], arg[1])
                out.append(True)
            else:
                out.append(self.redis.ttls.get(arg, -1))
        return out


class _FakeRedis:
    def __init__(self, version: int = 7) -> None:
        self.data: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.down = False
        self.version = version

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)

    async def expire(self, name: str, ttl: int) -> bool:
        if self.down:
            raise ConnectionError("down")
        if name not in self.data:
            return False
        self.ttls[name] = ttl
        return True

    async def delete(self, name: str) -> None:
        if self.down:
            raise ConnectionError("down")
        self.data.pop(name, None)
        self.ttls.pop(name, None)


async def test_redis_rate_limiter_enforces_limits_on_redis_6() -> None:
    redis = _FakeRedis(version=6)
    limiter = RedisRateLimiter(redis)
    assert (await limiter.hit("login", 2, 60)).allowed
    assert (await limiter.hit("login", 2, 60)).allowed
    blocked = await limiter.hit("login", 2, 60)
    assert not blocked.allowed and blocked.retry_after == 60
    assert redis.ttls["rl:login"] == 60


async def test_redis_rate_limiter_repairs_counter_without_ttl() -> None:
    redis = _FakeRedis()
    redis.data["rl:k"] = 5  # left behind without a TTL
    limiter = RedisRateLimiter(redis)
    assert not (await limiter.hit("k", 3, 30)).allowed
    assert redis.ttls["rl:k"] == 30


async def test_redis_rate_limiter_counts_in_memory_while_redis_fails() -> None:
    now = [0.0]
    redis = _FakeRedis()
    redis.down = True
    limiter = RedisRateLimiter(redis, retry_sec=30, clock=lambda: now[0])
    assert (await limiter.hit("a", 1, 60)).allowed
    assert not (await limiter.hit("a", 1, 60)).allowed  # still limited, per process
    redis.down = False
    now[0] += 31
    assert (await limiter.hit("a", 1, 60)).allowed  # back on Redis
    assert redis.data["rl:a"] == 1


async def test_redis_rate_limiter() -> None:
    redis = _FakeRedis()
    limiter = build_rate_limiter(redis)
    assert isinstance(limiter, RedisRateLimiter)
    assert (await limiter.hit("a", 1, 30)).allowed
    blocked = await limiter.hit("a", 1, 30)
    assert not blocked.allowed and blocked.retry_after == 30
    await limiter.reset("a")
    assert (await limiter.hit("a", 1, 30)).allowed
    redis.down = True
    assert (await limiter.hit("a", 1, 30)).allowed  # counted in memory meanwhile
    await limiter.reset("a")  # does not raise
    assert isinstance(build_rate_limiter(None), MemoryRateLimiter)


# ---------------------------------------------------------------------------------------------
# Middleware stack and errors (through a test module)
# ---------------------------------------------------------------------------------------------
class Echo(BaseModel):
    n: int


def _module() -> FeatureModule:
    public = APIRouter()

    @public.get("/sub/{token}")
    async def sub(token: str) -> dict[str, str]:
        return {"ok": token[:2]}

    @public.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    @public.post("/typed")
    async def typed(body: Echo) -> dict[str, int]:
        return {"n": body.n}

    @public.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret detail")

    @public.get("/teapot")
    async def teapot() -> None:
        raise ApiError(418, "teapot", "I am a teapot", extra={"hint": "tea"})

    @public.get("/framed")
    async def framed() -> Any:
        from fastapi.responses import HTMLResponse

        return HTMLResponse("x", headers={"content-security-policy": "default-src 'self'"})

    return FeatureModule(name="probe", title_ru="p", title_en="p", web_routers=[public])


@pytest.fixture
def client(session_factory: SessionFactory) -> AsgiClient:
    ctx = make_ctx(make_settings(), session_factory, _module())
    app = web_app(ctx, rate_rules=(RateRule("sub", "/sub/", 3, 60),), body_limit=100)
    return AsgiClient(app)


async def test_security_headers_by_area(client: AsgiClient) -> None:
    api = await client.get("/api/admin/auth/me")
    assert api.headers["x-frame-options"] == "DENY"
    assert api.headers["content-security-policy"].startswith("default-src 'none'")
    assert api.headers["cache-control"] == "no-store"
    assert api.headers["x-content-type-options"] == "nosniff"
    assert api.headers["referrer-policy"] == "no-referrer"
    assert api.headers["strict-transport-security"].startswith("max-age=")

    admin = await client.get("/admin/")
    assert "frame-ancestors 'none'" in admin.headers["content-security-policy"]
    assert admin.headers["cross-origin-opener-policy"] == "same-origin"

    miniapp = await client.get("/app/")
    assert "x-frame-options" not in miniapp.headers
    assert "https://web.telegram.org" in miniapp.headers["content-security-policy"]

    other = await client.get("/sub/abc")
    assert "frame-ancestors 'none'" in other.headers["content-security-policy"]

    framed = await client.get("/framed")
    assert framed.headers["content-security-policy"] == "default-src 'self'"  # route value is kept


async def test_no_hsts_without_https_base(session_factory: SessionFactory) -> None:
    ctx = make_ctx(make_settings(public_base_url="http://localhost:8080"), session_factory)
    resp = await AsgiClient(web_app(ctx)).get("/healthz")
    assert "strict-transport-security" not in resp.headers


async def test_request_id(client: AsgiClient) -> None:
    given = await client.get("/sub/x", headers={"x-request-id": "abc-123"})
    assert given.headers["x-request-id"] == "abc-123"
    bad = await client.get("/sub/x", headers={"x-request-id": "bad id with spaces"})
    assert bad.headers["x-request-id"] != "bad id with spaces"
    assert len(bad.headers["x-request-id"]) == 16


async def test_rate_limit(client: AsgiClient) -> None:
    for _ in range(3):
        assert (await client.get("/sub/x")).status == 200
    blocked = await client.get("/sub/x")
    assert blocked.status == 429
    assert blocked.json()["error"] == "too_many_requests"
    assert int(blocked.headers["retry-after"]) > 0
    other_ip = await client.get("/sub/x", client_ip="198.51.100.77")
    assert other_ip.status == 200
    assert (await client.post("/echo", body=b"x")).status == 200  # unlisted paths are not limited


async def test_body_limit_by_content_length(client: AsgiClient) -> None:
    assert (await client.post("/echo", body=b"x" * 100)).json() == {"size": 100}
    resp = await client.post("/echo", body=b"x" * 101)
    assert resp.status == 413
    assert resp.json()["error"] == "payload_too_large"
    bad = await client.post("/echo", body=b"x", headers={"content-length": "nope"})
    assert bad.status == 400


async def test_body_limit_streamed(client: AsgiClient) -> None:
    resp = await client.post("/echo", chunks=[b"x" * 60, b"x" * 60])
    assert resp.status == 413
    typed = await client.post(
        "/typed", chunks=[b'{"n": 1, "pad": "' + b"x" * 90, b'"}'], headers={"content-type": "application/json"}
    )
    assert typed.status == 413
    ok = await client.post("/echo", chunks=[b"x" * 40, b"x" * 40])
    assert ok.json() == {"size": 80}


async def test_error_shapes(client: AsgiClient) -> None:
    missing = await client.get("/api/nothing")
    assert missing.status == 404
    assert missing.json() == {"error": "not_found", "message": "Not Found"}

    invalid = await client.post("/typed", json={"n": "x"})
    assert invalid.status == 422
    body = invalid.json()
    assert body["error"] == "validation_error"
    assert body["details"][0]["loc"] == ["body", "n"]

    teapot = await client.get("/teapot")
    assert teapot.status == 418
    assert teapot.json() == {"error": "teapot", "message": "I am a teapot", "hint": "tea"}

    boom = await client.get("/boom")
    assert boom.status == 500
    assert boom.json() == {"error": "internal_error", "message": "Internal server error"}
    assert "secret detail" not in boom.text


@pytest.fixture
def full_ip_client(session_factory: SessionFactory) -> AsgiClient:
    """A client of an app whose owner chose to log customer IPs in full."""
    ctx = make_ctx(make_settings(client_ip_logging="full"), session_factory, _module())
    return AsgiClient(web_app(ctx, rate_rules=(RateRule("sub", "/sub/", 3, 60),), body_limit=100))


async def test_access_log_masks_tokens(full_ip_client: AsgiClient, caplog: pytest.LogCaptureFixture) -> None:
    client = full_ip_client
    with caplog.at_level(logging.INFO, logger="app.web.access"):
        await client.get(f"/sub/{TOKEN}?token=zzz&page=1")
    lines = [r.getMessage() for r in caplog.records if r.name == "app.web.access"]
    assert lines, "access log line expected"
    assert TOKEN not in lines[-1] and "zzz" not in lines[-1]
    assert "/sub/***" in lines[-1] and "page=1" in lines[-1]
    assert "203.0.113.5" in lines[-1]


async def test_access_log_uses_forwarded_ip_from_trusted_proxy(
    full_ip_client: AsgiClient, caplog: pytest.LogCaptureFixture
) -> None:
    client = full_ip_client
    with caplog.at_level(logging.INFO, logger="app.web.access"):
        await client.get("/sub/x", client_ip="127.0.0.1", headers={"x-forwarded-for": "198.51.100.9"})
        await client.get("/sub/x", client_ip="198.51.100.10", headers={"x-forwarded-for": "1.2.3.4"})
    lines = [r.getMessage() for r in caplog.records if r.name == "app.web.access"]
    assert lines[-2].startswith("198.51.100.9 ")
    assert lines[-1].startswith("198.51.100.10 ")


async def test_docs_only_in_dev_mode(session_factory: SessionFactory) -> None:
    prod = AsgiClient(web_app(make_ctx(make_settings(), session_factory)))
    assert (await prod.get("/api/openapi.json")).status == 404
    dev = AsgiClient(web_app(make_ctx(make_settings(dev_mode=True), session_factory)))
    assert (await dev.get("/api/openapi.json")).status == 200


async def test_large_body_limit_applies_only_to_upload_endpoints(session_factory: SessionFactory) -> None:
    client = AsgiClient(web_app(make_ctx(make_settings(), session_factory)))
    two_mib = {"content-length": str(2 * 1024 * 1024), "content-type": "application/json"}
    for path in ("/api/admin/backups/restore", "/api/admin/backups", "/api/admin/broadcasts/media/x"):
        resp = await client.post(path, headers=two_mib)
        assert resp.status == 413, path
    for path in ("/api/admin/backups/upload", "/api/admin/broadcasts/media"):
        assert (await client.post(path, headers=two_mib)).status != 413, path
