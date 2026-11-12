"""Optional panel backend capabilities: server status, core restart, devices, Reality inbound creation."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app.services.devices import DeviceLister, DeviceRemover
from app.services.panels.base import (
    CoreRestarter,
    NodeStatus,
    PanelUnsupportedError,
    RemoteAccount,
    ServerMonitor,
    ServerStatus,
)
from app.services.panels.remnawave import RemnawaveBackend, parse_node, parse_system_stats
from app.services.panels.xui import (
    XuiBackend,
    build_reality_inbound,
    generate_reality_keys,
    parse_client_ips,
    parse_server_status,
    reality_public_key,
)

TOKEN = "tok-1234567890"
STATUS = {
    "cpu": 12.5,
    "cpuCores": 2,
    "mem": {"current": 512, "total": 2048},
    "swap": {"current": 0, "total": 0},
    "disk": {"current": 10, "total": 40},
    "xray": {"state": "running", "errorMsg": "", "version": "25.10.15"},
    "uptime": 3600,
    "loads": [0.1, 0.2, 0.3],
    "tcpCount": 42,
    "netIO": {"up": 100, "down": 200},
    "netTraffic": {"sent": 1000, "recv": 2000},
    "panelVersion": "3.11.0",
}


def ok(obj: Any = None) -> web.Response:
    return web.json_response({"success": True, "msg": "", "obj": obj})


class FakeXuiServer:
    def __init__(self, *, modern: bool) -> None:
        self.modern = modern
        self.calls: list[tuple[str, str]] = []
        self.inbounds: list[dict[str, Any]] = []
        self.ips: Any = json.dumps(["1.2.3.4 (2026-11-04 10:00:00)", "5.6.7.8"])
        self.added: list[dict[str, Any]] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    async def handle(self, request: web.Request) -> web.StreamResponse:
        path = request.path
        self.calls.append((request.method, path))
        if path == "/csrf-token":
            raise web.HTTPNotFound()
        if path == "/login":
            return web.json_response({"success": True, "msg": "", "obj": None})
        if self.modern and request.headers.get("Authorization") != f"Bearer {TOKEN}":
            raise web.HTTPUnauthorized()
        if path == "/panel/api/server/status":
            return ok(STATUS if self.modern else {**STATUS, "panelVersion": ""})
        if path.startswith("/panel/api/clients/get/"):
            if not self.modern:
                raise web.HTTPNotFound()
            return web.json_response({"success": False, "msg": "client not found", "obj": None})
        if path == "/panel/api/server/restartXrayService" and request.method == "POST":
            return ok()
        if path.startswith("/panel/api/clients/ips/"):
            if not self.modern:
                raise web.HTTPNotFound()
            return ok([{"ip": "9.9.9.9", "timestamp": 1793750400}])
        if path.startswith("/panel/api/inbounds/clientIps/"):
            return ok(self.ips)
        if path == "/panel/api/inbounds/add":
            body = await request.json()
            self.added.append(body)
            created = {**body, "id": 7, "streamSettings": json.loads(body["streamSettings"])}
            self.inbounds.append(created)
            return ok(created)
        if path == "/panel/api/inbounds/list":
            return ok(self.inbounds)
        raise web.HTTPNotFound()


@pytest.fixture
async def xui_server() -> AsyncIterator[tuple[FakeXuiServer, str]]:
    fake = FakeXuiServer(modern=True)
    server = TestServer(fake.app())
    await server.start_server()
    try:
        yield fake, str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


@pytest.fixture
async def legacy_server() -> AsyncIterator[tuple[FakeXuiServer, str]]:
    fake = FakeXuiServer(modern=False)
    server = TestServer(fake.app())
    await server.start_server()
    try:
        yield fake, str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


def _account(email: str = "tg1-1") -> RemoteAccount:
    return RemoteAccount(email=email, uuid="u", enabled=True, expires_at=None, device_limit=0, traffic_limit_bytes=0)


async def test_xui_capabilities(xui_server: tuple[FakeXuiServer, str]) -> None:
    fake, url = xui_server
    backend = XuiBackend(url, api_token=TOKEN)
    try:
        assert isinstance(backend, ServerMonitor) and isinstance(backend, CoreRestarter)
        assert isinstance(backend, DeviceLister) and not isinstance(backend, DeviceRemover)
        status = await backend.server_status()
        assert status.cpu_percent == 12.5 and status.mem_percent == 25.0 and status.core_running
        assert status.load == (0.1, 0.2, 0.3) and status.net_down_bps == 200 and status.tcp_connections == 42
        await backend.restart_core()
        assert ("POST", "/panel/api/server/restartXrayService") in fake.calls
        (device,) = await backend.list_devices(_account())
        assert device.id == device.ip == "9.9.9.9" and device.created_at == datetime(2026, 11, 4, tzinfo=UTC)

        info = await backend.create_reality_inbound(44333, "www.microsoft.com", remark="DE")
        assert info.remote_id == 7 and info.port == 44333 and info.remark == "DE"
        assert info.params["security"] == "reality" and info.params["sni"] == "www.microsoft.com"
        assert info.params["flow"] == "xtls-rprx-vision" and len(info.params["sid"]) == 16
        stream = json.loads(fake.added[0]["streamSettings"])["realitySettings"]
        assert stream["target"] == "www.microsoft.com:443"
        assert reality_public_key(stream["privateKey"]) == stream["settings"]["publicKey"] == info.params["pbk"]
    finally:
        await backend.close()


async def test_xui_legacy_devices(legacy_server: tuple[FakeXuiServer, str]) -> None:
    fake, url = legacy_server
    backend = XuiBackend(url, username="admin", password="pw")
    try:
        devices = await backend.list_devices(_account())
        assert [d.id for d in devices] == ["1.2.3.4", "5.6.7.8"]
        assert devices[0].created_at == datetime(2026, 11, 4, 10, tzinfo=UTC)
        fake.ips = "No IP Record"
        assert await backend.list_devices(_account()) == []
        assert not any(path.startswith("/panel/api/clients/ips/") for _, path in fake.calls)
    finally:
        await backend.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("No IP Record", []),
        (["1.1.1.1", "1.1.1.1 (2026-01-01 00:00:00)"], ["1.1.1.1"]),
        ('["2.2.2.2"]', ["2.2.2.2"]),
        ("3.3.3.3\n4.4.4.4", ["3.3.3.3", "4.4.4.4"]),
        ([{"ip": "::1", "timestamp": "2026-01-01 00:00:00"}, {"nope": 1}, 5], ["::1"]),
        (None, []),
    ],
)
def test_parse_client_ips(raw: Any, expected: list[str]) -> None:
    assert [ip for ip, _ in parse_client_ips(raw)] == expected


def test_reality_helpers() -> None:
    private, public = generate_reality_keys()
    raw = base64.urlsafe_b64decode(private + "=")
    assert len(raw) == 32 and "=" not in private + public
    assert reality_public_key(private) == public
    X25519PrivateKey.from_private_bytes(raw)  # a valid key
    body = build_reality_inbound(443, "Example.COM", private_key=private, short_id="ab")
    stream = json.loads(body["streamSettings"])
    assert stream["realitySettings"]["serverNames"] == ["example.com"]
    assert stream["realitySettings"]["shortIds"] == ["ab"] and body["remark"] == "reality-443"
    for bad in ({"port": 0, "sni": "a.com"}, {"port": 443, "sni": "bad sni"}, {"port": 443, "sni": "a.com/x"}):
        with pytest.raises(ValueError):
            build_reality_inbound(bad["port"], bad["sni"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_reality_inbound(443, "a.com", short_id="xyz")
    with pytest.raises(ValueError):
        reality_public_key("c2hvcnQ")


def test_parse_server_status_tolerates_missing_fields() -> None:
    status = parse_server_status({"cpu": "n/a", "mem": "x", "xray": None, "loads": ["1", None]})
    assert status == ServerStatus(load=(1.0,))
    assert status.core_running is None and status.mem_percent is None


# --- Remnawave -------------------------------------------------------------------------------------------------------


class FakeRemnawave:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.devices = [
            {
                "hwid": "hw-1",
                "userId": 5,
                "platform": "iOS",
                "osVersion": "18.1",
                "deviceModel": "iPhone",
                "userAgent": "Happ/3.2.1",
                "requestIp": "1.2.3.4",
                "createdAt": "2026-11-03T20:51:26.567Z",
            },
            {"hwid": "hw-2", "userId": 5, "createdAt": "bad"},
            {"userId": 5},
        ]
        self.stats_fail = False
        self.no_nodes = False

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json() if request.can_read_body else None
        self.calls.append((request.method, request.path, body))
        path = request.path
        if path == "/api/system/metadata":
            return web.json_response({"response": {"version": "3.4.8"}})
        if path == "/api/hwid/devices/5":
            return web.json_response({"response": {"total": len(self.devices), "devices": self.devices}})
        if path == "/api/hwid/devices/delete":
            if body and body.get("hwid") == "missing":
                return web.json_response({"message": "not found", "errorCode": "A101"}, status=404)
            return web.json_response({"response": {"total": 0, "devices": []}})
        if path == "/api/nodes":
            return web.json_response(
                {
                    "response": [
                        {
                            "uuid": "n1",
                            "name": "DE-1",
                            "address": "de.example.com",
                            "isConnected": True,
                            "isDisabled": False,
                            "usersOnline": 3,
                            "xrayUptime": "120",
                            "trafficUsedBytes": 1024,
                            "xrayVersion": "25.1.1",
                            "countryCode": "DE",
                        },
                        {"uuid": "n2", "name": "NL-1", "isConnected": False, "lastStatusMessage": "timeout"},
                        "junk",
                    ]
                }
            )
        if path == "/api/system/stats":
            if self.stats_fail:
                return web.json_response({"message": "forbidden"}, status=403)
            return web.json_response(
                {
                    "response": {
                        "cpu": {"cores": 8},
                        "memory": {"total": 1000, "used": 900, "available": 400},
                        "uptime": 99,
                        "onlineStats": {"onlineNow": 11},
                    }
                }
            )
        if path == "/api/nodes/actions/restart-all":
            if self.no_nodes:
                body = {"statusCode": 409, "message": "Enabled nodes not found", "errorCode": "A054"}
                return web.json_response(body, status=409)
            return web.json_response({"response": {"eventSent": True}})
        raise web.HTTPNotFound()


@pytest.fixture
async def rw_server() -> AsyncIterator[tuple[FakeRemnawave, str]]:
    fake = FakeRemnawave()
    server = TestServer(fake.app())
    await server.start_server()
    try:
        yield fake, str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


async def test_remnawave_capabilities(rw_server: tuple[FakeRemnawave, str]) -> None:
    fake, url = rw_server
    backend = RemnawaveBackend(url, "token", squad_uuids=["s"])
    backend.retry_delays = ()
    account = RemoteAccount(
        email="tg_5", uuid="u", enabled=True, expires_at=None, device_limit=0, traffic_limit_bytes=0, remote_id="5"
    )
    try:
        assert isinstance(backend, DeviceLister) and isinstance(backend, DeviceRemover)
        assert isinstance(backend, ServerMonitor) and isinstance(backend, CoreRestarter)
        devices = await backend.list_devices(account)
        assert [d.id for d in devices] == ["hw-1", "hw-2"]
        first = devices[0]
        assert (first.platform, first.model, first.os_version, first.ip) == ("iOS", "iPhone", "18.1", "1.2.3.4")
        assert first.created_at is not None and devices[1].created_at is None

        await backend.delete_device(account, "hw-1")
        await backend.delete_device(account, "missing")  # not found is fine
        assert ("POST", "/api/hwid/devices/delete", {"userId": 5, "hwid": "hw-1"}) in fake.calls

        status = await backend.server_status()
        assert [n.name for n in status.nodes] == ["DE-1", "NL-1"]
        assert status.nodes[0].uptime_sec == 120 and status.nodes[0].users_online == 3
        assert [n.name for n in status.nodes_offline] == ["NL-1"]
        assert (status.cpu_cores, status.mem_used, status.mem_total, status.online_users) == (8, 600, 1000, 11)

        fake.stats_fail = True
        status = await backend.server_status()
        assert status.online_users == 3 and status.mem_total is None

        await backend.restart_core()
        assert ("POST", "/api/nodes/actions/restart-all", {"forceRestart": False}) in fake.calls
        fake.no_nodes = True
        await backend.restart_core()  # nothing to restart is not an error
    finally:
        await backend.close()


def test_remnawave_parsers() -> None:
    node = parse_node(
        {"address": "x.example.com", "isConnected": True, "isDisabled": True, "nodeTraffic": {"usedTrafficBytes": "7"}}
    )
    assert node == NodeStatus(
        "x.example.com", online=False, address="x.example.com", disabled=True, traffic_used_bytes=7
    )
    status = parse_system_stats({"memory": "x"}, [])
    assert status == ServerStatus()


async def test_legacy_panels_without_ip_routes(legacy_server: tuple[FakeXuiServer, str]) -> None:
    fake, _url = legacy_server

    async def gone(request: web.Request) -> web.StreamResponse:
        raise web.HTTPNotFound()

    original = fake.handle

    async def handle(request: web.Request) -> web.StreamResponse:
        if request.path.startswith("/panel/api/inbounds/clientIps/"):
            return await gone(request)
        return await original(request)

    fake.handle = handle  # type: ignore[method-assign]
    server = TestServer(fake.app())
    await server.start_server()
    backend = XuiBackend(str(server.make_url("")).rstrip("/"), username="a", password="b")
    try:
        with pytest.raises(PanelUnsupportedError):
            await backend.list_devices(_account())
    finally:
        await backend.close()
        await server.close()
