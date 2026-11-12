"""RemnawaveBackend against an in-process fake Remnawave 3.x API (aiohttp.web).

The fake mirrors the behaviour verified on Remnawave 3.4.8: ``{"response": ...}`` envelopes, error codes
(A019/A025/A029/A030/A063), ``204`` on delete, PATCH refusing past ``expireAt``, EXPIRED users being
re-activated by a future ``expireAt``, and the socket being dropped without ``X-Forwarded-*`` headers.
"""

from __future__ import annotations

import base64
import json
import re
import uuid as uuidlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.db.models import PanelKind
from app.services.panels.base import (
    AccountSpec,
    PanelAuthError,
    PanelBackend,
    PanelError,
    PanelUnsupportedError,
    RemoteAccount,
)
from app.services.panels.remnawave import (
    NEVER_EXPIRES,
    RemnawaveApiError,
    RemnawaveBackend,
    format_datetime,
    is_placeholder_link,
    parse_datetime,
    parse_version,
)

TOKEN = "test-api-token"
SQUAD_A = "11111111-1111-4111-8111-111111111111"
SQUAD_B = "22222222-2222-4222-8222-222222222222"
PLACEHOLDER = "vless://00000000-0000-0000-0000-000000000000@0.0.0.0:1?type=tcp&security=none#No%20hosts"
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,36}$")


def _err(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"timestamp": "now", "path": "/", "message": message, "errorCode": code}, status=status)


def _ok(data: Any, status: int = 200) -> web.Response:
    return web.json_response({"response": data}, status=status)


@dataclass
class FakeRemnawave:
    """In-memory Remnawave 3.x subset. Every request is recorded in ``requests``."""

    version: str = "3.4.8"
    require_forwarded: bool = True
    squads: list[str] = field(default_factory=lambda: [SQUAD_A, SQUAD_B])
    users: dict[int, dict[str, Any]] = field(default_factory=dict)
    devices: dict[int, list[str]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    # Fault injection
    fail_once: dict[tuple[str, str], int] = field(default_factory=dict)  # (method, path) -> status
    hide_username_once: set[str] = field(default_factory=set)  # by-username answers 404 once
    hide_username_always: set[str] = field(default_factory=set)  # by-username always answers 404
    stale_status: dict[int, str] = field(default_factory=dict)  # by-username reports this status once
    delete_already_gone: bool = False
    forbidden_paths: set[str] = field(default_factory=set)
    _next_id: int = 1

    # ---------------------------------------------------------------- app

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._middleware])
        r = app.router
        r.add_get("/api/system/metadata", self.metadata)
        r.add_get("/api/internal-squads", self.list_squads)
        r.add_get("/api/users", self.list_users)
        r.add_post("/api/users", self.create_user)
        r.add_patch("/api/users", self.update_user)
        r.add_get("/api/users/by-username/{username}", self.by_username)
        r.add_get("/api/users/{id:\\d+}", self.by_id)
        r.add_delete("/api/users/{id:\\d+}", self.delete_user)
        r.add_post("/api/users/{id:\\d+}/actions/{action}", self.action)
        r.add_get("/api/subscriptions/connection-keys/{id:\\d+}", self.connection_keys)
        r.add_post("/api/hwid/devices/delete-all", self.delete_devices)
        r.add_get("/api/sub/{short}", self.public_sub)
        return app

    @web.middleware
    async def _middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        body = await request.json() if request.can_read_body else None
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "headers": dict(request.headers),
                "json": body,
            }
        )
        forwarded = request.headers.get("X-Forwarded-For") and request.headers.get("X-Forwarded-Proto") == "https"
        if self.require_forwarded and not forwarded:
            assert request.transport is not None
            request.transport.close()  # what Remnawave does: socket destroyed, empty reply
            raise web.HTTPInternalServerError()
        injected = self.fail_once.pop((request.method, request.path), None)
        if injected:
            return web.json_response({"message": "injected"}, status=injected)
        if request.path in self.forbidden_paths:
            return _err(403, "A004", "Forbidden")
        if not request.path.startswith("/api/sub/") and request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return _err(401, "A003", "Unauthorized")
        return await handler(request)

    # ---------------------------------------------------------------- helpers

    def add_user(self, username: str, **overrides: Any) -> dict[str, Any]:
        uid = self._next_id
        self._next_id += 1
        short = f"short{uid:011d}"
        user: dict[str, Any] = {
            "id": uid,
            "shortUuid": short,
            "username": username,
            "status": "ACTIVE",
            "trafficLimitBytes": 0,
            "trafficLimitStrategy": "NO_RESET",
            "expireAt": "2030-01-01T00:00:00.000Z",
            "telegramId": None,
            "email": None,
            "description": None,
            "tag": None,
            "hwidDeviceLimit": None,
            "externalSquadUuid": None,
            "vlessUuid": str(uuidlib.uuid4()),
            "subscriptionUrl": f"https://panel.example.com/api/sub/{short}",
            "activeInternalSquads": [],
            "userTraffic": {"usedTrafficBytes": 0, "lifetimeUsedTrafficBytes": 0, "onlineAt": None},
        }
        user.update(overrides)
        self.users[uid] = user
        return user

    def by_name(self, username: str) -> dict[str, Any] | None:
        return next((u for u in self.users.values() if u["username"] == username), None)

    def calls(self, method: str | None = None, prefix: str = "/api/") -> list[tuple[str, str]]:
        return [
            (r["method"], r["path"])
            for r in self.requests
            if (method is None or r["method"] == method)
            and r["path"].startswith(prefix)
            and r["path"] != "/api/system/metadata"
        ]

    def last(self, method: str, path: str) -> dict[str, Any]:
        return next(r for r in reversed(self.requests) if r["method"] == method and r["path"] == path)

    @staticmethod
    def _squads_view(uuids: list[str]) -> list[dict[str, str]]:
        return [{"uuid": u, "name": f"squad-{u[:4]}"} for u in uuids]

    # ---------------------------------------------------------------- handlers

    async def metadata(self, request: web.Request) -> web.Response:
        return _ok({"version": self.version, "build": {"time": "", "number": "1"}})

    async def list_squads(self, request: web.Request) -> web.Response:
        items = [
            {"uuid": u, "name": f"squad-{i}", "info": {"membersCount": i, "inboundsCount": 1}, "inbounds": []}
            for i, u in enumerate(self.squads)
        ]
        return _ok({"total": len(items), "internalSquads": items})

    async def list_users(self, request: web.Request) -> web.Response:
        start, size = int(request.query.get("start", 0)), int(request.query.get("size", 25))
        users = sorted(self.users.values(), key=lambda u: u["id"])
        return _ok({"users": users[start : start + size], "total": len(users)})

    async def by_username(self, request: web.Request) -> web.Response:
        name = request.match_info["username"]
        user = self.by_name(name)
        if name in self.hide_username_once or name in self.hide_username_always:
            self.hide_username_once.discard(name)
            user = None
        if user is None:
            return _err(404, "A063", "User with specified params not found")
        status = self.stale_status.pop(user["id"], None)
        return _ok({**user, "status": status} if status else user)

    async def by_id(self, request: web.Request) -> web.Response:
        user = self.users.get(int(request.match_info["id"]))
        return _ok(user) if user else _err(404, "A063", "User with specified params not found")

    async def create_user(self, request: web.Request) -> web.Response:
        body = await request.json()
        if not USERNAME_RE.match(body.get("username", "")):
            return web.json_response(
                {
                    "statusCode": 400,
                    "message": "Validation failed",
                    "errors": [{"path": ["username"], "message": "Invalid username"}],
                },
                status=400,
            )
        if self.by_name(body["username"]):
            return _err(400, "A019", "User username already exists")
        body["activeInternalSquads"] = self._squads_view(body.get("activeInternalSquads", []))
        user = self.add_user(body.pop("username"), **body)
        return _ok(user, status=201)

    async def update_user(self, request: web.Request) -> web.Response:
        body = await request.json()
        user = self.users.get(body.pop("id"))
        if user is None:
            return _err(404, "A025", "User not found")
        if "expireAt" in body and parse_datetime(body["expireAt"]) <= datetime.now(UTC):  # type: ignore[operator]
            return web.json_response(
                {
                    "statusCode": 400,
                    "message": "Validation failed",
                    "errors": [{"path": ["expireAt"], "message": "Expiration date cannot be in the past"}],
                },
                status=400,
            )
        reactivate = user["status"] == "EXPIRED" and "status" not in body
        if reactivate and body.get("expireAt", user["expireAt"]) != user["expireAt"]:
            user["status"] = "ACTIVE"
        if "activeInternalSquads" in body:
            body["activeInternalSquads"] = self._squads_view(body["activeInternalSquads"])
        user.update(body)
        return _ok(user)

    async def action(self, request: web.Request) -> web.Response:
        user = self.users.get(int(request.match_info["id"]))
        if user is None:
            return _err(404, "A025", "User not found")
        action = request.match_info["action"]
        if action == "enable":
            if user["status"] == "ACTIVE":
                return _err(400, "A030", "User already enabled")
            user["status"] = "ACTIVE"
        elif action == "disable":
            if user["status"] == "DISABLED":
                return _err(400, "A029", "User already disabled")
            user["status"] = "DISABLED"
        return _ok(user)

    async def delete_user(self, request: web.Request) -> web.Response:
        uid = int(request.match_info["id"])
        if self.delete_already_gone:
            self.users.pop(uid, None)
        if self.users.pop(uid, None) is None:
            return _err(404, "A025", "User not found")
        return web.Response(status=204)

    async def connection_keys(self, request: web.Request) -> web.Response:
        user = self.users.get(int(request.match_info["id"]))
        if user is None:
            return _err(404, "A025", "User not found")
        enabled = [
            f"vless://{user['vlessUuid']}@de1.example.com:443?security=reality&type=tcp#DE-1",
            PLACEHOLDER,
            f"trojan://{user['vlessUuid']}@nl1.example.com:443?security=tls#NL",
        ]
        return _ok({"enabledKeys": enabled, "hiddenKeys": ["vless://h@hidden:1#H"], "disabledKeys": []})

    async def delete_devices(self, request: web.Request) -> web.Response:
        uid = (await request.json())["userId"]
        if uid not in self.users:
            return _err(404, "A025", "User not found")
        self.devices[uid] = []
        return _ok({"total": 0, "devices": []})

    async def public_sub(self, request: web.Request) -> web.Response:
        user = next((u for u in self.users.values() if u["shortUuid"] == request.match_info["short"]), None)
        if user is None:
            return web.Response(status=404, text="Not found")
        body = base64.b64encode(f"vless://{user['vlessUuid']}@de1.example.com:443#DE".encode()).decode()
        return web.Response(
            text=body,
            headers={
                "profile-title": "base64:UmVtbmF3YXZl",
                "subscription-userinfo": "upload=0; download=0; total=0; expire=1801612800",
                "X-Powered-By": "Express",
                "ETag": 'W/"x"',
            },
        )


# ---------------------------------------------------------------------- fixtures


@pytest.fixture
def fake() -> FakeRemnawave:
    return FakeRemnawave()


@pytest.fixture
async def server(fake: FakeRemnawave) -> AsyncIterator[TestServer]:
    srv = TestServer(fake.app())
    async with srv:
        yield srv


def _make_backend(server: TestServer, **kwargs: Any) -> RemnawaveBackend:
    token = kwargs.pop("api_token", TOKEN)
    params: dict[str, Any] = {"squad_uuids": [SQUAD_A], **kwargs}
    backend = RemnawaveBackend(str(server.make_url("")), token, **params)
    backend.retry_delays = (0.0, 0.0)
    return backend


@pytest.fixture
async def backend(server: TestServer) -> AsyncIterator[RemnawaveBackend]:
    b = _make_backend(server)
    try:
        yield b
    finally:
        await b.close()


def _spec(**overrides: Any) -> AccountSpec:
    base: dict[str, Any] = {
        "email": "tg_123456789",
        "uuid": "0b9a3c0e-5f4e-4c7a-9a4b-2c1d3e4f5a6b",
        "enabled": True,
        "expires_at": datetime(2031, 12, 4, tzinfo=UTC),
        "device_limit": 3,
        "traffic_limit_bytes": 100 * 1024**3,
        "tg_id": 123456789,
        "sub_id": "abcdefghijklmnop",
        "comment": "vpnbot sub #1",
    }
    base.update(overrides)
    return AccountSpec(**base)


# ---------------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("3.4.8", (3, 4, 8)), ("v3.5.0-beta.1", (3, 5, 0)), ("2.6", (2, 6, 0)), (" 10.0.1 ", (10, 0, 1))],
)
def test_parse_version(raw: str, expected: tuple[int, int, int]) -> None:
    assert parse_version(raw) == expected


@pytest.mark.parametrize("raw", ["", "dev", "3", "x.y.z"])
def test_parse_version_rejects_garbage(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_version(raw)


def test_format_datetime_is_utc_millis_z() -> None:
    dt = datetime(2026, 12, 4, 3, 0, 0, 123456, tzinfo=timezone(timedelta(hours=3)))
    assert format_datetime(dt) == "2026-12-04T00:00:00.123Z"
    assert format_datetime(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05.000Z"  # naive = UTC


def test_parse_datetime_roundtrip() -> None:
    assert parse_datetime("2026-12-04T00:00:00.000Z") == datetime(2026, 12, 4, tzinfo=UTC)
    assert parse_datetime("2026-12-04T03:00:00+03:00") == datetime(2026, 12, 4, tzinfo=UTC)
    assert parse_datetime(None) is None
    assert parse_datetime("") is None


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (PLACEHOLDER, True),
        ("trojan://pw@0.0.0.0:1?security=none#Expired", True),
        ("ss://YWVzLTEyOC1nY206cHc=@0.0.0.0:1#x", True),
        ("vless://id@de1.example.com:443?type=tcp#DE", False),
        ("vless://id@0.0.0.0:443#DE", False),
        ("vless://id@[2001:db8::1]:1#v6", False),
        ("vmess://" + base64.b64encode(json.dumps({"add": "0.0.0.0", "port": 1}).encode()).decode(), True),
        ("vmess://" + base64.b64encode(json.dumps({"add": "a.example", "port": "443"}).encode()).decode(), False),
        ("vmess://not-base64!!", False),
        ("vless://id@host:notaport#bad", False),
        ("garbage", False),
    ],
)
def test_is_placeholder_link(link: str, expected: bool) -> None:
    assert is_placeholder_link(link) is expected


def test_constructor_validation_and_url_normalization() -> None:
    with pytest.raises(ValueError):
        RemnawaveBackend("http://x", TOKEN, squad_uuids=[], traffic_limit_strategy="YEARLY")
    b = RemnawaveBackend(" https://panel.example.com/api/ ", TOKEN, squad_uuids=[])
    assert b.base_url == "https://panel.example.com"
    assert TOKEN not in repr(b)
    assert b.kind is PanelKind.REMNAWAVE
    assert isinstance(b, PanelBackend)


# ---------------------------------------------------------------------- system / auth / transport


async def test_ping_returns_version_and_sends_auth_and_forwarded_headers(
    backend: RemnawaveBackend, fake: FakeRemnawave
) -> None:
    assert await backend.ping() == "3.4.8"
    assert backend.version == "3.4.8"
    headers = fake.last("GET", "/api/system/metadata")["headers"]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["X-Forwarded-For"] == "127.0.0.1"
    assert headers["X-Forwarded-Proto"] == "https"


@pytest.mark.parametrize("version", ["2.6.3", "1.0.0", "2.99.99"])
async def test_ping_rejects_versions_below_3(backend: RemnawaveBackend, fake: FakeRemnawave, version: str) -> None:
    fake.version = version
    with pytest.raises(PanelUnsupportedError, match=version):
        await backend.ping()
    assert backend.version is None


async def test_unsupported_version_blocks_every_operation(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.version = "2.7.0"
    with pytest.raises(PanelUnsupportedError):
        await backend.upsert_account(None, _spec())
    assert fake.calls() == []


async def test_ping_unparseable_version(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.version = "nightly"
    with pytest.raises(PanelError, match="unrecognized version"):
        await backend.ping()


async def test_missing_metadata_route_means_unsupported(server: TestServer) -> None:
    b = _make_backend(server)
    b.base_url += "/wrong-prefix"
    try:
        with pytest.raises(PanelUnsupportedError, match=r"3\.0"):
            await b.ping()
    finally:
        await b.close()


async def test_version_checked_once_lazily(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    await backend.get_account(None, "tg_1")
    await backend.get_account(None, "tg_2")
    assert [r["path"] for r in fake.requests].count("/api/system/metadata") == 1


async def test_bad_token_raises_auth_error(server: TestServer) -> None:
    b = _make_backend(server, api_token="wrong")
    try:
        with pytest.raises(PanelAuthError, match="401"):
            await b.ping()
    finally:
        await b.close()


async def test_missing_scope_raises_auth_error(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.forbidden_paths.add("/api/hwid/devices/delete-all")
    acc = await backend.upsert_account(None, _spec())
    with pytest.raises(PanelAuthError, match="scope"):
        await backend.reset_devices(acc)


async def test_direct_mode_required_without_proxy(server: TestServer, fake: FakeRemnawave) -> None:
    b = _make_backend(server, direct=False)
    try:
        with pytest.raises(PanelError, match="direct mode") as exc_info:
            await b.ping()
        assert not isinstance(exc_info.value, PanelAuthError)
        # Connection errors on idempotent calls are retried (aiohttp may add its own reconnect per attempt).
        assert len(fake.requests) >= len(b.retry_delays) + 1
        assert {r["path"] for r in fake.requests} == {"/api/system/metadata"}
    finally:
        await b.close()


async def test_non_direct_mode_through_proxy_sends_no_forwarded_headers(
    server: TestServer, fake: FakeRemnawave
) -> None:
    fake.require_forwarded = False
    b = _make_backend(server, direct=False, extra_headers={"X-Api-Key": "caddy-key"}, cookies={"auth": "c00kie"})
    try:
        await b.ping()
    finally:
        await b.close()
    headers = fake.requests[-1]["headers"]
    assert "X-Forwarded-For" not in headers
    assert headers["X-Api-Key"] == "caddy-key"
    assert "auth=c00kie" in headers.get("Cookie", "")


async def test_unreachable_panel_raises_panel_error() -> None:
    b = RemnawaveBackend("http://127.0.0.1:9", TOKEN, squad_uuids=[], timeout=2)
    b.retry_delays = (0.0,)
    try:
        with pytest.raises(PanelError, match="GET /api/system/metadata"):
            await b.ping()
    finally:
        await b.close()


async def test_gateway_errors_are_retried_for_idempotent_calls(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.fail_once[("GET", "/api/system/metadata")] = 503
    assert await backend.ping() == "3.4.8"
    assert [r["path"] for r in fake.requests].count("/api/system/metadata") == 2


async def test_create_is_not_retried(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.fail_once[("POST", "/api/users")] = 502
    with pytest.raises(RemnawaveApiError) as exc_info:
        await backend.upsert_account(None, _spec())
    assert exc_info.value.status == 502
    assert fake.calls("POST") == [("POST", "/api/users")]


async def test_non_json_success_body_is_an_error(server: TestServer, fake: FakeRemnawave) -> None:
    async def html(_: web.Request) -> web.Response:
        return web.Response(text="<html>login</html>", content_type="text/html")

    app = fake.app()
    app.router.add_get("/html/api/system/metadata", html)
    srv = TestServer(app)
    async with srv:
        b = _make_backend(srv)
        b.base_url += "/html"
        try:
            with pytest.raises(PanelError, match="non-JSON"):
                await b.ping()
        finally:
            await b.close()


async def test_validation_errors_are_readable(backend: RemnawaveBackend) -> None:
    await backend.ping()
    with pytest.raises(RemnawaveApiError) as exc_info:
        await backend._request("POST", "/api/users", json_body={"username": "!"}, idempotent=False)
    err = exc_info.value
    assert err.status == 400 and err.code is None
    assert "Validation failed" in str(err) and "username: Invalid username" in str(err)


async def test_close_is_idempotent_and_closed_backend_never_reopens(backend: RemnawaveBackend) -> None:
    """The registry closes replaced backends that callers may still hold: no orphan session may appear."""
    await backend.ping()
    await backend.close()
    await backend.close()
    backend._version = None
    with pytest.raises(PanelError, match="closed"):
        await backend.ping()
    assert backend._session is None


async def test_list_inbounds_is_empty(backend: RemnawaveBackend) -> None:
    assert await backend.list_inbounds() == []


# ---------------------------------------------------------------------- squads


async def test_list_squads(backend: RemnawaveBackend) -> None:
    squads = await backend.list_squads()
    assert [s.uuid for s in squads] == [SQUAD_A, SQUAD_B]
    assert squads[1].members_count == 1 and squads[1].inbounds_count == 1


async def test_empty_squad_list_means_all_squads_and_is_cached(server: TestServer, fake: FakeRemnawave) -> None:
    b = _make_backend(server, squad_uuids=[])
    try:
        await b.upsert_account(None, _spec())
        await b.upsert_account(None, _spec(email="tg_2"))
    finally:
        await b.close()
    user = fake.by_name("tg_2")
    assert user is not None
    assert {s["uuid"] for s in user["activeInternalSquads"]} == {SQUAD_A, SQUAD_B}
    assert fake.calls("GET", "/api/internal-squads") == [("GET", "/api/internal-squads")]


# ---------------------------------------------------------------------- upsert: create


async def test_upsert_creates_user_with_exact_body(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    acc = await backend.upsert_account(None, _spec())
    body = fake.last("POST", "/api/users")["json"]
    assert body == {
        "username": "tg_123456789",
        "vlessUuid": "0b9a3c0e-5f4e-4c7a-9a4b-2c1d3e4f5a6b",
        "status": "ACTIVE",
        "expireAt": "2031-12-04T00:00:00.000Z",
        "trafficLimitBytes": 107374182400,
        "trafficLimitStrategy": "NO_RESET",
        "hwidDeviceLimit": 3,
        "telegramId": 123456789,
        "description": "vpnbot sub #1",
        "activeInternalSquads": [SQUAD_A],
    }
    assert acc.email == "tg_123456789"
    assert acc.uuid == "0b9a3c0e-5f4e-4c7a-9a4b-2c1d3e4f5a6b"
    assert acc.enabled is True
    assert acc.expires_at == datetime(2031, 12, 4, tzinfo=UTC)
    assert acc.device_limit == 3
    assert acc.traffic_limit_bytes == 107374182400
    assert acc.remote_id == "1"
    assert acc.short_id == "short00000000001"
    assert acc.subscription_url == "https://panel.example.com/api/sub/short00000000001"
    assert acc.raw["status"] == "ACTIVE"


async def test_upsert_creates_disabled_user(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    acc = await backend.upsert_account(None, _spec(enabled=False))
    assert fake.last("POST", "/api/users")["json"]["status"] == "DISABLED"
    assert acc.enabled is False
    assert fake.calls("POST", "/api/users/") == []  # no extra action calls


async def test_upsert_never_expiring(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    acc = await backend.upsert_account(None, _spec(expires_at=None))
    assert fake.last("POST", "/api/users")["json"]["expireAt"] == format_datetime(NEVER_EXPIRES)
    assert acc.expires_at is None
    # Second upsert: nothing changed -> no PATCH.
    await backend.upsert_account(None, _spec(expires_at=None))
    assert fake.calls("PATCH") == []


async def test_upsert_far_future_expiry_reads_back_as_pushed(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    # A very long plan lands past 2099: the real date must come back, not "never expires", or every scan
    # sees the account as drifted.
    far = datetime(2126, 10, 11, 12, 0, tzinfo=UTC)
    acc = await backend.upsert_account(None, _spec(expires_at=far))
    assert acc.expires_at == far
    listed = await backend.get_account(None, "tg_123456789")
    assert listed is not None and listed.expires_at == far
    await backend.upsert_account(None, _spec(expires_at=far))
    assert fake.calls("PATCH") == []


async def test_upsert_create_with_past_expiry_is_allowed(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    past = datetime(2020, 1, 1, tzinfo=UTC)
    acc = await backend.upsert_account(None, _spec(expires_at=past, enabled=False))
    assert fake.last("POST", "/api/users")["json"]["expireAt"] == "2020-01-01T00:00:00.000Z"
    assert acc.expires_at == past


async def test_upsert_zero_tg_id_and_custom_strategy(server: TestServer, fake: FakeRemnawave) -> None:
    b = _make_backend(server, traffic_limit_strategy="MONTH")
    try:
        await b.upsert_account(None, _spec(tg_id=0, device_limit=0, traffic_limit_bytes=0, comment=""))
    finally:
        await b.close()
    body = fake.last("POST", "/api/users")["json"]
    assert body["telegramId"] is None
    assert body["hwidDeviceLimit"] == 0  # 0 = unlimited in both models
    assert body["trafficLimitBytes"] == 0
    assert body["trafficLimitStrategy"] == "MONTH"


@pytest.mark.parametrize("username", ["ab", "x" * 37, "tg 1", "tg.1", "тг_1", ""])
async def test_upsert_rejects_invalid_username_before_any_request(
    backend: RemnawaveBackend, fake: FakeRemnawave, username: str
) -> None:
    with pytest.raises(PanelError, match="invalid Remnawave username"):
        await backend.upsert_account(None, _spec(email=username))
    assert fake.requests == []


@pytest.mark.parametrize(
    "call",
    [
        lambda b: b.upsert_account(1, _spec()),
        lambda b: b.get_account(1, "tg_1"),
        lambda b: b.list_accounts(5),
        lambda b: b.delete_account(1, "tg_1", "u"),
    ],
)
async def test_target_must_be_none(backend: RemnawaveBackend, call: Any) -> None:
    with pytest.raises(ValueError, match="target must be None"):
        await call(backend)


async def test_create_race_a019_falls_back_to_update(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    existing = fake.add_user("tg_123456789", vlessUuid=_spec().uuid)
    fake.hide_username_once.add("tg_123456789")  # lookup misses it, create then hits A019
    acc = await backend.upsert_account(None, _spec())
    assert acc.remote_id == str(existing["id"])
    assert fake.calls("POST", "/api/users") == [("POST", "/api/users")]
    assert fake.calls("PATCH") == [("PATCH", "/api/users")]
    assert existing["hwidDeviceLimit"] == 3


async def test_a019_with_user_still_invisible_reraises(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.add_user("tg_123456789", vlessUuid=_spec().uuid)
    fake.hide_username_always.add("tg_123456789")
    with pytest.raises(RemnawaveApiError) as exc_info:
        await backend.upsert_account(None, _spec())
    assert exc_info.value.code == "A019"


# ---------------------------------------------------------------------- upsert: update


async def test_upsert_updates_only_changed_fields(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    await backend.upsert_account(None, _spec())
    new_expiry = datetime(2032, 3, 1, 12, 30, tzinfo=UTC)
    acc = await backend.upsert_account(None, _spec(expires_at=new_expiry, device_limit=5, traffic_limit_bytes=0))
    patch = fake.last("PATCH", "/api/users")["json"]
    assert patch == {
        "id": 1,
        "expireAt": "2032-03-01T12:30:00.000Z",
        "hwidDeviceLimit": 5,
        "trafficLimitBytes": 0,
    }
    assert acc.expires_at == new_expiry and acc.device_limit == 5 and acc.traffic_limit_bytes == 0


async def test_upsert_is_a_noop_when_in_sync(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    await backend.upsert_account(None, _spec())
    fake.requests.clear()
    acc = await backend.upsert_account(None, _spec())
    assert fake.calls() == [("GET", "/api/users/by-username/tg_123456789")]
    assert acc.enabled is True


async def test_upsert_changes_squads_description_and_telegram(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.add_user(
        "tg_123456789",
        vlessUuid=_spec().uuid,
        activeInternalSquads=[{"uuid": SQUAD_B, "name": "b"}],
        hwidDeviceLimit=3,
        trafficLimitBytes=100 * 1024**3,
        telegramId=1,
        description=None,
        expireAt="2031-12-04T00:00:00Z",
    )
    await backend.upsert_account(None, _spec())
    assert fake.last("PATCH", "/api/users")["json"] == {
        "id": 1,
        "telegramId": 123456789,
        "description": "vpnbot sub #1",
        "activeInternalSquads": [SQUAD_A],
    }


async def test_empty_comment_matches_null_description(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.add_user(
        "tg_123456789",
        vlessUuid=_spec().uuid,
        activeInternalSquads=[{"uuid": SQUAD_A, "name": "a"}],
        hwidDeviceLimit=3,
        trafficLimitBytes=100 * 1024**3,
        telegramId=123456789,
        description=None,
        expireAt="2031-12-04T00:00:00.000Z",
    )
    await backend.upsert_account(None, _spec(comment=""))
    assert fake.calls("PATCH") == []


async def test_upsert_disables_and_enables(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    await backend.upsert_account(None, _spec())
    acc = await backend.upsert_account(None, _spec(enabled=False))
    assert acc.enabled is False and fake.users[1]["status"] == "DISABLED"
    assert ("POST", "/api/users/1/actions/disable") in fake.calls("POST")
    acc = await backend.upsert_account(None, _spec(enabled=True))
    assert acc.enabled is True and fake.users[1]["status"] == "ACTIVE"
    assert ("POST", "/api/users/1/actions/enable") in fake.calls("POST")


async def test_already_disabled_a029_is_success(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    await backend.upsert_account(None, _spec(enabled=False))
    fake.stale_status[1] = "ACTIVE"  # our read is stale; the disable action answers A029
    acc = await backend.upsert_account(None, _spec(enabled=False))
    assert acc.enabled is False
    assert ("POST", "/api/users/1/actions/disable") in fake.calls("POST")
    assert fake.calls("GET")[-1] == ("GET", "/api/users/1")


async def test_already_enabled_a030_is_success(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    await backend.upsert_account(None, _spec())
    fake.stale_status[1] = "DISABLED"  # our read is stale; the enable action answers A030
    acc = await backend.upsert_account(None, _spec())
    assert acc.enabled is True
    assert ("POST", "/api/users/1/actions/enable") in fake.calls("POST")
    assert fake.calls("GET")[-1] == ("GET", "/api/users/1")


async def test_limited_user_is_not_force_enabled(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.add_user("tg_123456789", vlessUuid=_spec().uuid, status="LIMITED")
    acc = await backend.upsert_account(None, _spec())
    assert acc.enabled is True  # LIMITED is a runtime state, not an admin "disabled"
    assert fake.users[1]["status"] == "LIMITED"
    assert not any(path.endswith("/enable") for _, path in fake.calls("POST"))


async def test_limited_user_is_disabled_on_request(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.add_user("tg_123456789", vlessUuid=_spec().uuid, status="LIMITED")
    acc = await backend.upsert_account(None, _spec(enabled=False))
    assert acc.enabled is False and fake.users[1]["status"] == "DISABLED"


async def test_expired_user_reactivated_by_new_expiry(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.add_user("tg_123456789", vlessUuid=_spec().uuid, status="EXPIRED", expireAt="2025-01-01T00:00:00.000Z")
    acc = await backend.upsert_account(None, _spec())
    assert fake.users[1]["status"] == "ACTIVE" and acc.enabled
    assert not any(path.endswith("/enable") for _, path in fake.calls("POST"))  # PATCH alone sufficed


async def test_expired_user_with_same_expiry_is_enabled(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.add_user("tg_123456789", vlessUuid=_spec().uuid, status="EXPIRED", expireAt="2031-12-04T00:00:00.000Z")
    await backend.upsert_account(None, _spec())
    assert fake.users[1]["status"] == "ACTIVE"
    assert ("POST", "/api/users/1/actions/enable") in fake.calls("POST")


@pytest.mark.parametrize("delta", [timedelta(days=-3), timedelta(seconds=10)])
async def test_past_expiry_update_keeps_date_and_disables(
    backend: RemnawaveBackend, fake: FakeRemnawave, delta: timedelta
) -> None:
    await backend.upsert_account(None, _spec())
    acc = await backend.upsert_account(None, _spec(expires_at=datetime.now(UTC) + delta, enabled=True))
    assert all("expireAt" not in (r["json"] or {}) for r in fake.requests if r["method"] == "PATCH")
    assert acc.enabled is False and fake.users[1]["status"] == "DISABLED"
    assert fake.users[1]["expireAt"] == "2031-12-04T00:00:00.000Z"


async def test_naive_expiry_is_treated_as_utc(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    await backend.upsert_account(None, _spec())
    await backend.upsert_account(None, _spec(expires_at=datetime(2033, 1, 1)))
    assert fake.last("PATCH", "/api/users")["json"]["expireAt"] == "2033-01-01T00:00:00.000Z"


async def test_changed_vless_uuid_recreates_user(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    old = fake.add_user("tg_123456789", vlessUuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    acc = await backend.upsert_account(None, _spec())
    assert ("DELETE", f"/api/users/{old['id']}") in fake.calls("DELETE")
    assert old["id"] not in fake.users
    assert acc.uuid == _spec().uuid and acc.remote_id != str(old["id"])


async def test_uuid_comparison_is_case_insensitive(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.add_user("tg_123456789", vlessUuid=_spec().uuid.upper())
    await backend.upsert_account(None, _spec())
    assert fake.calls("DELETE") == []


async def test_patch_error_propagates(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    await backend.upsert_account(None, _spec())
    fake.fail_once[("PATCH", "/api/users")] = 500
    with pytest.raises(RemnawaveApiError) as exc_info:
        await backend.upsert_account(None, _spec(device_limit=9))
    assert exc_info.value.status == 500


# ---------------------------------------------------------------------- reads


async def test_get_account_found_and_mapped(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.add_user(
        "tg_42",
        status="LIMITED",
        hwidDeviceLimit=None,
        trafficLimitBytes=5,
        userTraffic={"usedTrafficBytes": 1234, "lifetimeUsedTrafficBytes": 9999},
    )
    acc = await backend.get_account(None, "tg_42")
    assert acc is not None
    assert acc.enabled is True and acc.device_limit == 0 and acc.traffic_limit_bytes == 5
    assert acc.used_bytes == 1234 and acc.remote_id == "1"


async def test_get_account_missing_returns_none(backend: RemnawaveBackend) -> None:
    assert await backend.get_account(None, "tg_nobody") is None


async def test_get_used_bytes(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.add_user("tg_42", userTraffic={"usedTrafficBytes": 1234, "lifetimeUsedTrafficBytes": 9999})
    assert await backend.get_used_bytes(None, "tg_42") == 1234
    assert await backend.get_used_bytes(None, "tg_nobody") is None


async def test_get_account_escapes_username(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.add_user("tg_a-b_c")
    assert await backend.get_account(None, "tg_a-b_c") is not None


async def test_unknown_route_404_is_not_treated_as_missing_user(server: TestServer) -> None:
    b = _make_backend(server)
    b._version = "3.4.8"
    b.base_url += "/nope"
    try:
        with pytest.raises(RemnawaveApiError) as exc_info:
            await b.get_account(None, "tg_1")
    finally:
        await b.close()
    assert exc_info.value.status == 404 and not exc_info.value.is_not_found


async def test_list_accounts_paginates_and_filters_prefix(server: TestServer, fake: FakeRemnawave) -> None:
    for i in range(5):
        fake.add_user(f"tg_{i}")
    fake.add_user("manual_user")
    b = _make_backend(server)
    b.page_size = 2
    try:
        accounts = await b.list_accounts(None)
    finally:
        await b.close()
    assert sorted(a.email for a in accounts) == [f"tg_{i}" for i in range(5)]
    starts = [r["query"]["start"] for r in fake.requests if r["path"] == "/api/users"]
    assert starts == ["0", "2", "4"]


async def test_list_accounts_empty_panel(backend: RemnawaveBackend) -> None:
    assert await backend.list_accounts(None) == []


async def test_list_accounts_custom_prefix(server: TestServer, fake: FakeRemnawave) -> None:
    fake.add_user("tg_1")
    fake.add_user("shop_1")
    b = _make_backend(server, username_prefix="shop_")
    try:
        assert [a.email for a in await b.list_accounts(None)] == ["shop_1"]
    finally:
        await b.close()


# ---------------------------------------------------------------------- delete


async def test_delete_account(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    await backend.upsert_account(None, _spec())
    await backend.delete_account(None, "tg_123456789", _spec().uuid)
    assert fake.users == {}
    assert ("DELETE", "/api/users/1") in fake.calls("DELETE")


async def test_delete_missing_account_is_success(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    await backend.delete_account(None, "tg_404", "u")
    assert fake.calls("DELETE") == []


async def test_delete_race_a025_is_success(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.add_user("tg_123456789")
    fake.delete_already_gone = True
    await backend.delete_account(None, "tg_123456789", "u")
    assert ("DELETE", "/api/users/1") in fake.calls("DELETE")


async def test_delete_server_error_propagates(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    fake.add_user("tg_123456789")
    fake.fail_once[("DELETE", "/api/users/1")] = 500
    with pytest.raises(RemnawaveApiError):
        await backend.delete_account(None, "tg_123456789", "u")


# ---------------------------------------------------------------------- links / devices / subscription


async def test_get_links_filters_placeholders(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    acc = await backend.upsert_account(None, _spec())
    links = await backend.get_links(acc, {"user-agent": "Happ/3"})
    assert links == [
        f"vless://{_spec().uuid}@de1.example.com:443?security=reality&type=tcp#DE-1",
        f"trojan://{_spec().uuid}@nl1.example.com:443?security=tls#NL",
    ]
    assert fake.calls("GET", "/api/subscriptions") == [("GET", "/api/subscriptions/connection-keys/1")]


async def test_get_links_with_stale_remote_id_falls_back_to_username(
    backend: RemnawaveBackend, fake: FakeRemnawave
) -> None:
    acc = await backend.upsert_account(None, _spec())
    assert len(await backend.get_links(replace(acc, remote_id="999"))) == 2


async def test_get_links_without_remote_id(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    acc = await backend.upsert_account(None, _spec())
    assert len(await backend.get_links(replace(acc, remote_id=None))) == 2


async def test_get_links_for_missing_user_is_empty(backend: RemnawaveBackend) -> None:
    ghost = RemoteAccount("tg_ghost", "u", True, None, 0, 0, remote_id="77")
    assert await backend.get_links(ghost) == []


async def test_reset_devices(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    acc = await backend.upsert_account(None, _spec())
    fake.devices[1] = ["hwid-aaaaaaaaaa"]
    await backend.reset_devices(acc)
    assert fake.devices[1] == []
    assert fake.last("POST", "/api/hwid/devices/delete-all")["json"] == {"userId": 1}


async def test_reset_traffic_lifts_traffic_limit(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    await backend.upsert_account(None, _spec())
    fake.users[1]["status"] = "LIMITED"
    await backend.reset_traffic(None, "tg_123456789")
    assert fake.last("POST", "/api/users/1/actions/reset-traffic")
    assert fake.users[1]["status"] == "ACTIVE"  # still LIMITED after the reset: enabled explicitly


async def test_reset_traffic_for_missing_user_is_noop(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    await backend.reset_traffic(None, "tg_ghost")
    assert fake.calls("POST") == []


async def test_invalidate_squads_refetches_all_squads(server: TestServer, fake: FakeRemnawave) -> None:
    backend = _make_backend(server, squad_uuids=[])  # "all squads", cached
    new_squad = "33333333-3333-4333-8333-333333333333"
    try:
        await backend.upsert_account(None, _spec())
        fake.squads.append(new_squad)
        await backend.upsert_account(None, _spec())
        assert new_squad not in str(fake.users[1]["activeInternalSquads"])  # served from the cache
        backend.invalidate_squads()
        await backend.upsert_account(None, _spec())
        assert new_squad in str(fake.users[1]["activeInternalSquads"])
    finally:
        await backend.close()


async def test_reset_devices_for_missing_user_is_noop(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    await backend.reset_devices(RemoteAccount("tg_ghost", "u", True, None, 0, 0))
    assert fake.calls("POST") == []


async def test_fetch_subscription_forwards_client_headers_only(backend: RemnawaveBackend, fake: FakeRemnawave) -> None:
    acc = await backend.upsert_account(None, _spec())
    assert acc.short_id
    body, headers = await backend.fetch_subscription(
        acc.short_id,
        {
            "User-Agent": "Happ/3.2.1",
            "X-HWID": "abc123device",
            "x-device-os": "iOS",
            "X-Ver-OS": "18.1",
            "x-device-model": "iPhone15",
            "x-remnawave-real-ip": "1.2.3.4",
            "Cookie": "session=secret",
            "Authorization": "Bearer user-supplied",
        },
    )
    sent = fake.last("GET", f"/api/sub/{acc.short_id}")["headers"]
    assert sent["User-Agent"] == "Happ/3.2.1"
    assert sent["x-hwid"] == "abc123device"
    assert sent["x-ver-os"] == "18.1"
    assert sent["x-remnawave-real-ip"] == "1.2.3.4"
    assert sent["X-Forwarded-Proto"] == "https"
    assert "Authorization" not in sent and "Cookie" not in sent
    assert base64.b64decode(body).decode().startswith(f"vless://{_spec().uuid}@")
    assert headers["profile-title"] == "base64:UmVtbmF3YXZl"
    assert headers["subscription-userinfo"].startswith("upload=0")
    assert "x-powered-by" not in headers and "etag" not in headers and "content-length" not in headers


async def test_fetch_subscription_unknown_short_id(backend: RemnawaveBackend) -> None:
    with pytest.raises(RemnawaveApiError) as exc_info:
        await backend.fetch_subscription("nope/../x")
    assert exc_info.value.status == 404
