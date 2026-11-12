"""XuiBackend against an in-process emulation of both 3x-ui API generations.

`FakeXui` reproduces the behaviour recorded live on 3x-ui v3.11.0 (modern client API, CSRF, Bearer tokens)
and v2.9.4 (legacy client API inside inbound settings): envelopes, status codes for unauthenticated calls,
JSON-string vs object inbound fields, full-replace updates and the "last client" deletion refusal.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.db.models import PanelKind
from app.services.panels import xui as xui_module
from app.services.panels.base import AccountSpec, PanelAuthError, PanelError, PanelUnsupportedError, RemoteAccount
from app.services.panels.xui import (
    ApiGeneration,
    XuiApiError,
    XuiBackend,
    XuiRouteNotFoundError,
    build_client,
    from_expiry_ms,
    normalize_link_params,
    parse_inbound,
    to_expiry_ms,
    xhttp_extra,
)
from app.services.provisioning import XUI_EMAIL_RE

Generation = Literal["modern", "legacy"]
BASE = "/s3cr3t"
USER, PASSWORD, TOKEN = "admin", "pa$$word", "tok-1234567890"
PUBKEY = "liF7tVzvv9JeofLDtCNnCVrEmdikc9FvdOggyqgXml0"

REALITY_STREAM: dict[str, Any] = {
    "network": "tcp",
    "security": "reality",
    "externalProxy": [],
    "realitySettings": {
        "show": False,
        "xver": 0,
        "target": "www.google.com:443",
        "serverNames": ["www.google.com", "google.com"],
        "privateKey": "qA7u-6orT8t-E30Xn60LL1Br4vaLoutmTr8oBjTJSFk",
        "shortIds": ["6ba85179e30d4fc2", "ab"],
        "mldsa65Seed": "",
        "settings": {
            "publicKey": PUBKEY,
            "fingerprint": "chrome",
            "serverName": "",
            "spiderX": "/",
            "mldsa65Verify": "",
        },
    },
    "tcpSettings": {"acceptProxyProtocol": False, "header": {"type": "none"}},
}
SNIFFING = {"enabled": True, "destOverride": ["http", "tls", "quic"], "metadataOnly": False, "routeOnly": False}


# ---------------------------------------------------------------------------------------------
# 3x-ui emulator
# ---------------------------------------------------------------------------------------------
def ok(obj: Any = None, msg: str = "") -> web.Response:
    return web.json_response({"success": True, "msg": msg, "obj": obj})


def fail(msg: str) -> web.Response:
    return web.json_response({"success": False, "msg": msg, "obj": None})


class FakeXui:
    """Minimal but faithful 3x-ui: auth layer + inbound and client endpoints of one generation."""

    def __init__(self, generation: Generation, *, token: str | None = None) -> None:
        self.generation = generation
        self.modern = generation == "modern"
        self.csrf_enabled = self.modern
        self.token = token if self.modern else None
        self.sessions: dict[str, dict[str, Any]] = {}  # cookie -> {"csrf": str, "auth": bool}
        self.logins = 0
        self.calls: list[tuple[str, str, Any]] = []  # (method, path below BASE, json body)
        self.inbounds: dict[int, dict[str, Any]] = {}
        self.clients: dict[str, dict[str, Any]] = {}  # modern: email -> {"client": ..., "inboundIds": [...]}
        self.traffic: dict[str, tuple[int, int]] = {}
        self.cleared_ips: list[str] = []
        self.add_inbound(1, 44333)

    # --- state helpers ----------------------------------------------------------------------------
    def add_inbound(self, inbound_id: int, port: int, stream: dict[str, Any] | None = None, **extra: Any) -> None:
        self.inbounds[inbound_id] = {
            "id": inbound_id,
            "up": 0,
            "down": 0,
            "total": 0,
            "remark": f"in-{port}",
            "enable": True,
            "expiryTime": 0,
            "listen": "",
            "port": port,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none", "encryption": "none", "fallbacks": []},
            "streamSettings": stream or REALITY_STREAM,
            "sniffing": SNIFFING,
            "tag": f"in-{port}-tcp",
            **extra,
        }

    def expire_sessions(self) -> None:
        for session in self.sessions.values():
            session["auth"] = False

    def rotate_csrf(self) -> None:
        for session in self.sessions.values():
            session["csrf"] = secrets.token_urlsafe(8)

    def raw_client(self, email: str) -> dict[str, Any] | None:
        if self.modern:
            entry = self.clients.get(email)
            return entry["client"] if entry else None
        for inbound in self.inbounds.values():
            for client in inbound["settings"]["clients"]:
                if client["email"] == email:
                    return client
        return None

    def seed_client(self, inbound_id: int, client: dict[str, Any], *, used: tuple[int, int] = (0, 0)) -> None:
        """Insert a client directly (``client`` in write shape: ``id`` = uuid)."""
        self.traffic[client["email"]] = used
        if self.modern:
            stored = self._modern_read_shape(client)
            entry = self.clients.setdefault(client["email"], {"client": stored, "inboundIds": []})
            entry["inboundIds"].append(inbound_id)
        else:
            self.inbounds[inbound_id]["settings"]["clients"].append(dict(client))

    @staticmethod
    def _modern_read_shape(client: dict[str, Any]) -> dict[str, Any]:
        stored = {k: v for k, v in client.items() if k != "id"}
        stored.update(
            id=1,
            uuid=client.get("id", ""),
            allowedIPs="",
            limitHwid=client.get("limitHwid", 0),
            group=client.get("group", ""),
            createdAt=1,
            updatedAt=1,
        )
        return stored

    # --- aiohttp plumbing ---------------------------------------------------------------------------
    def upgrade_to_modern(self) -> None:
        """Simulate an in-place panel upgrade v2 -> v3: clients move to their own table, sessions reset."""
        for inbound in self.inbounds.values():
            for client in inbound["settings"]["clients"]:
                entry = self.clients.setdefault(
                    client["email"], {"client": self._modern_read_shape(client), "inboundIds": []}
                )
                entry["inboundIds"].append(inbound["id"])
            inbound["settings"]["clients"] = []
        self.generation, self.modern, self.csrf_enabled = "modern", True, True
        self.sessions.clear()

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._record, self._auth])
        get, post = app.router.add_get, app.router.add_post
        api = f"{BASE}/panel/api"
        get(f"{BASE}/", self._route("index"))
        get(f"{BASE}/csrf-token", self._route("csrf_token"))
        post(f"{BASE}/login", self._route("login"))
        get(f"{api}/server/status", self._route("status"))
        get(f"{api}/inbounds/list", self._route("inbounds_list"))
        get(f"{api}/inbounds/get/{{id}}", self._route("inbound_get"))
        get(f"{api}/clients/get/{{email}}", self._route("m_get", "modern"))
        get(f"{api}/clients/list", self._route("m_list", "modern"))
        post(f"{api}/clients/add", self._route("m_add", "modern"))
        post(f"{api}/clients/update/{{email}}", self._route("m_update", "modern"))
        post(f"{api}/clients/del/{{email}}", self._route("m_del", "modern"))
        post(f"{api}/clients/{{email}}/attach", self._route("m_attach", "modern"))
        post(f"{api}/clients/{{email}}/detach", self._route("m_detach", "modern"))
        post(f"{api}/clients/clearIps/{{email}}", self._route("clear_ips", "modern"))
        post(f"{api}/clients/resetTraffic/{{email}}", self._route("reset_traffic", "modern"))
        post(f"{api}/inbounds/{{id}}/resetClientTraffic/{{email}}", self._route("reset_traffic", "legacy"))
        post(f"{api}/inbounds/addClient", self._route("l_add", "legacy"))
        post(f"{api}/inbounds/updateClient/{{uuid}}", self._route("l_update", "legacy"))
        post(f"{api}/inbounds/{{id}}/delClient/{{uuid}}", self._route("l_del", "legacy"))
        get(f"{api}/inbounds/getClientTraffics/{{email}}", self._route("l_traffic", "legacy"))
        post(f"{api}/inbounds/update/{{id}}", self._route("l_inbound_update", "legacy"))
        post(f"{api}/inbounds/clearClientIps/{{email}}", self._route("clear_ips", "legacy"))
        return app

    def _route(self, name: str, only: Generation | None = None) -> Callable[[web.Request], Awaitable[web.Response]]:
        """Late-bound handler (tests may monkeypatch it) that 404s when the generation lacks the route."""

        async def handler(request: web.Request) -> web.Response:
            if only is not None and only != self.generation:
                raise web.HTTPNotFound()
            return await getattr(self, name)(request)

        return handler

    @web.middleware
    async def _record(self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        body = None
        if request.method == "POST" and request.content_type == "application/json":
            body = await request.json()
        self.calls.append((request.method, request.path.removeprefix(BASE), body))
        return await handler(request)

    def _session(self, request: web.Request) -> dict[str, Any] | None:
        return self.sessions.get(request.cookies.get("3x-ui", ""))

    @web.middleware
    async def _auth(self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        if not request.path.startswith(f"{BASE}/panel/api/"):
            return await handler(request)
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if bearer:
            if not self.modern:
                raise web.HTTPNotFound()
            if bearer != self.token:
                raise web.HTTPUnauthorized()
            return await handler(request)
        session = self._session(request)
        if not session or not session["auth"]:
            hidden = not self.modern or request.headers.get("X-Requested-With") != "XMLHttpRequest"
            raise web.HTTPNotFound() if hidden else web.HTTPUnauthorized()
        if self.csrf_enabled and request.method == "POST" and request.headers.get("X-CSRF-Token") != session["csrf"]:
            raise web.HTTPForbidden()
        return await handler(request)

    # --- auth endpoints -------------------------------------------------------------------------------
    async def index(self, request: web.Request) -> web.Response:
        version = "3.11.0" if self.modern else "2.9.4"
        html = f'<html><link rel="stylesheet" href="/assets/css/custom.min.css?{version}"></html>'
        return web.Response(text=html, content_type="text/html")

    async def csrf_token(self, request: web.Request) -> web.Response:
        if not self.csrf_enabled:
            raise web.HTTPNotFound()
        cookie = request.cookies.get("3x-ui")
        if cookie not in self.sessions:
            cookie = secrets.token_hex(8)
            self.sessions[cookie] = {"csrf": secrets.token_urlsafe(8), "auth": False}
        resp = web.json_response({"obj": self.sessions[cookie]["csrf"], "success": True})
        resp.set_cookie("3x-ui", cookie)
        return resp

    async def login(self, request: web.Request) -> web.Response:
        cookie = request.cookies.get("3x-ui", "")
        if self.csrf_enabled:
            session = self.sessions.get(cookie)
            if not session or request.headers.get("X-CSRF-Token") != session["csrf"]:
                raise web.HTTPForbidden()
        form = await request.post()
        if form.get("username") != USER or form.get("password") != PASSWORD:
            return fail("Invalid username or password or two-factor code.")
        if cookie not in self.sessions:
            cookie = secrets.token_hex(8)
            self.sessions[cookie] = {"csrf": "", "auth": False}
        self.sessions[cookie]["auth"] = True
        self.logins += 1
        resp = ok(msg="You have successfully logged into your account.")
        resp.set_cookie("3x-ui", cookie)
        return resp

    async def status(self, request: web.Request) -> web.Response:
        obj: dict[str, Any] = {"cpu": 1.5, "xray": {"state": "running", "version": "26.10.21"}}
        if self.modern:
            obj["panelVersion"] = "3.11.0"
        return ok(obj)

    # --- inbounds ------------------------------------------------------------------------------------
    def _inbound_view(self, inbound: dict[str, Any], *, with_stats: bool) -> dict[str, Any]:
        view = dict(inbound)
        emails = [c["email"] for c in inbound["settings"]["clients"]]
        if self.modern:
            emails = [e for e, entry in self.clients.items() if inbound["id"] in entry["inboundIds"]]
            view["settings"] = {**inbound["settings"], "clients": [self.clients[e]["client"] for e in emails]}
        else:
            for name in ("settings", "streamSettings", "sniffing"):
                view[name] = json.dumps(inbound[name], indent=2)
        stats = [
            {"email": e, "up": self.traffic.get(e, (0, 0))[0], "down": self.traffic.get(e, (0, 0))[1]} for e in emails
        ]
        view["clientStats"] = stats if with_stats or self.modern else None
        return view

    async def inbounds_list(self, request: web.Request) -> web.Response:
        return ok([self._inbound_view(i, with_stats=True) for i in self.inbounds.values()])

    async def inbound_get(self, request: web.Request) -> web.Response:
        inbound = self.inbounds.get(int(request.match_info["id"]))
        if inbound is None:
            return fail("Obtain (record not found)")
        return ok(self._inbound_view(inbound, with_stats=False))

    async def clear_ips(self, request: web.Request) -> web.Response:
        self.cleared_ips.append(request.match_info["email"])
        return ok(msg="The log has been cleared.")

    async def reset_traffic(self, request: web.Request) -> web.Response:
        email = request.match_info["email"]
        if self.raw_client(email) is None:
            return fail("Something went wrong (record not found)")
        self.traffic[email] = (0, 0)
        return ok(msg="Traffic has been reset.")

    # --- modern clients -------------------------------------------------------------------------------
    async def m_get(self, request: web.Request) -> web.Response:
        entry = self.clients.get(request.match_info["email"])
        if entry is None:
            return fail("Obtain (record not found)")
        email = entry["client"]["email"]
        used = sum(self.traffic.get(email, (0, 0)))
        return ok({"client": entry["client"], "inboundIds": entry["inboundIds"], "usedTraffic": used})

    async def m_list(self, request: web.Request) -> web.Response:
        items = []
        for email, entry in self.clients.items():
            up, down = self.traffic.get(email, (0, 0))
            items.append({**entry["client"], "inboundIds": entry["inboundIds"], "traffic": {"up": up, "down": down}})
        return ok(items)

    async def m_add(self, request: web.Request) -> web.Response:
        body = await request.json()
        client = body["client"]
        if not isinstance(client.get("tgId", 0), int):
            return fail("json: cannot unmarshal string into Go struct field .tgId of type int64")
        for inbound_id in body["inboundIds"]:
            if inbound_id not in self.inbounds:
                return fail(f"Something went wrong (inbound {inbound_id}: record not found)")
        if client["email"] in self.clients:
            return fail(f"Something went wrong (email already in use: {client['email']}\n)")
        self.clients[client["email"]] = {"client": self._modern_read_shape(client), "inboundIds": body["inboundIds"]}
        return ok(msg="Inbound client(s) have been added.")

    async def m_update(self, request: web.Request) -> web.Response:
        body = await request.json()
        if isinstance(body.get("allowedIPs"), str):
            return fail("Something went wrong (json: cannot unmarshal string into Go struct field .allowedIPs)")
        entry = self.clients.get(request.match_info["email"])
        if entry is None:
            return fail("Obtain (record not found)")
        if not body.get("email"):
            return fail("client email is required")
        entry["client"] = self._modern_read_shape(body)  # full replace: omitted fields are lost
        return ok(msg="Inbound client has been updated.")

    async def m_del(self, request: web.Request) -> web.Response:
        email = request.match_info["email"]
        if self.clients.pop(email, None) is None:
            return fail(f'Something went wrong (client "{email}" not found in any inbound or client record\n)')
        return ok(msg="Inbound client has been deleted.")

    async def m_attach(self, request: web.Request) -> web.Response:
        entry = self.clients[request.match_info["email"]]
        entry["inboundIds"] = sorted(set(entry["inboundIds"]) | set((await request.json())["inboundIds"]))
        return ok()

    async def m_detach(self, request: web.Request) -> web.Response:
        entry = self.clients[request.match_info["email"]]
        entry["inboundIds"] = [i for i in entry["inboundIds"] if i not in (await request.json())["inboundIds"]]
        return ok()

    # --- legacy clients --------------------------------------------------------------------------------
    @staticmethod
    async def _legacy_body(request: web.Request) -> tuple[int, list[dict[str, Any]]]:
        body = await request.json()
        return int(body["id"]), json.loads(body["settings"])["clients"]

    async def l_add(self, request: web.Request) -> web.Response:
        inbound_id, clients = await self._legacy_body(request)
        stored = {c["email"].lower() for i in self.inbounds.values() for c in i["settings"]["clients"]}
        for client in clients:
            if client["email"].lower() in stored:  # 3x-ui compares emails case-insensitively
                return fail(f"Something went wrong (Duplicate email: {client['email']}\n)")
        self.inbounds[inbound_id]["settings"]["clients"].extend(clients)
        return ok(msg="Inbound client(s) have been added.")

    async def l_update(self, request: web.Request) -> web.Response:
        inbound_id, (client,) = await self._legacy_body(request)
        stored = self.inbounds[inbound_id]["settings"]["clients"]
        for index, existing in enumerate(stored):
            if existing["id"] == request.match_info["uuid"]:
                stored[index] = client
                return ok(msg="Inbound client has been updated.")
        return fail("Something went wrong (empty client ID\n)")

    async def l_del(self, request: web.Request) -> web.Response:
        stored = self.inbounds[int(request.match_info["id"])]["settings"]["clients"]
        uuid = request.match_info["uuid"]
        if not any(c["id"] == uuid for c in stored):
            return fail(f"Something went wrong (Client Not Found In Inbound For ID: {uuid}\n)")
        if len(stored) == 1:
            return fail("Something went wrong (no client remained in Inbound\n)")
        stored[:] = [c for c in stored if c["id"] != uuid]
        return ok(msg="Inbound client has been deleted.")

    async def l_traffic(self, request: web.Request) -> web.Response:
        email = request.match_info["email"]
        inbound_id = next(
            (i["id"] for i in self.inbounds.values() if any(c["email"] == email for c in i["settings"]["clients"])),
            None,
        )
        if inbound_id is None:
            return fail(f"Error getting traffics. (Inbound Not Found For Email: {email}\n)")
        up, down = self.traffic.get(email, (0, 0))
        return ok({"id": 1, "inboundId": inbound_id, "email": email, "up": up, "down": down, "enable": True})

    async def l_inbound_update(self, request: web.Request) -> web.Response:
        body = await request.json()
        assert all(isinstance(body[k], str) for k in ("settings", "streamSettings", "sniffing"))
        inbound = self.inbounds[int(request.match_info["id"])]
        inbound["settings"] = json.loads(body["settings"])
        return ok(msg="Inbound has been successfully updated.")


# ---------------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------------
Started = Callable[..., Awaitable[tuple[FakeXui, XuiBackend]]]


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xui_module, "_RETRY_BASE_DELAY", 0)


@pytest.fixture
async def start() -> AsyncIterator[Started]:
    servers: list[TestServer] = []
    backends: list[XuiBackend] = []

    async def _start(
        generation: Generation = "modern", *, fake: FakeXui | None = None, **backend_kw: Any
    ) -> tuple[FakeXui, XuiBackend]:
        fake = fake or FakeXui(generation, token=TOKEN)
        server = TestServer(fake.app())
        await server.start_server()
        servers.append(server)
        backend_kw.setdefault("username", USER)
        backend_kw.setdefault("password", PASSWORD)
        backend = XuiBackend(str(server.make_url(BASE)), timeout=5, **backend_kw)
        backends.append(backend)
        return fake, backend

    yield _start
    for backend in backends:
        await backend.close()
    for server in servers:
        await server.close()


def spec(**overrides: Any) -> AccountSpec:
    values: dict[str, Any] = {
        "email": "tg42-1",
        "uuid": "11111111-2222-4333-8444-555555555555",
        "enabled": True,
        "expires_at": datetime(2027, 1, 15, 12, 0, tzinfo=UTC),
        "device_limit": 3,
        "traffic_limit_bytes": 50 * 1024**3,
        "tg_id": 42,
        "sub_id": "abcdefgh12345678",
        "comment": "vpn-bot",
        "flow": "xtls-rprx-vision",
    }
    values.update(overrides)
    return AccountSpec(**values)


def posts(fake: FakeXui, contains: str) -> list[Any]:
    return [body for method, path, body in fake.calls if method == "POST" and contains in path]


BOTH = pytest.mark.parametrize("generation", ["modern", "legacy"])


# ---------------------------------------------------------------------------------------------
# Detection, auth, transport
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("generation", "version"), [("modern", "3.11.0"), ("legacy", "2.9.4")])
async def test_ping_detects_generation_and_version(start: Started, generation: Generation, version: str) -> None:
    fake, backend = await start(generation)
    assert backend.kind == PanelKind.XUI
    assert backend.generation is None
    assert await backend.ping() == version
    assert backend.generation == ApiGeneration(generation)
    assert await backend.ping() == version  # cached generation; still checks connectivity
    assert fake.logins == 1
    assert [p for m, p, _ in fake.calls if p.startswith("/panel/api/server/status")] == ["/panel/api/server/status"] * 2


async def test_modern_login_sends_csrf_token_and_xhr_header(start: Started) -> None:
    fake, backend = await start("modern")
    await backend.ping()
    assert fake.logins == 1
    assert [p for _, p, _ in fake.calls][:2] == ["/csrf-token", "/login"]


async def test_wrong_password_raises_auth_error(start: Started) -> None:
    _, backend = await start("modern", password="nope")
    with pytest.raises(PanelAuthError, match="Invalid username"):
        await backend.ping()


async def test_wrong_base_path_is_reported(start: Started) -> None:
    _, backend = await start("legacy")
    wrong = XuiBackend(backend.url.removesuffix(BASE) + "/other", username=USER, password=PASSWORD)
    try:
        with pytest.raises(XuiRouteNotFoundError, match="base path"):
            await wrong.ping()
    finally:
        await wrong.close()


def test_backend_requires_credentials() -> None:
    with pytest.raises(ValueError, match="username"):
        XuiBackend("http://127.0.0.1:1")
    with pytest.raises(ValueError, match="username"):
        XuiBackend("http://127.0.0.1:1", username="only-user")


@BOTH
async def test_expired_session_relogs_in_and_retries(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    await backend.ping()
    fake.expire_sessions()
    assert len(await backend.list_inbounds()) == 1
    assert fake.logins == 2


async def test_concurrent_requests_after_expiry_login_once(start: Started) -> None:
    fake, backend = await start("modern")
    await backend.ping()
    fake.expire_sessions()
    results = await asyncio.gather(*(backend.list_inbounds() for _ in range(5)))
    assert all(len(r) == 1 for r in results)
    assert fake.logins == 2


async def test_rotated_csrf_token_is_refreshed_once(start: Started) -> None:
    fake, backend = await start("modern")
    await backend.upsert_account(1, spec())
    fake.rotate_csrf()
    await backend.upsert_account(1, spec(enabled=False))
    assert (fake.raw_client("tg42-1") or {})["enable"] is False
    assert fake.logins == 1


async def test_persistent_403_raises_auth_error(start: Started, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, backend = await start("modern")
    await backend.ping()
    original = fake.csrf_token

    async def broken_csrf(request: web.Request) -> web.Response:
        await original(request)
        return web.json_response({"obj": "wrong", "success": True})

    monkeypatch.setattr(fake, "csrf_token", broken_csrf)
    fake.rotate_csrf()
    with pytest.raises(PanelAuthError, match="forbidden"):
        await backend.upsert_account(1, spec())


async def test_bearer_token_skips_login(start: Started) -> None:
    fake, backend = await start("modern", username=None, password=None, api_token=TOKEN)
    await backend.upsert_account(1, spec())
    assert fake.logins == 0
    assert fake.raw_client("tg42-1") is not None


async def test_bad_token_without_credentials_raises(start: Started) -> None:
    _, backend = await start("modern", username=None, password=None, api_token="bad")
    with pytest.raises(PanelAuthError, match="API token"):
        await backend.ping()


async def test_bad_token_falls_back_to_session_login(start: Started) -> None:
    fake, backend = await start("modern", api_token="bad")
    assert await backend.ping() == "3.11.0"
    assert fake.logins == 1


async def test_token_on_legacy_panel_falls_back_to_login(start: Started) -> None:
    fake, backend = await start("legacy", api_token=TOKEN)
    assert await backend.ping() == "2.9.4"
    assert fake.logins == 1


async def test_token_on_legacy_panel_without_credentials_is_unsupported(start: Started) -> None:
    _, backend = await start("legacy", username=None, password=None, api_token=TOKEN)
    with pytest.raises(PanelUnsupportedError, match=r"v3\.1"):
        await backend.ping()


class _Refused(aiohttp.ClientConnectorError):
    """A connect failure without aiohttp's ConnectionKey plumbing."""

    def __init__(self) -> None:
        Exception.__init__(self, "refused")

    def __str__(self) -> str:
        return "refused"


class _FailingRequests:
    """Stands in for ``ClientSession.request``: every request raises ``error`` and is counted."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.methods: list[str] = []

    def __call__(self, method: str, url: str, **_: Any) -> _FailingRequests:
        self.methods.append(method)
        return self

    async def __aenter__(self) -> None:
        raise self.error

    async def __aexit__(self, *_: object) -> None:
        return None


@pytest.mark.parametrize(
    ("error", "expected_attempts"),
    [(_Refused(), 3), (aiohttp.ServerDisconnectedError(), 3), (TimeoutError(), 3)],
)
async def test_get_requests_are_retried_on_network_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected_attempts: int
) -> None:
    failing = _FailingRequests(error)
    monkeypatch.setattr(aiohttp.ClientSession, "request", lambda self, *a, **kw: failing(*a, **kw))
    backend = XuiBackend("http://panel.example:2053", username=USER, password=PASSWORD)
    try:
        with pytest.raises(PanelError, match=r"3x-ui GET /csrf-token: \w+"):
            await backend.ping()
    finally:
        await backend.close()
    assert failing.methods == ["GET"] * expected_attempts


@pytest.mark.parametrize(("error", "expected_attempts"), [(_Refused(), 3), (aiohttp.ServerDisconnectedError(), 1)])
async def test_post_is_retried_only_when_nothing_was_sent(
    start: Started, monkeypatch: pytest.MonkeyPatch, error: Exception, expected_attempts: int
) -> None:
    _, backend = await start("modern")
    await backend.ping()
    failing = _FailingRequests(error)
    monkeypatch.setattr(aiohttp.ClientSession, "request", lambda self, *a, **kw: failing(*a, **kw))
    with pytest.raises(PanelError, match="clients/get"):
        await backend.upsert_account(1, spec())  # GET first: retried
    failing.methods.clear()
    with pytest.raises(PanelError, match="clearIps"):
        await backend.reset_devices(RemoteAccount("tg42-1", "u", True, None, 0, 0))
    assert failing.methods == ["POST"] * expected_attempts


async def test_timeout_message_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        aiohttp.ClientSession, "request", lambda self, *a, **kw: _FailingRequests(TimeoutError())(*a, **kw)
    )
    backend = XuiBackend("http://panel.example:2053", username=USER, password=PASSWORD)
    try:
        with pytest.raises(PanelError, match="TimeoutError: timed out"):
            await backend.ping()
    finally:
        await backend.close()


async def test_non_json_answer_is_a_panel_error(start: Started, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, backend = await start("modern")

    async def html(request: web.Request) -> web.Response:
        return web.Response(text="<html>", content_type="text/html")

    monkeypatch.setattr(fake, "inbounds_list", html)
    with pytest.raises(PanelError, match="non-JSON"):
        await backend.list_inbounds()


async def test_api_error_carries_panel_message(start: Started) -> None:
    _, backend = await start("modern")
    with pytest.raises(XuiApiError, match=r"inbound 7: record not found") as info:
        await backend.upsert_account(7, spec())
    assert info.value.not_found


@pytest.mark.parametrize("target", [None, True, "1"])
async def test_target_must_be_an_inbound_id(start: Started, target: Any) -> None:
    _, backend = await start("modern")
    with pytest.raises(PanelError, match="inbound id"):
        await backend.get_account(target, "x")


async def test_generation_change_is_redetected(start: Started) -> None:
    fake, backend = await start("legacy")
    await backend.upsert_account(1, spec())
    assert backend.generation == ApiGeneration.LEGACY
    fake.upgrade_to_modern()
    account = await backend.upsert_account(1, spec(enabled=False))  # legacy route now 404 -> re-detect
    assert backend.generation == ApiGeneration.MODERN
    assert account.enabled is False
    assert fake.raw_client("tg42-1")["enable"] is False


# ---------------------------------------------------------------------------------------------
# Accounts: both generations
# ---------------------------------------------------------------------------------------------
@BOTH
async def test_upsert_creates_client_with_exact_wire_format(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    account = await backend.upsert_account(1, spec())
    expected = {
        "id": "11111111-2222-4333-8444-555555555555",
        "email": "tg42-1",
        "enable": True,
        "expiryTime": 1800014400000,
        "totalGB": 50 * 1024**3,
        "limitIp": 3,
        "tgId": 42,
        "subId": "abcdefgh12345678",
        "comment": "vpn-bot",
        "flow": "xtls-rprx-vision",
        "reset": 0,
    }
    if generation == "modern":
        assert posts(fake, "/clients/add") == [{"inboundIds": [1], "client": expected}]
    else:
        (body,) = posts(fake, "/inbounds/addClient")
        assert body["id"] == 1
        assert isinstance(body["settings"], str)
        assert json.loads(body["settings"]) == {"clients": [expected]}
    assert account.email == "tg42-1"
    assert account.uuid == expected["id"]
    assert account.enabled is True
    assert account.expires_at == datetime(2027, 1, 15, 12, 0, tzinfo=UTC)
    assert (account.device_limit, account.traffic_limit_bytes) == (3, 50 * 1024**3)


@BOTH
async def test_upsert_is_idempotent(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    await backend.upsert_account(1, spec())
    writes = len([c for c in fake.calls if c[0] == "POST"])
    await backend.upsert_account(1, spec())
    assert len([c for c in fake.calls if c[0] == "POST"]) == writes  # nothing changed -> no write


@BOTH
async def test_enable_disable_and_expiry_update(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    await backend.upsert_account(1, spec())
    await backend.upsert_account(1, spec(enabled=False))
    got = await backend.get_account(1, "tg42-1")
    assert got is not None and got.enabled is False
    new_expiry = datetime(2027, 6, 1, tzinfo=UTC)
    await backend.upsert_account(1, spec(enabled=True, expires_at=new_expiry, device_limit=5))
    got = await backend.get_account(1, "tg42-1")
    assert got is not None
    assert (got.enabled, got.expires_at, got.device_limit) == (True, new_expiry, 5)
    # full replace kept every managed field
    raw = fake.raw_client("tg42-1")
    assert raw is not None
    assert (raw["flow"], raw["subId"], raw["tgId"], raw["comment"]) == (
        "xtls-rprx-vision",
        "abcdefgh12345678",
        42,
        "vpn-bot",
    )


@BOTH
async def test_upsert_changes_uuid(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    await backend.upsert_account(1, spec())
    new_uuid = "99999999-2222-4333-8444-555555555555"
    account = await backend.upsert_account(1, spec(uuid=new_uuid))
    assert account.uuid == new_uuid
    got = await backend.get_account(1, "tg42-1")
    assert got is not None and got.uuid == new_uuid
    if generation == "legacy":
        assert any(p.endswith("/updateClient/11111111-2222-4333-8444-555555555555") for _, p, _ in fake.calls)


@BOTH
async def test_never_expiring_and_unlimited(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    account = await backend.upsert_account(1, spec(expires_at=None, traffic_limit_bytes=0, device_limit=0))
    assert account.expires_at is None
    raw = fake.raw_client("tg42-1")
    assert raw is not None and (raw["expiryTime"], raw["totalGB"], raw["limitIp"]) == (0, 0, 0)


@BOTH
async def test_get_account_reports_usage_and_missing(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    assert await backend.get_account(1, "tg42-1") is None
    fake.seed_client(1, build_client(spec()), used=(100, 250))
    got = await backend.get_account(1, "tg42-1")
    assert got is not None
    assert got.used_bytes == 350
    assert got.raw["email"] == "tg42-1"


@BOTH
async def test_list_accounts_filters_by_inbound(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    fake.add_inbound(2, 8443)
    fake.seed_client(1, build_client(spec()), used=(1, 2))
    fake.seed_client(2, build_client(spec(email="tg43-2", uuid="22222222-2222-4333-8444-555555555555")))
    accounts = await backend.list_accounts(1)
    assert [(a.email, a.used_bytes) for a in accounts] == [("tg42-1", 3)]
    assert [a.email for a in await backend.list_accounts(2)] == ["tg43-2"]


@BOTH
async def test_delete_missing_is_success(start: Started, generation: Generation) -> None:
    _, backend = await start(generation)
    await backend.delete_account(1, "ghost", "00000000-0000-4000-8000-000000000000")


@BOTH
async def test_delete_removes_client(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    other = spec(email="tg7-1", uuid="77777777-2222-4333-8444-555555555555")
    await backend.upsert_account(1, other)
    await backend.upsert_account(1, spec())
    await backend.delete_account(1, "tg42-1", spec().uuid)
    assert fake.raw_client("tg42-1") is None
    assert fake.raw_client("tg7-1") is not None


@BOTH
async def test_delete_last_client(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    await backend.upsert_account(1, spec())
    await backend.delete_account(1, "tg42-1", spec().uuid)
    assert fake.raw_client("tg42-1") is None
    assert await backend.get_account(1, "tg42-1") is None
    if generation == "legacy":  # the panel refuses an empty inbound: a disabled placeholder stays behind
        assert posts(fake, "/inbounds/update/") == []
        (placeholder,) = fake.inbounds[1]["settings"]["clients"]
        assert placeholder["enable"] is False
        assert XUI_EMAIL_RE.fullmatch(placeholder["email"]) is None  # never taken for an orphan
        assert await backend.list_accounts(1) == []  # nor listed as an account


@BOTH
async def test_reset_devices_clears_ips(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    account = await backend.upsert_account(1, spec())
    await backend.reset_devices(account)
    assert fake.cleared_ips == ["tg42-1"]


@BOTH
async def test_reset_traffic_zeroes_usage(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    await backend.upsert_account(1, spec())
    fake.traffic["tg42-1"] = (7, 9)
    await backend.reset_traffic(1, "tg42-1")
    assert fake.traffic["tg42-1"] == (0, 0)
    route = "/clients/resetTraffic/tg42-1" if generation == "modern" else "/inbounds/1/resetClientTraffic/tg42-1"
    assert posts(fake, route)


@BOTH
async def test_reset_traffic_of_missing_client_is_noop(start: Started, generation: Generation) -> None:
    _, backend = await start(generation)
    await backend.reset_traffic(1, "tg404-1")  # no error


@BOTH
async def test_get_links_is_empty(start: Started, generation: Generation) -> None:
    _, backend = await start(generation)
    account = RemoteAccount("e", "u", True, None, 0, 0)
    assert await backend.get_links(account) == []


# ---------------------------------------------------------------------------------------------
# Accounts: generation-specific quirks
# ---------------------------------------------------------------------------------------------
async def test_modern_update_preserves_unmanaged_fields_but_not_read_only_ones(start: Started) -> None:
    fake, backend = await start("modern")
    fake.seed_client(1, {**build_client(spec()), "limitHwid": 2, "reset": 30, "group": "vip"})
    await backend.upsert_account(1, spec(enabled=False))
    (body,) = posts(fake, "/clients/update/tg42-1")
    assert (body["limitHwid"], body["reset"], body["group"]) == (2, 30, "vip")
    assert "allowedIPs" not in body and "uuid" not in body and "createdAt" not in body
    assert body["id"] == spec().uuid


async def test_modern_upsert_attaches_existing_client_to_target(start: Started) -> None:
    fake, backend = await start("modern")
    fake.add_inbound(2, 8443)
    fake.seed_client(2, build_client(spec()))
    assert await backend.get_account(1, "tg42-1") is None  # exists, but not on inbound 1
    await backend.upsert_account(1, spec())
    assert fake.clients["tg42-1"]["inboundIds"] == [1, 2]
    assert posts(fake, "/clients/add") == []


async def test_modern_delete_detaches_shared_client(start: Started) -> None:
    fake, backend = await start("modern")
    fake.add_inbound(2, 8443)
    fake.seed_client(1, build_client(spec()))
    fake.clients["tg42-1"]["inboundIds"].append(2)
    await backend.delete_account(1, "tg42-1", spec().uuid)
    assert fake.clients["tg42-1"]["inboundIds"] == [2]


async def test_modern_delete_of_client_on_other_inbound_is_noop(start: Started) -> None:
    fake, backend = await start("modern")
    fake.add_inbound(2, 8443)
    fake.seed_client(2, build_client(spec()))
    await backend.delete_account(1, "tg42-1", spec().uuid)
    assert fake.clients["tg42-1"]["inboundIds"] == [2]


async def test_modern_concurrent_create_falls_back_to_update(start: Started, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, backend = await start("modern")
    original = fake.m_add

    async def racing_add(request: web.Request) -> web.Response:
        fake.seed_client(1, build_client(spec(enabled=False)))  # someone else created it first
        return await original(request)

    monkeypatch.setattr(fake, "m_add", racing_add)
    account = await backend.upsert_account(1, spec())
    assert account.enabled is True
    assert (fake.raw_client("tg42-1") or {})["enable"] is True


@BOTH
async def test_closed_backend_never_reopens_a_session(start: Started, generation: Generation) -> None:
    """The registry closes replaced backends that callers may still hold: no orphan session may appear."""
    fake, backend = await start(generation)
    await backend.ping()
    logins = fake.logins
    await backend.close()
    await backend.close()
    with pytest.raises(PanelError, match="closed"):
        await backend.get_account(1, "tg42-1")
    assert backend._t._http is None
    assert fake.logins == logins


@BOTH
async def test_get_used_bytes_reports_usage_and_missing(start: Started, generation: Generation) -> None:
    fake, backend = await start(generation)
    fake.add_inbound(2, 8443)
    assert await backend.get_used_bytes(1, "tg42-1") is None
    fake.seed_client(1, build_client(spec()), used=(100, 250))
    assert await backend.get_used_bytes(1, "tg42-1") == 350
    assert await backend.get_used_bytes(2, "tg42-1") is None  # the client lives on another inbound


async def test_legacy_usage_does_not_download_the_inbound(start: Started) -> None:
    """/sub usage polls must not fetch the inbound (its settings JSON holds every client)."""
    fake, backend = await start("legacy")
    fake.seed_client(1, build_client(spec()), used=(1, 2))
    await backend.ping()
    fake.calls.clear()
    assert await backend.get_used_bytes(1, "tg42-1") == 3
    assert [path for _, path, _ in fake.calls] == ["/panel/api/inbounds/getClientTraffics/tg42-1"]


async def test_legacy_upsert_of_new_client_does_not_download_the_inbound(start: Started) -> None:
    fake, backend = await start("legacy")
    fake.seed_client(1, build_client(spec(email="tg7-1", uuid="77777777-2222-4333-8444-555555555555")))
    await backend.ping()
    fake.calls.clear()
    account = await backend.upsert_account(1, spec())
    assert account.email == "tg42-1"
    assert not [path for _, path, _ in fake.calls if "/inbounds/get/" in path]
    assert (fake.raw_client("tg42-1") or {}).get("id") == spec().uuid


async def test_legacy_upsert_without_traffic_record_updates_existing_client(
    start: Started, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some panels answer ``obj: null`` for an unknown email; a client without stats is still updated."""
    fake, backend = await start("legacy")
    fake.seed_client(1, build_client(spec()))

    async def no_stats(request: web.Request) -> web.Response:
        return ok(None)

    monkeypatch.setattr(fake, "l_traffic", no_stats)
    await backend.upsert_account(1, spec(enabled=False))
    assert [c["email"] for c in fake.inbounds[1]["settings"]["clients"]] == ["tg42-1"]
    assert (fake.raw_client("tg42-1") or {})["enable"] is False


async def test_legacy_duplicate_email_on_other_inbound_is_an_error(start: Started) -> None:
    fake, backend = await start("legacy")
    fake.add_inbound(2, 8443)
    fake.seed_client(2, build_client(spec()))
    with pytest.raises(XuiApiError, match="Duplicate email"):
        await backend.upsert_account(1, spec())


async def test_legacy_update_keeps_unknown_fields_and_stored_email(start: Started) -> None:
    fake, backend = await start("legacy")
    fake.seed_client(1, {**build_client(spec(email="TG42-1")), "created_at": 5, "custom": "x"})
    await backend.upsert_account(1, spec(enabled=False))
    raw = fake.raw_client("TG42-1")
    assert raw is not None
    assert (raw["custom"], raw["created_at"], raw["enable"]) == ("x", 5, False)


async def test_legacy_string_tg_id_does_not_force_update(start: Started) -> None:
    fake, backend = await start("legacy")
    fake.seed_client(1, {**build_client(spec(tg_id=0)), "tgId": ""})
    await backend.upsert_account(1, spec(tg_id=0))
    assert posts(fake, "updateClient") == []


async def test_legacy_delete_on_missing_inbound_is_success(start: Started) -> None:
    _, backend = await start("legacy")
    await backend.delete_account(99, "tg42-1", spec().uuid)


async def test_legacy_last_client_delete_keeps_client_added_concurrently(start: Started) -> None:
    """Regression: deleting an inbound's last client must not rewrite the whole inbound (lost update)."""
    fake, backend = await start("legacy")
    await backend.upsert_account(1, spec())  # A is the inbound's only client
    other = spec(email="tg77-1", uuid="77777777-2222-4333-8444-555555555555")
    injected = False

    def inject(handler: Callable[[web.Request], Awaitable[web.Response]]) -> Any:
        async def wrapped(request: web.Request) -> web.Response:
            nonlocal injected
            if not injected:  # B's purchase lands between the delete's read and its first write
                injected = True
                await backend.upsert_account(1, other)
                assert fake.raw_client("tg77-1") is not None
            return await handler(request)

        return wrapped

    fake.l_add = inject(fake.l_add)  # type: ignore[method-assign]
    fake.l_del = inject(fake.l_del)  # type: ignore[method-assign]
    fake.l_inbound_update = inject(fake.l_inbound_update)  # type: ignore[method-assign]
    await backend.delete_account(1, "tg42-1", spec().uuid)
    assert injected
    assert fake.raw_client("tg42-1") is None
    assert fake.raw_client("tg77-1") is not None
    assert posts(fake, "/inbounds/update/") == []


async def test_legacy_last_client_delete_reuses_placeholder(start: Started) -> None:
    fake, backend = await start("legacy")
    for email in ("tg42-1", "tg43-1"):  # the second delete finds the placeholder from the first one
        await backend.upsert_account(1, spec(email=email, uuid=str(uuid4())))
        await backend.delete_account(1, email, "")
        (placeholder,) = fake.inbounds[1]["settings"]["clients"]
        assert placeholder["email"] == "vpn-bot-keep-1"
        assert placeholder["enable"] is False
    assert len(posts(fake, "/inbounds/addClient")) == 3  # two accounts, one placeholder


async def test_legacy_delete_becoming_last_client_concurrently_uses_placeholder(start: Started) -> None:
    fake, backend = await start("legacy")
    await backend.upsert_account(1, spec(email="tg7-1", uuid=str(uuid4())))
    await backend.upsert_account(1, spec())
    original = fake.l_del

    async def other_deleted_first(request: web.Request) -> web.Response:
        fake.l_del = original  # type: ignore[method-assign]
        clients = fake.inbounds[1]["settings"]["clients"]
        clients[:] = [c for c in clients if c["email"] != "tg7-1"]  # a concurrent delete of the other client
        return await original(request)

    fake.l_del = other_deleted_first  # type: ignore[method-assign]
    await backend.delete_account(1, "tg42-1", spec().uuid)
    assert [c["email"] for c in fake.inbounds[1]["settings"]["clients"]] == ["vpn-bot-keep-1"]
    assert posts(fake, "/inbounds/update/") == []


async def test_legacy_last_client_delete_fails_if_placeholder_cannot_be_added(start: Started) -> None:
    fake, backend = await start("legacy")
    fake.add_inbound(2, 44334)
    fake.seed_client(2, build_client(spec(email="vpn-bot-keep-1", uuid=str(uuid4()))))  # name taken elsewhere
    await backend.upsert_account(1, spec())
    with pytest.raises(XuiApiError, match="no client remained"):
        await backend.delete_account(1, "tg42-1", spec().uuid)
    assert fake.raw_client("tg42-1") is not None
    assert posts(fake, "/inbounds/update/") == []


async def test_legacy_get_on_missing_inbound_raises(start: Started) -> None:
    _, backend = await start("legacy")
    with pytest.raises(XuiApiError, match="record not found"):
        await backend.get_account(99, "tg42-1")


async def test_legacy_list_on_missing_inbound_raises(start: Started) -> None:
    _, backend = await start("legacy")
    with pytest.raises(XuiApiError, match="inbound 99 not found"):
        await backend.list_accounts(99)


# ---------------------------------------------------------------------------------------------
# Inbounds and link params
# ---------------------------------------------------------------------------------------------
@BOTH
async def test_list_inbounds_parses_strings_and_objects(start: Started, generation: Generation) -> None:
    _, backend = await start(generation)
    (inbound,) = await backend.list_inbounds()
    assert (inbound.remote_id, inbound.remark, inbound.protocol, inbound.port, inbound.enabled) == (
        1,
        "in-44333",
        "vless",
        44333,
        True,
    )
    assert inbound.params == {
        "type": "tcp",
        "encryption": "none",
        "security": "reality",
        "sni": "www.google.com",
        "pbk": PUBKEY,
        "sid": "6ba85179e30d4fc2",
        "fp": "chrome",
        "spx": "/",
        "flow_allowed": True,
    }


def test_parse_inbound_honours_disable_flow_and_disabled_state() -> None:
    raw = {"id": "3", "remark": "x", "protocol": "vless", "port": "443", "enable": False, "disableFlow": True}
    info = parse_inbound({**raw, "streamSettings": json.dumps(REALITY_STREAM), "settings": "{}"})
    assert (info.remote_id, info.port, info.enabled) == (3, 443, False)
    assert info.params["flow_allowed"] is False


def test_tls_tcp_params() -> None:
    stream = {
        "network": "tcp",
        "security": "tls",
        "tlsSettings": {
            "serverName": "vpn.example.com",
            "alpn": ["h2", "http/1.1"],
            "settings": {
                "fingerprint": "firefox",
                "echConfigList": "AEX+",
                "verifyPeerCertByName": " vpn.example.com ",
                "pinnedPeerCertSha256": ["abc", "", "def"],
            },
        },
    }
    assert normalize_link_params(stream) == {
        "type": "tcp",
        "encryption": "none",
        "security": "tls",
        "alpn": "h2,http/1.1",
        "sni": "vpn.example.com",
        "fp": "firefox",
        "ech": "AEX+",
        "vcn": "vpn.example.com",
        "pcs": "abc,def",
        "flow_allowed": True,
    }


def test_tls_without_client_settings_omits_fp() -> None:
    params = normalize_link_params({"network": "ws", "security": "tls", "tlsSettings": {"serverName": "a.ru"}})
    assert params["sni"] == "a.ru"
    assert "fp" not in params and "alpn" not in params
    assert params["flow_allowed"] is False  # vision only on raw tcp


def test_tcp_http_header_obfuscation() -> None:
    stream = {
        "network": "tcp",
        "security": "none",
        "tcpSettings": {
            "header": {
                "type": "http",
                "request": {"path": ["/a", "/b"], "headers": {"Host": ["req.example.com"]}},
                "response": {"headers": {}},
            }
        },
    }
    params = normalize_link_params(stream)
    assert (params["headerType"], params["path"], params["host"], params["security"]) == (
        "http",
        "/a",
        "req.example.com",
        "none",
    )
    assert params["flow_allowed"] is False


@pytest.mark.parametrize("network", ["ws", "httpupgrade"])
def test_ws_and_httpupgrade_host_sources(network: str) -> None:
    key = f"{network}Settings"
    explicit = normalize_link_params({"network": network, key: {"path": "/ws", "host": "cdn.example.ru"}})
    assert (explicit["path"], explicit["host"]) == ("/ws", "cdn.example.ru")
    from_headers = normalize_link_params({"network": network, key: {"path": "/ws", "headers": {"host": "h.ru"}}})
    assert from_headers["host"] == "h.ru"
    empty = normalize_link_params({"network": network, key: {}})
    assert (empty["path"], empty["host"]) == ("", "")


def test_grpc_params() -> None:
    stream = {"network": "grpc", "security": "reality", "grpcSettings": {"serviceName": "svc", "multiMode": True}}
    stream["realitySettings"] = REALITY_STREAM["realitySettings"]
    params = normalize_link_params(stream)
    assert (params["serviceName"], params["authority"], params["mode"]) == ("svc", "", "multi")
    assert params["flow_allowed"] is False
    single = normalize_link_params({"network": "grpc", "grpcSettings": {"serviceName": "s", "authority": "a"}})
    assert "mode" not in single and single["authority"] == "a"


def test_xhttp_params_and_extra() -> None:
    xhttp = {
        "path": "/api/v2/stream",
        "host": "cdn.example.ru",
        "mode": "packet-up",
        "xPaddingBytes": "100-1000",
        "uplinkHTTPMethod": "GET",
        "uplinkDataPlacement": "header",
        "uplinkDataKey": "X-Data",
        "scMaxEachPostBytes": "4096",
        "scMinPostsIntervalMs": "30",  # core default -> dropped
        "sessionPlacement": "path",  # legacy name -> lifted + aliased
        "noSSEHeader": True,  # server-only -> dropped
        "headers": {"Host": "ignored", "X-Real": "1"},
        "xmux": {},
    }
    params = normalize_link_params({"network": "xhttp", "security": "tls", "xhttpSettings": xhttp})
    assert (params["path"], params["host"], params["mode"], params["x_padding_bytes"]) == (
        "/api/v2/stream",
        "cdn.example.ru",
        "packet-up",
        "100-1000",
    )
    assert params["extra"] == {
        "mode": "packet-up",
        "xPaddingBytes": "100-1000",
        "uplinkHTTPMethod": "GET",
        "uplinkDataPlacement": "header",
        "uplinkDataKey": "X-Data",
        "scMaxEachPostBytes": "4096",
        "sessionIDPlacement": "path",
        "sessionPlacement": "path",
        "headers": {"X-Real": "1"},
    }


def test_xhttp_extra_edge_cases() -> None:
    assert xhttp_extra({}) == {}
    assert xhttp_extra({"scMaxEachPostBytes": "1000000", "uplinkChunkSize": 0, "noGRPCHeader": False}) == {}
    extra = xhttp_extra(
        {
            "xPaddingObfsMode": True,
            "xPaddingKey": "k",
            "sessionIDKey": "sid",
            "sessionKey": "old",
            "uplinkChunkSize": 64,
            "noGRPCHeader": True,
            "downloadSettings": {"address": "x"},
        }
    )
    assert extra == {
        "xPaddingObfsMode": True,
        "xPaddingKey": "k",
        "sessionIDKey": "sid",
        "sessionKey": "sid",
        "uplinkChunkSize": 64,
        "noGRPCHeader": True,
        "downloadSettings": {"address": "x"},
    }
    minimal = normalize_link_params({"network": "xhttp", "xhttpSettings": {"path": "/x"}})
    assert "extra" not in minimal and "mode" not in minimal and minimal["host"] == ""


def test_reality_legacy_layout_and_pqv() -> None:
    stream = {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
            "serverNames": [],
            "shortIds": [""],
            "settings": {"publicKey": "PBK", "fingerprint": "", "spiderX": "/x?y=1", "mldsa65Verify": "PQV"},
        },
    }
    params = normalize_link_params(stream)
    assert params["pbk"] == "PBK"
    assert params["pqv"] == "PQV"
    assert params["spx"] == "/x?y=1"
    assert "sni" not in params and "sid" not in params and "fp" not in params


def test_vless_encryption_allows_flow_on_any_transport() -> None:
    params = normalize_link_params({"network": "ws"}, {"encryption": "mlkem768x25519plus.native.0rtt.abc"})
    assert params["encryption"] == "mlkem768x25519plus.native.0rtt.abc"
    assert params["flow_allowed"] is True
    assert normalize_link_params({"network": "xdrive"}, {"decryption": "x"})["flow_allowed"] is False


@pytest.mark.parametrize("stream", [None, "", "not json", "[1]", {}])
def test_garbage_stream_settings_default_to_plain_tcp(stream: Any) -> None:
    assert normalize_link_params(stream) == {
        "type": "tcp",
        "encryption": "none",
        "security": "none",
        "flow_allowed": False,
    }


def test_unsupported_network_keeps_basic_params() -> None:
    assert normalize_link_params({"network": "kcp", "security": "none"})["type"] == "kcp"


# ---------------------------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------------------------
def test_expiry_conversions() -> None:
    moment = datetime(2027, 1, 15, 12, 0, tzinfo=UTC)
    assert to_expiry_ms(moment) == 1800014400000
    assert to_expiry_ms(None) == 0
    assert from_expiry_ms(1800014400000) == moment
    assert from_expiry_ms(0) is None
    assert from_expiry_ms(-86400000) is None  # "start on first use"
    assert from_expiry_ms("garbage") is None
    with pytest.raises(ValueError, match="aware"):
        to_expiry_ms(datetime(2027, 1, 1))


def test_build_client_clamps_negative_limits_and_keeps_carry() -> None:
    client = build_client(spec(device_limit=-1, traffic_limit_bytes=-5), {"reset": 7, "limitHwid": 1})
    assert (client["limitIp"], client["totalGB"], client["reset"], client["limitHwid"]) == (0, 0, 7, 1)
    assert client["expiryTime"] == to_expiry_ms(datetime(2027, 1, 15, 12, 0, tzinfo=UTC))
    assert to_expiry_ms(datetime.now(UTC) + timedelta(days=1)) > 0
