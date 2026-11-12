"""TelemtBackend against an in-process emulation of the telemt Control API v1 (as observed on telemt 3.5.16)."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.db.models import PanelKind
from app.services.panels import telemt as telemt_module
from app.services.panels.base import AccountSpec, PanelAuthError, PanelBackend, PanelError
from app.services.panels.mtproto import MtprotoProxyBackend, ProxyUserSpec, fake_tls_secret
from app.services.panels.telemt import (
    TelemtApiError,
    TelemtBackend,
    check_username,
    from_rfc3339,
    to_rfc3339,
    user_patch,
)

AUTH = "Bearer test-token"
DOMAIN = "petrovich.ru"
KEY_A = "a" * 32
KEY_B = "b" * 32
TAG = "c" * 32
BODY: web.RequestKey[dict[str, Any]] = web.RequestKey("body", dict)


class FakeTelemt:
    def __init__(self, *, read_only: bool = False, public_host: str = "proxy.example.com") -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.secrets: dict[str, str] = {}
        self.read_only = read_only
        self.public_host = public_host
        self.calls: list[tuple[str, str, Any]] = []
        self.revision = 0

    def ok(self, data: Any, status: int = 200) -> web.Response:
        self.revision += 1
        return web.json_response({"ok": True, "data": data, "revision": f"{self.revision:064x}"}, status=status)

    @staticmethod
    def err(status: int, code: str, message: str = "") -> web.Response:
        body = {"ok": False, "error": {"code": code, "message": message}, "request_id": 1}
        return web.json_response(body, status=status)

    def view(self, name: str) -> dict[str, Any]:
        user = dict(self.users[name])
        secret = fake_tls_secret(self.secrets[name], DOMAIN)
        user["links"] = {
            "classic": [],
            "secure": [],
            "tls": [f"tg://proxy?server={self.public_host}&port=443&secret={secret}"],
            "tls_domains": [],
        }
        return user

    def seed(self, name: str, key: str = KEY_A, **fields: Any) -> None:
        self.users[name] = {
            "username": name,
            "enabled": True,
            "in_runtime": True,
            "user_ad_tag": None,
            "max_tcp_conns": None,
            "expiration_rfc3339": None,
            "data_quota_bytes": None,
            "quota_period": None,
            "rate_limit_up_bps": None,
            "rate_limit_down_bps": None,
            "max_unique_ips": None,
            "current_connections": 0,
            "active_unique_ips": 0,
            "active_unique_ips_list": [],
            "recent_unique_ips": 0,
            "recent_unique_ips_list": [],
            "total_octets": 0,
            **fields,
        }
        self.secrets[name] = key

    @web.middleware
    async def middleware(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        body = await request.json() if request.can_read_body else None
        self.calls.append((request.method, request.path, body))
        if request.headers.get("Authorization") != AUTH:
            return self.err(401, "unauthorized", "Missing or invalid Authorization header")
        if self.read_only and request.method != "GET":
            return self.err(403, "read_only", "API runs in read-only mode")
        request[BODY] = body if isinstance(body, dict) else {}
        return await handler(request)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self.middleware])
        app.router.add_get("/v1/health", self.health)
        app.router.add_get("/v1/system/info", self.info)
        app.router.add_get("/v1/users", self.list_users)
        app.router.add_post("/v1/users", self.create_user)
        app.router.add_get("/v1/users/{name}", self.get_user)
        app.router.add_patch("/v1/users/{name}", self.patch_user)
        app.router.add_delete("/v1/users/{name}", self.delete_user)
        app.router.add_post("/v1/users/{name}/{action}", self.user_action)
        return app

    async def health(self, request: web.Request) -> web.Response:
        return self.ok({"status": "ok", "read_only": self.read_only})

    async def info(self, request: web.Request) -> web.Response:
        return self.ok({"version": "3.5.16", "uptime_seconds": 1.0})

    async def list_users(self, request: web.Request) -> web.Response:
        return self.ok([self.view(n) for n in sorted(self.users)])

    async def get_user(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in self.users:
            return self.err(404, "not_found", "User not found")
        return self.ok(self.view(name))

    async def create_user(self, request: web.Request) -> web.Response:
        body = request[BODY].copy()
        name = body.pop("username")
        if name in self.users:
            return self.err(409, "user_exists", "User already exists")
        key = body.pop("secret", None) or secrets.token_hex(16)
        enabled = body.pop("enabled", True)
        self.seed(name, key, enabled=enabled, in_runtime=False, **body)
        return self.ok({"user": self.view(name), "secret": key}, status=201)

    async def patch_user(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in self.users:
            return self.err(404, "not_found", "User not found")
        body = request[BODY].copy()
        if "secret" in body:
            self.secrets[name] = body.pop("secret")
        if "enabled" in body:
            body["enabled"] = body["enabled"] is not False
        self.users[name].update(body)
        return self.ok(self.view(name))

    async def delete_user(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in self.users:
            return self.err(404, "not_found", "User not found")
        if len(self.users) == 1:
            return self.err(409, "last_user_forbidden", "Cannot delete the last configured user")
        del self.users[name]
        return self.ok({"username": name, "in_runtime": True})

    async def user_action(self, request: web.Request) -> web.Response:
        name, action = request.match_info["name"], request.match_info["action"]
        if name not in self.users:
            return self.err(404, "not_found", "User not found")
        if action == "rotate-secret":
            body = request[BODY]
            self.secrets[name] = body.get("secret") or secrets.token_hex(16)
            return self.ok({"user": self.view(name), "secret": self.secrets[name]})
        if action in ("enable", "disable"):
            self.users[name]["enabled"] = action == "enable"
            return self.ok(self.view(name))
        if action == "reset-quota":
            self.users[name]["total_octets"] = 0
            return self.ok({"username": name, "used_bytes": 0, "last_reset_epoch_secs": 1})
        return self.err(404, "not_found", "")


Started = Callable[..., Awaitable[tuple[FakeTelemt, TelemtBackend]]]


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemt_module, "_RETRY_BASE_DELAY", 0)


@pytest.fixture
async def start() -> AsyncIterator[Started]:
    servers: list[TestServer] = []
    backends: list[TelemtBackend] = []

    async def _start(fake: FakeTelemt | None = None, **kw: Any) -> tuple[FakeTelemt, TelemtBackend]:
        fake = fake or FakeTelemt()
        server = TestServer(fake.app())
        await server.start_server()
        servers.append(server)
        kw.setdefault("auth_header", AUTH)
        backend = TelemtBackend(str(server.make_url("")), timeout=5, **kw)
        backends.append(backend)
        return fake, backend

    yield _start
    for backend in backends:
        await backend.close()
    for server in servers:
        await server.close()


def spec(**overrides: Any) -> ProxyUserSpec:
    values: dict[str, Any] = {"name": "tg42", "enabled": True, "key": KEY_A}
    values.update(overrides)
    return ProxyUserSpec(**values)


def mutations(fake: FakeTelemt) -> list[tuple[str, str, Any]]:
    return [c for c in fake.calls if c[0] != "GET"]


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def test_rfc3339_round_trip() -> None:
    moment = datetime(2026, 12, 1, 10, 0, 5, 999, tzinfo=UTC)
    assert to_rfc3339(moment) == "2026-12-01T10:00:05Z"
    assert from_rfc3339("2026-12-01T10:00:05Z") == moment.replace(microsecond=0)
    assert from_rfc3339("2026-12-01T13:00:05+03:00") == moment.replace(microsecond=0)
    assert from_rfc3339("garbage") is None and from_rfc3339(None) is None
    with pytest.raises(ValueError):
        to_rfc3339(datetime(2026, 1, 1))


@pytest.mark.parametrize("name", ["", "a b", "x" * 65, "имя", "a/b"])
def test_username_validation(name: str) -> None:
    with pytest.raises(PanelError):
        check_username(name)


def test_patch_is_minimal() -> None:
    user = {"username": "tg42", "enabled": True, "links": {"tls": []}}
    assert user_patch(user, spec(key=None)) == {}
    full = user_patch(
        {**user, "max_unique_ips": 5, "data_quota_bytes": 10, "quota_period": "monthly", "user_ad_tag": TAG},
        spec(key=None, max_unique_ips=0, ad_tag=None),
    )
    assert full == {"max_unique_ips": None, "data_quota_bytes": None, "quota_period": None, "user_ad_tag": None}


def test_backend_validates_url() -> None:
    with pytest.raises(ValueError):
        TelemtBackend("localhost:9091")


# ---------------------------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------------------------
async def test_implements_both_protocols(start: Started) -> None:
    _, backend = await start()
    assert isinstance(backend, PanelBackend) and isinstance(backend, MtprotoProxyBackend)
    assert backend.kind == PanelKind.TELEMT
    assert await backend.list_inbounds() == []


async def test_ping_returns_version(start: Started) -> None:
    _, backend = await start()
    assert await backend.ping() == "3.5.16"


async def test_ping_rejects_read_only_api(start: Started) -> None:
    _, backend = await start(FakeTelemt(read_only=True))
    with pytest.raises(PanelError, match="read-only"):
        await backend.ping()


async def test_bad_auth_header(start: Started) -> None:
    _, backend = await start(auth_header="Bearer wrong")
    with pytest.raises(PanelAuthError):
        await backend.ping()


async def test_non_api_answer_is_reported(start: Started) -> None:
    fake = FakeTelemt()
    app = web.Application()

    async def nginx(request: web.Request) -> web.Response:
        return web.Response(text="<html>nginx</html>")

    app.router.add_get("/v1/health", nginx)
    server = TestServer(app)
    await server.start_server()
    backend = TelemtBackend(str(server.make_url("")), auth_header=AUTH)
    try:
        with pytest.raises(PanelError, match="not a Control API answer"):
            await backend.ping()
    finally:
        await backend.close()
        await server.close()
    assert fake.calls == []


async def test_create_sends_only_set_fields(start: Started) -> None:
    fake, backend = await start()
    expires = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    account = await backend.upsert_proxy_user(
        None, spec(expires_at=expires, max_unique_ips=3, rate_limit_bps=2_000_000, ad_tag=TAG.upper())
    )
    assert mutations(fake) == [
        (
            "POST",
            "/v1/users",
            {
                "username": "tg42",
                "secret": KEY_A,
                "expiration_rfc3339": "2026-12-31T23:59:00Z",
                "max_unique_ips": 3,
                "rate_limit_up_bps": 2_000_000,
                "rate_limit_down_bps": 2_000_000,
                "user_ad_tag": TAG,
            },
        )
    ]
    assert account.key == KEY_A and account.enabled and account.expires_at == expires
    assert account.max_unique_ips == 3 and account.rate_limit_bps == 2_000_000 and account.ad_tag == TAG
    assert [link.tg_url for link in account.links] == [
        f"tg://proxy?server=proxy.example.com&port=443&secret={fake_tls_secret(KEY_A, DOMAIN)}"
    ]


async def test_create_disabled_and_generated_secret(start: Started) -> None:
    fake, backend = await start()
    account = await backend.upsert_proxy_user(None, spec(enabled=False, key=None))
    assert mutations(fake)[0][2] == {"username": "tg42", "enabled": False}
    assert account.key == fake.secrets["tg42"] and not account.enabled


async def test_upsert_is_idempotent(start: Started) -> None:
    fake, backend = await start()
    wanted = spec(expires_at=datetime(2027, 1, 1, tzinfo=UTC), max_unique_ips=2, ad_tag=TAG)
    await backend.upsert_proxy_user(None, wanted)
    fake.calls.clear()
    account = await backend.upsert_proxy_user(None, wanted)
    assert mutations(fake) == [] and account.key == KEY_A


async def test_update_patches_changed_fields_only(start: Started) -> None:
    fake, backend = await start()
    fake.seed("tg42", KEY_A, max_unique_ips=3, user_ad_tag=TAG, rate_limit_down_bps=10, rate_limit_up_bps=10)
    account = await backend.upsert_proxy_user(None, spec(enabled=False, max_unique_ips=3))
    assert mutations(fake) == [
        (
            "PATCH",
            "/v1/users/tg42",
            {"enabled": False, "rate_limit_up_bps": None, "rate_limit_down_bps": None, "user_ad_tag": None},
        )
    ]
    assert not account.enabled and account.ad_tag is None and account.rate_limit_bps == 0


async def test_new_key_is_patched_as_secret(start: Started) -> None:
    fake, backend = await start()
    fake.seed("tg42", KEY_A)
    account = await backend.upsert_proxy_user(None, spec(key=KEY_B))
    assert mutations(fake) == [("PATCH", "/v1/users/tg42", {"secret": KEY_B})]
    assert account.key == KEY_B and fake.secrets["tg42"] == KEY_B


async def test_concurrent_create_falls_back_to_patch(start: Started, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, backend = await start()
    original = backend._get_user
    calls = 0

    async def racing_get(name: str) -> dict[str, Any] | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            fake.seed(name, KEY_B)  # someone else creates it right after our read
            return None
        return await original(name)

    monkeypatch.setattr(backend, "_get_user", racing_get)
    account = await backend.upsert_proxy_user(None, spec())
    assert [c[0] for c in mutations(fake)] == ["POST", "PATCH"] and account.key == KEY_A


async def test_get_list_and_usage(start: Started) -> None:
    fake, backend = await start()
    fake.seed("tg1", KEY_A, total_octets=1234, recent_unique_ips=4, current_connections=2)
    fake.seed("tg2", KEY_B)
    users = await backend.list_proxy_users(None)
    assert [u.name for u in users] == ["tg1", "tg2"]
    assert users[0].used_bytes == 1234 and users[0].recent_unique_ips == 4 and users[0].current_connections == 2
    assert (await backend.get_proxy_user(None, "tg2")).key == KEY_B  # type: ignore[union-attr]
    assert await backend.get_proxy_user(None, "nobody") is None
    assert await backend.get_used_bytes(None, "tg1") == 1234
    assert await backend.get_used_bytes(None, "nobody") is None


async def test_public_host_override(start: Started) -> None:
    fake, backend = await start(FakeTelemt(public_host="UNKNOWN"), public_host="1.2.3.4", public_port=8443)
    fake.seed("tg1", KEY_A)
    account = await backend.get_proxy_user(None, "tg1")
    assert account is not None and [(x.server, x.port) for x in account.links] == [("1.2.3.4", 8443)]


async def test_delete_is_idempotent_and_keeps_last_user_disabled(start: Started) -> None:
    fake, backend = await start()
    fake.seed("tg1", KEY_A)
    fake.seed("tg2", KEY_B)
    await backend.delete_proxy_user(None, "tg1")
    await backend.delete_proxy_user(None, "tg1")
    assert list(fake.users) == ["tg2"]
    await backend.delete_proxy_user(None, "tg2")  # telemt refuses to delete its last user
    assert fake.users["tg2"]["enabled"] is False


async def test_rotate_secret(start: Started) -> None:
    fake, backend = await start()
    fake.seed("tg1", KEY_A)
    rotated = await backend.rotate_secret("tg1")
    assert rotated.key == fake.secrets["tg1"] != KEY_A
    chosen = await backend.rotate_secret("tg1", KEY_B)
    assert chosen.key == KEY_B
    with pytest.raises(PanelError):
        await backend.rotate_secret("tg1", "nope")
    with pytest.raises(TelemtApiError) as info:
        await backend.rotate_secret("nobody")
    assert info.value.not_found


async def test_invalid_spec_values_are_rejected_before_any_call(start: Started) -> None:
    fake, backend = await start()
    with pytest.raises(PanelError):
        await backend.upsert_proxy_user(None, spec(key="short"))
    with pytest.raises(PanelError):
        await backend.upsert_proxy_user(None, spec(ad_tag="zz"))
    with pytest.raises(PanelError):
        await backend.upsert_proxy_user(None, spec(name="bad name"))
    assert fake.calls == []


async def test_generic_panel_backend_mapping(start: Started) -> None:
    fake, backend = await start()
    account_spec = AccountSpec(
        email="tg7",
        uuid="ignored",
        enabled=True,
        expires_at=None,
        device_limit=2,
        traffic_limit_bytes=5 * 1024**3,
        tg_id=7,
        sub_id="",
    )
    remote = await backend.upsert_account(None, account_spec)
    assert remote.email == "tg7" and remote.device_limit == 2 and remote.traffic_limit_bytes == 5 * 1024**3
    assert remote.uuid == fake.secrets["tg7"]
    assert [a.email for a in await backend.list_accounts(None)] == ["tg7"]
    got = await backend.get_account(None, "tg7")
    assert got is not None
    links = await backend.get_links(got)
    assert links and links[0].startswith("tg://proxy?")
    fake.users["tg7"]["total_octets"] = 99
    await backend.reset_traffic(None, "tg7")
    await backend.reset_traffic(None, "nobody")
    assert fake.users["tg7"]["total_octets"] == 0
    fake.calls.clear()
    await backend.reset_devices(got)
    assert [c[1] for c in mutations(fake)] == ["/v1/users/tg7/disable", "/v1/users/tg7/enable"]
    fake.seed("other", KEY_B)
    await backend.delete_account(None, "tg7", "ignored")
    assert await backend.get_account(None, "tg7") is None


async def test_network_errors_are_panel_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = TelemtBackend("http://127.0.0.1:9", auth_header=AUTH, timeout=1)
    try:
        with pytest.raises(PanelError, match="telemt GET /v1/health"):
            await backend.ping()
    finally:
        await backend.close()
    with pytest.raises(PanelError, match="closed"):
        await backend.ping()


async def test_get_is_retried_after_connect_errors(start: Started, monkeypatch: pytest.MonkeyPatch) -> None:
    _, backend = await start()
    real = backend._session().request
    failures = 2

    def flaky(method: str, url: str, **kw: Any) -> Any:
        nonlocal failures
        if failures:
            failures -= 1
            raise aiohttp.ClientConnectionError("reset")
        return real(method, url, **kw)

    monkeypatch.setattr(backend._session(), "request", flaky)
    assert await backend.ping() == "3.5.16"


async def test_post_is_not_retried_after_send_errors(start: Started, monkeypatch: pytest.MonkeyPatch) -> None:
    _, backend = await start()
    attempts = 0

    def broken(method: str, url: str, **kw: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise aiohttp.ServerDisconnectedError()

    monkeypatch.setattr(backend._session(), "request", broken)
    with pytest.raises(PanelError):
        await backend.set_enabled("tg1", False)
    assert attempts == 1
