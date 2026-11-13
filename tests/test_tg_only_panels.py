"""Telegram-only account on Remnawave, Marzban, PasarGuard and Marzneshin: the panel's own API writes the user and the
routing rules (exact Xray email, order, idempotency, removal), and the servers become eligible."""

from __future__ import annotations

import copy
import json
import re
import uuid as uuidlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.db.models import Location, Panel, PanelKind
from app.db.session import SessionFactory
from app.services import tg_only
from app.services.panels.base import TG_ONLY_USERNAME, AccountSpec, TgOnlyManaged, TgOnlyRouting
from app.services.panels.marzban import MarzbanBackend
from app.services.panels.marzneshin import MarzneshinBackend
from app.services.panels.pasarguard import PasarGuardBackend
from app.services.panels.remnawave import RemnawaveBackend
from app.services.panels.xray_template import (
    TG_ONLY_BLOCK_TAG,
    TG_ONLY_DOMAIN_TAG,
    TG_ONLY_IP_TAG,
    marzban_user_match,
    regexp_user_supported,
    set_tg_only_config,
    xray_version,
)
from tests.conftest import make_settings

CLIENT = str(uuidlib.UUID(int=7))
LINK = (
    "vless://{uid}@203.0.113.9:443?type=tcp&security=reality&pbk=PUBKEY&sid=6ba85179&sni=www.example.ru"
    "&fp=chrome&flow=xtls-rprx-vision#node"
)
OPERATOR_RULE = {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"}
REALITY_KEY = "UuMBgl7MXTPx9inmQp2UC7Jcnwc6XYbwDNebonM-FCc"


def xray_config() -> dict[str, Any]:
    """A panel-style Xray config: one VLESS Reality inbound, DIRECT / BLOCK outbounds and an operator rule."""
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "VLESS TCP REALITY",
                "listen": "0.0.0.0",
                "port": 443,
                "protocol": "vless",
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "dest": "www.example.ru:443",
                        "xver": 0,
                        "serverNames": ["www.example.ru"],
                        "privateKey": REALITY_KEY,
                        "shortIds": ["6ba85179"],
                    },
                },
                "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}, {"protocol": "blackhole", "tag": "BLOCK"}],
        "routing": {"rules": [copy.deepcopy(OPERATOR_RULE)]},
    }


def spec(**changes: Any) -> AccountSpec:
    values: dict[str, Any] = {
        "email": TG_ONLY_USERNAME,
        "uuid": CLIENT,
        "enabled": True,
        "expires_at": None,
        "device_limit": 0,
        "traffic_limit_bytes": 0,
        "tg_id": 0,
        "sub_id": "x" * 16,
        "comment": "Telegram-only",
    }
    values.update(changes)
    return AccountSpec(**values)


def tg_rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in config["routing"]["rules"] if r.get("ruleTag")]


def assert_confined(config: dict[str, Any], users: list[str]) -> None:
    """The three rules sit first, match exactly ``users`` and leave the operator's rule behind them."""
    rules = config["routing"]["rules"]
    assert [r.get("ruleTag") for r in rules[:3]] == [TG_ONLY_IP_TAG, TG_ONLY_DOMAIN_TAG, TG_ONLY_BLOCK_TAG]
    assert all(r["user"] == users for r in rules[:3])
    assert rules[0]["outboundTag"] == "DIRECT" and rules[1]["outboundTag"] == "DIRECT"
    block = rules[2]["outboundTag"]
    assert any(o["tag"] == block and o["protocol"] == "blackhole" for o in config["outbounds"])
    assert OPERATOR_RULE in rules[3:] and len(tg_rules(config)) == 3
    assert config["inbounds"][0]["sniffing"]["enabled"] is True


class FakePanelServer:
    """Base of the in-process panel fakes: ``events`` records what happened, in order."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.config = xray_config()
        self.config_writes = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    async def handle(self, request: web.Request) -> web.StreamResponse:  # pragma: no cover - overridden
        raise NotImplementedError

    def tg_state(self, status: str) -> str:
        return status


# ---------------------------------------------------------------------------------------------
# Marzban
# ---------------------------------------------------------------------------------------------
class FakeMarzban(FakePanelServer):
    def __init__(self, version: str = "26.10.31", node_versions: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.version = version
        self.nodes = [{"id": i, "status": "connected", "xray_version": v} for i, v in enumerate(node_versions, 1)]
        self.users: dict[str, dict[str, Any]] = {}
        self.next_id = 41

    def _user(self, name: str) -> dict[str, Any]:
        row = self.users[name]
        return {**row, "links": [LINK.format(uid=row["proxies"]["vless"]["id"])], "subscription_url": f"/sub/{name}"}

    async def handle(self, request: web.Request) -> web.StreamResponse:
        path, method = request.path, request.method
        if path == "/api/admin/token":
            return web.json_response({"access_token": "tok"})
        if request.headers.get("Authorization") != "Bearer tok":
            return web.json_response({}, status=401)
        body = await request.json() if request.can_read_body else None
        if path == "/api/core":
            return web.json_response({"version": self.version, "started": True})
        if path == "/api/nodes":
            return web.json_response(self.nodes)
        if path == "/api/core/config":
            if method == "PUT":
                self.config, self.config_writes = body, self.config_writes + 1
                status = self.users.get(TG_ONLY_USERNAME, {}).get("status", "absent")
                self.events.append(f"config:{'rules' if tg_rules(body) else 'clean'}:user={status}")
            return web.json_response(self.config)
        name = path.removeprefix("/api/user/") if path.startswith("/api/user/") else ""
        if path == "/api/user" and method == "POST":
            row = {"username": body["username"], "status": "active", "proxies": body["proxies"], "used_traffic": 0}
            row.update(expire=body.get("expire", 0), data_limit=body.get("data_limit", 0))
            self.users[body["username"]] = row
            self.next_id += 1
            self.events.append("user:create")
            return web.json_response(self._user(body["username"]))
        if name and name in self.users:
            if method == "GET":
                return web.json_response(self._user(name))
            if method == "PUT":
                self.users[name].update({k: v for k, v in body.items() if k in ("proxies", "status", "data_limit")})
                self.events.append(f"user:{self.users[name]['status']}")
                return web.json_response(self._user(name))
            if method == "DELETE":
                del self.users[name]
                self.events.append("user:delete")
                return web.json_response({})
        return web.json_response({"detail": "not found"}, status=404)


@pytest.fixture
async def marzban() -> AsyncIterator[tuple[FakeMarzban, MarzbanBackend]]:
    fake = FakeMarzban()
    async with TestServer(fake.app()) as server:
        backend = MarzbanBackend(
            str(server.make_url("")), username="a", password="b", inbound_tags=["VLESS TCP REALITY"]
        )
        yield fake, backend
        await backend.close()


async def test_marzban_user_is_disabled_until_the_rules_are_in(marzban: tuple[FakeMarzban, MarzbanBackend]) -> None:
    fake, backend = marzban
    assert isinstance(backend, TgOnlyManaged) and not isinstance(backend, TgOnlyRouting)
    access = await backend.ensure_tg_only(spec())
    assert fake.events == ["user:create", "user:disabled", "config:rules:user=disabled", "user:active"]
    assert_confined(fake.config, [marzban_user_match(TG_ONLY_USERNAME)])
    assert fake.config["routing"]["rules"][0]["user"] == [r"regexp:^[0-9]+\.tgonly$"]
    assert access.links == (LINK.format(uid=CLIENT),)


async def test_marzban_is_idempotent_and_removal_deletes_the_user_first(
    marzban: tuple[FakeMarzban, MarzbanBackend],
) -> None:
    fake, backend = marzban
    await backend.ensure_tg_only(spec())
    before, writes = copy.deepcopy(fake.config), fake.config_writes
    fake.events.clear()
    await backend.ensure_tg_only(spec())
    assert fake.config == before and fake.config_writes == writes  # nothing rewritten, no core restart
    assert "user:create" not in fake.events
    fake.events.clear()
    await backend.remove_tg_only(TG_ONLY_USERNAME)
    assert fake.events == ["user:delete", "config:clean:user=absent"]  # client first, then the rules
    assert not tg_rules(fake.config) and fake.config["routing"]["rules"] == [OPERATOR_RULE]
    await backend.remove_tg_only(TG_ONLY_USERNAME)  # safe to repeat
    assert fake.config_writes == writes + 1


async def test_marzban_rotation_changes_the_id_in_place(marzban: tuple[FakeMarzban, MarzbanBackend]) -> None:
    fake, backend = marzban
    await backend.ensure_tg_only(spec())
    other = str(uuidlib.UUID(int=8))
    access = await backend.ensure_tg_only(spec(uuid=other))
    assert fake.users[TG_ONLY_USERNAME]["proxies"]["vless"]["id"] == other and LINK.format(uid=other) in access.links
    assert_confined(fake.config, [marzban_user_match(TG_ONLY_USERNAME)])


@pytest.mark.parametrize("core,nodes", [("24.9.30", ()), ("", ()), ("26.10.31", ("25.1.30", "24.1.1"))])
async def test_marzban_refuses_a_core_that_would_ignore_the_regular_expression(
    core: str, nodes: tuple[str, ...]
) -> None:
    fake = FakeMarzban(version=core, node_versions=nodes)
    async with TestServer(fake.app()) as server:
        backend = MarzbanBackend(str(server.make_url("")), username="a", password="b")
        with pytest.raises(Exception, match=r"older than 24.10.16"):
            await backend.ensure_tg_only(spec())
        await backend.close()
    assert fake.users == {} and fake.config_writes == 0  # nothing was created: no unrestricted client


def test_xray_versions_and_the_user_pattern() -> None:
    assert xray_version("Xray 25.1.30 (go1.23)") == (25, 1, 30) and xray_version("v26.10.31") == (26, 10, 31)
    assert xray_version("") is None and not regexp_user_supported("24.10.15") and regexp_user_supported("24.10.16")
    assert regexp_user_supported("26.10.31") and not regexp_user_supported(None) and not regexp_user_supported("1.8")
    pattern = re.compile(marzban_user_match("tg-only.x").removeprefix("regexp:"))
    assert pattern.match("12.tg-only.x") and not pattern.match("12.tg-only.xy") and not pattern.match("tg-only.x")
    assert not pattern.match("12.other.tg-only.x") and not pattern.match("1a2.tg-only.x")


# ---------------------------------------------------------------------------------------------
# Remnawave
# ---------------------------------------------------------------------------------------------
SQUAD = "11111111-1111-4111-8111-111111111111"
PROFILE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_PROFILE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class FakeRemnawave(FakePanelServer):
    def __init__(self) -> None:
        super().__init__()
        self.users: dict[int, dict[str, Any]] = {}
        self.next_id = 100
        self.other_config = xray_config()  # a profile no squad uses: it must stay untouched
        self.other_writes = 0

    def _ok(self, data: Any, status: int = 200) -> web.Response:
        return web.json_response({"response": data}, status=status)

    def _missing(self) -> web.Response:
        return web.json_response({"message": "nf", "errorCode": "A025"}, status=404)

    def _by_name(self, name: str) -> dict[str, Any] | None:
        return next((u for u in self.users.values() if u["username"] == name), None)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        path, method = request.path, request.method
        if request.headers.get("Authorization") != "Bearer tok":
            return web.json_response({}, status=401)
        body = await request.json() if request.can_read_body else None
        if path == "/api/system/metadata":
            return self._ok({"version": "3.4.10"})
        if path == "/api/internal-squads":
            squad = {"uuid": SQUAD, "name": "s", "info": {}, "inbounds": [{"profileUuid": PROFILE, "tag": "t"}]}
            return self._ok({"internalSquads": [squad]})
        if path == "/api/config-profiles" and method == "GET":
            profiles = [
                {"uuid": PROFILE, "name": "main", "config": self.config},
                {"uuid": OTHER_PROFILE, "name": "other", "config": self.other_config},
            ]
            return self._ok({"total": 2, "configProfiles": profiles})
        if path == "/api/config-profiles" and method == "PATCH":
            if body["uuid"] == PROFILE:
                self.config, self.config_writes = body["config"], self.config_writes + 1
                user = next(iter(self.users.values()), {"status": "absent"})
                self.events.append(f"config:{'rules' if tg_rules(body['config']) else 'clean'}:user={user['status']}")
            else:
                self.other_config, self.other_writes = body["config"], self.other_writes + 1
            return self._ok({})
        if path.startswith("/api/users/by-username/"):
            user = self._by_name(path.rsplit("/", 1)[1])
            return self._ok(user) if user else self._missing()
        if path == "/api/users" and method == "POST":
            self.next_id += 1
            user = {
                "id": self.next_id,
                "username": body["username"],
                "vlessUuid": body["vlessUuid"],
                "status": body["status"],
                "expireAt": body["expireAt"],
                "trafficLimitBytes": body["trafficLimitBytes"],
                "hwidDeviceLimit": 0,
                "shortUuid": "s",
                "activeInternalSquads": [{"uuid": u} for u in body["activeInternalSquads"]],
                "userTraffic": {"usedTrafficBytes": 0},
            }
            self.users[user["id"]] = user
            self.events.append(f"user:create:{user['status']}")
            return self._ok(user, 201)
        if path == "/api/users" and method == "PATCH":
            user = self.users[body["id"]]
            user.update({k: v for k, v in body.items() if k != "id"})
            if "activeInternalSquads" in body:
                user["activeInternalSquads"] = [{"uuid": u} for u in body["activeInternalSquads"]]
            return self._ok(user)
        match = re.fullmatch(r"/api/users/(\d+)/actions/(enable|disable)", path)
        if match:
            user = self.users[int(match.group(1))]
            user["status"] = "ACTIVE" if match.group(2) == "enable" else "DISABLED"
            self.events.append(f"user:{user['status'].lower()}")
            return self._ok(user)
        match = re.fullmatch(r"/api/users/(\d+)", path)
        if match and method == "DELETE":
            self.users.pop(int(match.group(1)), None)
            self.events.append("user:delete")
            return web.Response(status=204)
        if path == "/api/nodes/actions/restart-all" and method == "POST":
            self.events.append(f"nodes:restart:force={body['forceRestart']}")
            return self._ok({"eventSent": True})
        match = re.fullmatch(r"/api/subscriptions/connection-keys/(\d+)", path)
        if match:
            user = self.users[int(match.group(1))]
            return self._ok({"enabledKeys": [LINK.format(uid=user["vlessUuid"])]})
        return web.json_response({"message": "nf"}, status=404)


@pytest.fixture
async def remnawave() -> AsyncIterator[tuple[FakeRemnawave, RemnawaveBackend]]:
    fake = FakeRemnawave()
    async with TestServer(fake.app()) as server:
        backend = RemnawaveBackend(str(server.make_url("")), "tok", squad_uuids=[SQUAD])
        yield fake, backend
        await backend.close()


async def test_remnawave_rules_match_the_numeric_user_id(remnawave: tuple[FakeRemnawave, RemnawaveBackend]) -> None:
    fake, backend = remnawave
    assert isinstance(backend, TgOnlyManaged) and not isinstance(backend, TgOnlyRouting)
    access = await backend.ensure_tg_only(spec())
    # the nodes reload once more after the user is enabled (a node still restarting can miss it otherwise)
    assert fake.events == [
        "user:create:DISABLED",
        "config:rules:user=DISABLED",
        "user:active",
        "nodes:restart:force=True",
    ]
    assert_confined(fake.config, ["101"])  # the Xray client is named after the user id (node handler: id.toString())
    assert access.links == (LINK.format(uid=CLIENT),)
    assert fake.other_writes == 0 and not tg_rules(fake.other_config)  # a profile outside the squads is not touched


async def test_remnawave_is_idempotent_and_follows_a_recreated_user(
    remnawave: tuple[FakeRemnawave, RemnawaveBackend],
) -> None:
    fake, backend = remnawave
    await backend.ensure_tg_only(spec())
    writes = fake.config_writes
    fake.events.clear()
    await backend.ensure_tg_only(spec())
    assert fake.config_writes == writes and len(fake.users) == 1
    assert not any(e.startswith("nodes:restart") for e in fake.events)  # nothing changed: no restart
    other = str(uuidlib.UUID(int=9))
    await backend.ensure_tg_only(spec(uuid=other))  # a new client id recreates the user: the rule follows its new id
    assert list(fake.users) == [102] and fake.users[102]["status"] == "ACTIVE"
    assert_confined(fake.config, ["102"])


async def test_remnawave_removal_deletes_the_user_then_the_rules(
    remnawave: tuple[FakeRemnawave, RemnawaveBackend],
) -> None:
    fake, backend = remnawave
    await backend.ensure_tg_only(spec())
    fake.events.clear()
    await backend.remove_tg_only(TG_ONLY_USERNAME)
    assert fake.events == ["user:delete", "config:clean:user=absent"]
    assert fake.users == {} and fake.config["routing"]["rules"] == [OPERATOR_RULE]


async def test_remnawave_without_a_squad_inbound_is_refused() -> None:
    fake = FakeRemnawave()
    async with TestServer(fake.app()) as server:
        backend = RemnawaveBackend(
            str(server.make_url("")), "tok", squad_uuids=["33333333-3333-4333-8333-333333333333"]
        )
        with pytest.raises(Exception, match="no inbounds"):
            await backend.ensure_tg_only(spec())
        await backend.close()
    assert fake.users == {}


# ---------------------------------------------------------------------------------------------
# PasarGuard
# ---------------------------------------------------------------------------------------------
class FakePasarGuard(FakePanelServer):
    def __init__(self) -> None:
        super().__init__()
        self.users: dict[str, dict[str, Any]] = {}
        self.next_id = 6
        self.restart_nodes: list[str] = []

    def _view(self, name: str) -> dict[str, Any]:
        return {**self.users[name], "subscription_url": f"/sub/tok-{name}"}

    async def handle(self, request: web.Request) -> web.StreamResponse:
        path, method = request.path, request.method
        if path.startswith("/sub/tok-") and path.endswith("/links"):
            row = self.users.get(path.removeprefix("/sub/tok-").removesuffix("/links"))
            if row is None:
                return web.Response(status=404)
            return web.Response(text=LINK.format(uid=row["proxy_settings"]["vless"]["id"]) + "\n")
        if request.headers.get("Authorization") != "ApiKey key":
            return web.json_response({}, status=401)
        body = await request.json() if request.can_read_body else None
        if path == "/api/cores":
            core = {"id": 1, "name": "Default", "type": "xray", "config": self.config}
            return web.json_response(
                {"count": 1, "cores": [{**core, "exclude_inbound_tags": [], "fallbacks_inbound_tags": []}]}
            )
        if path == "/api/core/1" and method == "PUT":
            self.config, self.config_writes = body["config"], self.config_writes + 1
            self.restart_nodes.append(request.query.get("restart_nodes", ""))
            user = next(iter(self.users.values()), {"status": "absent"})
            self.events.append(f"config:{'rules' if tg_rules(body['config']) else 'clean'}:user={user['status']}")
            return web.json_response({"id": 1})
        if path == "/api/user" and method == "POST":
            if body["status"] not in ("active", "on_hold"):  # the real panel refuses to create a disabled user
                return web.json_response({"detail": {"status": "status must be in active, on_hold"}}, status=422)
            self.next_id += 1
            row = {"id": self.next_id, "username": body["username"], "proxy_settings": body["proxy_settings"]}
            row.update(status=body["status"], data_limit=body["data_limit"], used_traffic=0, expire=None, hwid_limit=0)
            self.users[body["username"]] = row
            self.events.append(f"user:create:{row['status']}")
            return web.json_response(self._view(body["username"]))
        name = path.removeprefix("/api/user/") if path.startswith("/api/user/") else ""
        if name in self.users:
            if method == "GET":
                return web.json_response(self._view(name))
            if method == "PUT":
                self.users[name].update(status=body["status"], data_limit=body["data_limit"])
                self.events.append(f"user:{body['status']}")
                return web.json_response(self._view(name))
            if method == "DELETE":
                del self.users[name]
                self.events.append("user:delete")
                return web.Response(status=204)
        return web.json_response({"detail": "nf"}, status=404)


@pytest.fixture
async def pasarguard() -> AsyncIterator[tuple[FakePasarGuard, PasarGuardBackend]]:
    fake = FakePasarGuard()
    async with TestServer(fake.app()) as server:
        backend = PasarGuardBackend(str(server.make_url("")), "key", group_ids=[1])
        yield fake, backend
        await backend.close()


async def test_pasarguard_rules_match_the_numeric_user_id(pasarguard: tuple[FakePasarGuard, PasarGuardBackend]) -> None:
    fake, backend = pasarguard
    assert isinstance(backend, TgOnlyManaged) and not isinstance(backend, TgOnlyRouting)
    access = await backend.ensure_tg_only(spec())
    assert fake.events == ["user:create:active", "user:disabled", "config:rules:user=disabled", "user:active"]
    assert_confined(fake.config, ["7"])  # node.user email = str(user.id)
    assert fake.restart_nodes == ["true"] and access.links == (LINK.format(uid=CLIENT),)
    writes = fake.config_writes
    await backend.ensure_tg_only(spec())
    assert fake.config_writes == writes and len(fake.users) == 1


async def test_pasarguard_rotation_and_removal(pasarguard: tuple[FakePasarGuard, PasarGuardBackend]) -> None:
    fake, backend = pasarguard
    await backend.ensure_tg_only(spec())
    await backend.ensure_tg_only(spec(uuid=str(uuidlib.UUID(int=9))))  # recreated under a new id: the rule follows
    assert_confined(fake.config, ["8"])
    fake.events.clear()
    await backend.remove_tg_only(TG_ONLY_USERNAME)
    assert fake.events == ["user:delete", "config:clean:user=absent"] and fake.config["routing"]["rules"] == [
        OPERATOR_RULE
    ]


# ---------------------------------------------------------------------------------------------
# Marzneshin
# ---------------------------------------------------------------------------------------------
class FakeMarzneshin(FakePanelServer):
    def __init__(self) -> None:
        super().__init__()
        self.users: dict[str, dict[str, Any]] = {}
        self.next_id = 20
        self.second_config = xray_config()  # a second node with its own config
        self.second_writes = 0

    def _view(self, name: str) -> dict[str, Any]:
        return {**self.users[name], "subscription_url": f"/sub/{name}/tok"}

    async def handle(self, request: web.Request) -> web.StreamResponse:
        path, method = request.path, request.method
        if path == "/api/admins/token":
            return web.json_response({"access_token": "tok"})
        if path.startswith("/sub/") and path.endswith("/links"):
            row = self.users.get(path.split("/")[2])
            if row is None:
                return web.Response(status=404)
            return web.Response(text=LINK.format(uid=str(uuidlib.UUID(row["key"]))) + "\n")
        if request.headers.get("Authorization") != "Bearer tok":
            return web.json_response({}, status=401)
        body = await request.json() if request.can_read_body else None
        if path == "/api/nodes":
            backends = [{"name": "xray", "backend_type": "xray", "version": "26.10.31", "running": True}]
            nodes = [
                {"id": 1, "status": "healthy", "backends": backends},
                {"id": 2, "status": "healthy", "backends": backends},
                {"id": 3, "status": "disabled", "backends": backends},
            ]
            return web.json_response({"items": nodes, "pages": 1})
        match = re.fullmatch(r"/api/nodes/(\d)/xray/config", path)
        if match:
            node = int(match.group(1))
            current = self.config if node == 1 else self.second_config
            if method == "PUT":
                assert body["format"] == 1
                updated = json.loads(body["config"])
                if node == 1:
                    self.config, self.config_writes = updated, self.config_writes + 1
                    user = next(iter(self.users.values()), {"enabled": None})
                    state = "absent" if user["enabled"] is None else ("enabled" if user["enabled"] else "disabled")
                    self.events.append(f"config:{'rules' if tg_rules(updated) else 'clean'}:user={state}")
                else:
                    self.second_config, self.second_writes = updated, self.second_writes + 1
                return web.json_response({})
            return web.json_response({"config": json.dumps(current), "format": 1})
        if path == "/api/users" and method == "POST":
            self.next_id += 1
            row = {
                "id": self.next_id,
                "username": body["username"],
                "key": body["key"],
                "enabled": True,
                "data_limit": body["data_limit"],
                "used_traffic": 0,
                "expire_date": None,
            }
            self.users[body["username"]] = row
            self.events.append("user:create")
            return web.json_response(self._view(body["username"]))
        match = re.fullmatch(r"/api/users/([^/]+)(?:/(enable|disable))?", path)
        if match and match.group(1) in self.users:
            name, action = match.groups()
            if action:
                self.users[name]["enabled"] = action == "enable"
                self.events.append(f"user:{action}")
                return web.json_response(self._view(name))
            if method == "GET":
                return web.json_response(self._view(name))
            if method == "PUT":
                self.users[name]["data_limit"] = body["data_limit"]
                return web.json_response(self._view(name))
            if method == "DELETE":
                del self.users[name]
                self.events.append("user:delete")
                return web.Response(status=204)
        return web.json_response({"detail": "nf"}, status=404)


@pytest.fixture
async def marzneshin() -> AsyncIterator[tuple[FakeMarzneshin, MarzneshinBackend]]:
    fake = FakeMarzneshin()
    async with TestServer(fake.app()) as server:
        backend = MarzneshinBackend(str(server.make_url("")), username="a", password="b", service_ids=[1])
        yield fake, backend
        await backend.close()


async def test_marzneshin_rules_match_id_dot_username_on_every_enabled_node(
    marzneshin: tuple[FakeMarzneshin, MarzneshinBackend],
) -> None:
    fake, backend = marzneshin
    assert isinstance(backend, TgOnlyManaged) and not isinstance(backend, TgOnlyRouting)
    access = await backend.ensure_tg_only(spec())
    assert fake.events == ["user:create", "user:disable", "config:rules:user=disabled", "user:enable"]
    assert_confined(fake.config, ["21.tgonly"])  # marznode: email = f"{user.id}.{user.username}"
    assert_confined(fake.second_config, ["21.tgonly"])
    assert access.links == (LINK.format(uid=CLIENT),)
    writes = fake.config_writes
    await backend.ensure_tg_only(spec())
    assert fake.config_writes == writes and fake.second_writes == 1


async def test_marzneshin_removal_deletes_the_user_then_the_rules(
    marzneshin: tuple[FakeMarzneshin, MarzneshinBackend],
) -> None:
    fake, backend = marzneshin
    await backend.ensure_tg_only(spec())
    fake.events.clear()
    await backend.remove_tg_only(TG_ONLY_USERNAME)
    assert fake.events == ["user:delete", "config:clean:user=absent"]
    assert not tg_rules(fake.second_config) and fake.config["routing"]["rules"] == [OPERATOR_RULE]


# ---------------------------------------------------------------------------------------------
# Rules helper
# ---------------------------------------------------------------------------------------------
def test_config_rules_keep_existing_routing_and_are_idempotent() -> None:
    config = xray_config()
    once, changed = set_tg_only_config(config, ["5"])
    assert changed and config == xray_config()
    twice, changed = set_tg_only_config(once, ["5"])
    assert not changed and twice == once
    gone, changed = set_tg_only_config(once, [])
    assert changed and gone["routing"]["rules"] == [OPERATOR_RULE]
    _, changed = set_tg_only_config(gone, [])
    assert not changed
    quiet = xray_config()
    quiet["inbounds"][0]["sniffing"] = {"enabled": False}
    out, changed = set_tg_only_config(quiet, ["5"])
    assert (
        changed
        and out["inbounds"][0]["sniffing"]["enabled"] is True
        and {"http", "tls"} <= set(out["inbounds"][0]["sniffing"]["destOverride"])
    )


# ---------------------------------------------------------------------------------------------
# Eligibility and convergence through tg_only
# ---------------------------------------------------------------------------------------------
class ManagedFake:
    """A TgOnlyManaged backend that records the calls."""

    kind = PanelKind.REMNAWAVE

    def __init__(self) -> None:
        self.events: list[str] = []
        self.specs: list[AccountSpec] = []
        self.used = 0
        self.links: tuple[str, ...] = ()
        self.fail = False

    async def ensure_tg_only(self, spec: AccountSpec) -> Any:
        from app.services.panels.base import PanelError, TgOnlyAccess

        if self.fail:
            raise PanelError("boom")
        self.events.append("ensure")
        self.specs.append(spec)
        return TgOnlyAccess(self.links or (LINK.format(uid=spec.uuid),))

    async def remove_tg_only(self, email: str) -> None:
        self.events.append(f"remove:{email}")

    async def get_used_bytes(self, target: int | None, email: str) -> int | None:
        return self.used

    async def reset_traffic(self, target: int | None, email: str) -> None:
        self.events.append("reset")


class Registry:
    def __init__(self, backends: dict[int, Any]) -> None:
        self.backends = backends

    async def get(self, panel_id: int) -> Any:
        return self.backends[panel_id]


async def add_panel(
    sf: SessionFactory, panel_id: int, kind: PanelKind, flag: str | None, *, location_id: int | None = None
) -> None:
    async with sf.begin() as session:
        options: dict[str, Any] = {}
        if flag is not None:
            lid = location_id or panel_id
            session.add(Location(id=lid, name=f"L{lid}", flag=flag))
            options["location_id"] = lid
        session.add(
            Panel(id=panel_id, name=f"p{panel_id}", kind=kind, url="https://p.example", options=json.dumps(options))
        )


async def test_every_managed_kind_is_eligible_by_its_location(session_factory: SessionFactory) -> None:
    await add_panel(session_factory, 1, PanelKind.REMNAWAVE, "🇩🇪")
    await add_panel(session_factory, 2, PanelKind.MARZBAN, "🇳🇱")
    await add_panel(session_factory, 3, PanelKind.PASARGUARD, "🇫🇮")
    await add_panel(session_factory, 4, PanelKind.MARZNESHIN, "🇸🇪")
    await add_panel(session_factory, 5, PanelKind.REMNAWAVE, "🇷🇺")  # excluded country
    await add_panel(session_factory, 6, PanelKind.MARZBAN, None)  # no location: unknown country
    await add_panel(session_factory, 7, PanelKind.HIDDIFY, "🇩🇪")  # no routing API
    servers, skipped = await tg_only.load_servers(session_factory, make_settings())
    assert [(s.panel_id, s.managed, s.country, s.email) for s in servers] == [
        (1, True, "DE", "tgonly"),
        (2, True, "NL", "tgonly"),
        (3, True, "FI", "tgonly"),
        (4, True, "SE", "tgonly"),
    ]
    assert all(s.remote_id is None and s.inbound_id == 0 and s.endpoints == () for s in servers)
    assert [(s.panel_id, s.reason) for s in skipped] == [(5, "country"), (6, "unknown_country")]
    allowed, _ = await tg_only.load_servers(session_factory, make_settings(tg_only_allow_unknown_country=True))
    assert [s.panel_id for s in allowed] == [1, 2, 3, 4, 6]
    off, _ = await tg_only.load_servers(session_factory, make_settings(tg_only_enabled=False))
    assert off == []


async def test_a_managed_server_is_offered_with_the_panels_links(session_factory: SessionFactory) -> None:
    await add_panel(session_factory, 1, PanelKind.REMNAWAVE, "🇩🇪")
    fake = ManagedFake()
    registry = Registry({1: fake})
    report = await tg_only.reconcile(session_factory, registry, make_settings())
    assert report.ready == [1] and fake.events == ["ensure"]
    assert fake.specs[0].email == "tgonly" and fake.specs[0].enabled and fake.specs[0].traffic_limit_bytes == 0
    rendered = await tg_only.render_configs(session_factory, make_settings(), "ru")
    assert rendered is not None and rendered.servers == 1
    outbound = rendered.configs[0]["outbounds"][0]
    vnext = outbound["settings"]["vnext"][0]
    assert vnext["address"] == "203.0.113.9" and vnext["users"][0]["id"] == fake.specs[0].uuid
    assert outbound["streamSettings"]["realitySettings"]["fingerprint"] == "firefox"  # compact hello
    states = await tg_only.load_states(session_factory, [1])
    assert states[1].ok and len(states[1].links) == 1
    await tg_only.reconcile(session_factory, registry, make_settings())  # stable client id across runs
    assert fake.specs[1].uuid == fake.specs[0].uuid


async def test_the_compact_hello_can_be_switched_off(session_factory: SessionFactory) -> None:
    await add_panel(session_factory, 1, PanelKind.MARZBAN, "🇩🇪")
    await tg_only.reconcile(session_factory, Registry({1: ManagedFake()}), make_settings(reality_compact_hello=False))
    rendered = await tg_only.render_configs(session_factory, make_settings(reality_compact_hello=False), "ru")
    assert rendered is not None
    assert rendered.configs[0]["outbounds"][0]["streamSettings"]["realitySettings"]["fingerprint"] == "chrome"


async def test_a_panel_without_usable_links_or_failing_is_not_offered(session_factory: SessionFactory) -> None:
    await add_panel(session_factory, 1, PanelKind.PASARGUARD, "🇩🇪")
    fake = ManagedFake()
    fake.links = ("vmess://abc", "not a link")
    report = await tg_only.reconcile(session_factory, Registry({1: fake}), make_settings())
    assert report.ready == [] and "no usable" in report.failed[1]
    fake.links, fake.fail = (), True
    report = await tg_only.reconcile(session_factory, Registry({1: fake}), make_settings())
    assert "boom" in report.failed[1]
    assert await tg_only.render_configs(session_factory, make_settings(), "ru") is None


async def test_a_managed_server_that_stops_being_eligible_is_removed(session_factory: SessionFactory) -> None:
    await add_panel(session_factory, 1, PanelKind.MARZNESHIN, "🇩🇪")
    fake = ManagedFake()
    registry = Registry({1: fake})
    await tg_only.reconcile(session_factory, registry, make_settings())
    fake.events.clear()
    report = await tg_only.reconcile(session_factory, registry, make_settings(tg_only_excluded_countries="DE"))
    assert report.removed == [1] and fake.events == ["remove:tgonly"]
    assert await tg_only.load_states(session_factory, [1]) == {}
    switched_off = await tg_only.reconcile(session_factory, registry, make_settings(tg_only_enabled=False))
    assert switched_off.ready == []


async def test_the_monthly_limit_applies_to_a_managed_account(session_factory: SessionFactory) -> None:
    await add_panel(session_factory, 1, PanelKind.REMNAWAVE, "🇩🇪")
    fake = ManagedFake()
    fake.used = 3 * 1024**3
    await tg_only.reconcile(session_factory, Registry({1: fake}), make_settings(tg_only_monthly_limit_gb=2))
    assert fake.specs[0].traffic_limit_bytes == 2 * 1024**3 and fake.specs[0].enabled is False  # exhausted stays off


async def test_x_ui_panels_still_use_their_own_capability(session_factory: SessionFactory) -> None:
    from tests.test_tg_only import FakeTgPanel, seed

    await seed(session_factory)
    await add_panel(session_factory, 2, PanelKind.MARZBAN, "🇳🇱", location_id=2)
    xui, managed = FakeTgPanel(), ManagedFake()
    report = await tg_only.reconcile(session_factory, Registry({1: xui, 2: managed}), make_settings())
    assert sorted(report.ready) == [1, 2] and xui.events == ["rules:tg-only", "client:tg-only"]
    rendered = await tg_only.render_configs(session_factory, make_settings(), "ru")
    assert rendered is not None and rendered.servers == 2


def test_the_panel_backends_declare_the_right_capability() -> None:
    for backend in (RemnawaveBackend, MarzbanBackend, PasarGuardBackend, MarzneshinBackend):
        assert hasattr(backend, "ensure_tg_only") and hasattr(backend, "remove_tg_only")
        assert not hasattr(backend, "apply_tg_only")
    assert not isinstance(ManagedFake(), TgOnlyRouting) and isinstance(ManagedFake(), TgOnlyManaged)


# ---------------------------------------------------------------------------------------------
# Bot commands
# ---------------------------------------------------------------------------------------------
def proxy_listed(**overrides: Any) -> bool:
    from app.core.modules import ModuleRegistry
    from app.modules.tg_proxy import module

    commands = ModuleRegistry([module]).commands(make_settings(**overrides), admin=False)
    return any(c.command == "proxy" for c in commands)


def test_the_proxy_command_is_listed_only_while_a_proxy_is_offered() -> None:
    assert not proxy_listed(tg_proxy_access="off", tg_socks_enabled=False)  # the product defaults
    assert proxy_listed(tg_proxy_access="everyone", tg_socks_enabled=False)
    assert proxy_listed(tg_proxy_access="subscribers", tg_socks_enabled=False)
    assert proxy_listed(tg_proxy_access="off", tg_socks_enabled=True)
    assert not proxy_listed(tg_proxy_access="everyone", disabled_modules="tg_proxy")


def test_other_commands_are_not_affected() -> None:
    from app.core.modules import BotCommandSpec, FeatureModule, ModuleRegistry

    plain = FeatureModule(name="x", title_ru="x", title_en="x", commands=[BotCommandSpec("hello", "a", "b")])
    assert [c.command for c in ModuleRegistry([plain]).commands(make_settings(), admin=False)] == ["hello"]
