"""Panels deployed by the installer: idempotent provisioning against fake panel APIs, no secrets in output."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.__main__ import build_local_setup, build_parser, cmd_bootstrap_local
from app.db.models import Panel, PanelKind
from app.db.session import SessionFactory
from app.services import panel_setup
from app.services.local_setup import LocalSetup, SetupError, bootstrap_local
from app.services.panel_setup import PanelInstall, PanelSetupError, provision_panel
from app.utils.crypto import SecretBox
from tests.fakes import FakeCapablePanel

ADMIN_PASSWORD = "Nx0123456789abcdef0123456789abcdefAy1"
NODE_KEY = "node-secret-key-value"
TOKEN = "issued-api-token-value"


async def _no_sleep(_: float) -> None:
    return None


@dataclass
class FakePanel:
    """A tiny stateful stand-in for a panel's HTTP API; ``calls`` records ``METHOD path``."""

    kind: PanelKind
    calls: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    registered: bool = False

    async def close(self) -> None:
        return None

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        form: Mapping[str, str] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> tuple[int, Any]:
        self.calls.append(f"{method} {path}")
        handler = getattr(self, f"_{self.kind.value}")
        return handler(method, path, json, form, auth)

    # ---- Remnawave -------------------------------------------------------------------------------------------
    def _remnawave(self, method: str, path: str, body: Any, form: Any, auth: Any) -> tuple[int, Any]:
        st = self.state
        wrap = lambda value: (200, {"response": value})  # noqa: E731
        if path == "/api/auth/status":
            return wrap({"isRegisterAllowed": not self.registered, "isLoginAllowed": True})
        if path == "/api/auth/register":
            self.registered = True
            return wrap({"accessToken": "jwt"})
        if path == "/api/auth/login":
            return wrap({"accessToken": "jwt"}) if self.registered else (401, None)
        if path == "/api/tokens" and method == "POST":
            st["token"] = TOKEN
            return wrap({"token": TOKEN})
        if auth is None or auth[1] not in ("jwt", TOKEN):
            return 401, None
        if path == "/api/keygen":
            return wrap({"secretKey": NODE_KEY})
        if path == "/api/config-profiles" and method == "GET":
            return wrap({"configProfiles": st.get("profiles", [])})
        if path == "/api/config-profiles":
            profile = {"uuid": "p1", "name": body["name"], "inbounds": [{"uuid": "i1", "tag": panel_setup.INBOUND_TAG}]}
            st.setdefault("profiles", []).append(profile)
            st["profile_config"] = body["config"]
            return wrap(profile)
        if path == "/api/nodes" and method == "GET":
            return wrap(st.get("nodes", []))
        if path == "/api/nodes":
            st.setdefault("nodes", []).append(body)
            return wrap(body)
        if path == "/api/hosts" and method == "GET":
            return wrap(st.get("hosts", []))
        if path == "/api/hosts":
            st.setdefault("hosts", []).append(body)
            return wrap(body)
        if path == "/api/internal-squads" and method == "GET":
            return wrap({"internalSquads": [{"uuid": "s1", "inbounds": st.get("squad_inbounds", [])}]})
        if path == "/api/internal-squads":
            st["squad_inbounds"] = [{"uuid": u} for u in body["inbounds"]]
            return wrap({})
        return 404, None

    # ---- Marzban ---------------------------------------------------------------------------------------------
    def _marzban(self, method: str, path: str, body: Any, form: Any, auth: Any) -> tuple[int, Any]:
        st = self.state
        if path == "/api/admin/token":
            return (200, {"access_token": "jwt"}) if form is None or form.get("password") else (401, None)
        if auth is None:
            return 401, None
        if path == "/api/core/config" and method == "GET":
            return 200, st.setdefault("config", {"inbounds": [{"tag": "Shadowsocks TCP"}], "outbounds": []})
        if path == "/api/core/config":
            st["config"] = body
            # like the real panel: every new inbound gets a default host
            hosts = st.setdefault("hosts", {})
            for inbound in body.get("inbounds", []):
                hosts.setdefault(inbound["tag"], [{"remark": "default", "address": "{SERVER_IP}", "port": None}])
            return 200, body
        if path == "/api/hosts" and method == "GET":
            return 200, st.setdefault("hosts", {})
        if path == "/api/hosts":
            st["hosts"] = {**st.get("hosts", {}), **body}
            return 200, st["hosts"]
        return 404, None

    # ---- PasarGuard ------------------------------------------------------------------------------------------
    def _pasarguard(self, method: str, path: str, body: Any, form: Any, auth: Any) -> tuple[int, Any]:
        st = self.state
        if path == "/api/admin/token":
            return 200, {"access_token": "jwt"}
        if path == "/api/system/resources":
            return (200, {}) if auth == ("ApiKey", TOKEN) else (401, None)
        if auth != ("Bearer", "jwt"):
            return 401, None
        collections = {
            "/api/cores": ("cores", "cores"),
            "/api/groups": ("groups", "groups"),
            "/api/nodes": ("nodes", "nodes"),
        }
        if path in collections and method == "GET":
            key, field_name = collections[path]
            return 200, {field_name: st.get(key, [])}
        if path == "/api/core":
            item = {"id": 1, **body}
            st.setdefault("cores", []).append(item)
            return 201, item
        if path == "/api/group":
            item = {"id": 7, **body}
            st.setdefault("groups", []).append(item)
            return 201, item
        if path == "/api/node":
            # the real schema accepts an IP address or a dotted domain only
            if "." not in body.get("address", ""):
                return 422, {"detail": "Invalid address format"}
            st.setdefault("nodes", []).append(body)
            return 201, body
        if path == "/api/hosts" and method == "GET":
            return 200, st.get("hosts", [])
        if path == "/api/host/":
            if "priority" not in body:
                return 422, {"detail": "priority: Field required"}
            st.setdefault("hosts", []).append(body)
            return 201, body
        if path == "/api/api_key":
            return 201, {"api_key": TOKEN}
        return 404, None

    # ---- Marzneshin ------------------------------------------------------------------------------------------
    def _marzneshin(self, method: str, path: str, body: Any, form: Any, auth: Any) -> tuple[int, Any]:
        st = self.state
        if path == "/api/admins/token":
            return 200, {"access_token": "jwt"}
        if auth is None:
            return 401, None
        if path == "/api/nodes" and method == "GET":
            return 200, {"items": st.get("nodes", [])}
        if path == "/api/nodes":
            st.setdefault("nodes", []).append(body)
            return 200, body
        if path == "/api/inbounds":
            # the node reports its inbounds once it is connected; the panel then adds a default host for each
            connected = bool(st.get("nodes"))
            if connected and "hosts" not in st:
                st["hosts"] = [{"id": 1, "remark": "default", "address": "{SERVER_IP}", "port": None}]
            return 200, {"items": [{"id": 5, "tag": panel_setup.INBOUND_TAG}] if connected else []}
        if path == "/api/inbounds/5/hosts" and method == "GET":
            return 200, {"items": st.get("hosts", [])}
        if path == "/api/inbounds/5/hosts":
            st.setdefault("hosts", []).append({"id": 2, **body})
            return 200, body
        if path.startswith("/api/inbounds/hosts/") and method == "DELETE":
            st["hosts"] = [h for h in st.get("hosts", []) if str(h["id"]) != path.rsplit("/", 1)[1]]
            return 200, {}
        if path == "/api/services" and method == "GET":
            return 200, {"items": st.get("services", [])}
        if path == "/api/services":
            item = {"id": 3, "name": body["name"], "inbound_ids": body["inbound_ids"]}
            st.setdefault("services", []).append(item)
            return 200, item
        return 404, None


def install_for(kind: PanelKind, tmp_path: Path, **overrides: Any) -> PanelInstall:
    values: dict[str, Any] = {
        "kind": kind,
        "url": "http://panel:8000",
        "admin_user": "admin1",
        "admin_password": ADMIN_PASSWORD,
        "reality_port": 8445,
        "reality_sni": "vpn.example.com",
        "reality_dest": "caddy:8444",
        "endpoint_address": "203.0.113.5",
        "node_address": "pg-node.internal" if kind == PanelKind.PASARGUARD else "node",
        "node_secret_out": str(tmp_path / "rw-key") if kind == PanelKind.REMNAWAVE else None,
        "node_cert": "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----"
        if kind == PanelKind.PASARGUARD
        else None,
        "node_api_key": "11111111-2222-3333-4444-555555555555" if kind == PanelKind.PASARGUARD else None,
    }
    values.update(overrides)
    return PanelInstall(**values)


def test_reality_keys_have_the_xray_format() -> None:
    private, public = panel_setup.reality_keypair()
    assert len(private) == len(public) == 43 and "=" not in private + public
    other = panel_setup.reality_keypair()
    assert other[0] != private


def test_the_inbound_borrows_the_site_and_has_no_clients() -> None:
    config = panel_setup.new_reality_config(8445, "vpn.example.com", "caddy:8444")
    inbound = config["inbounds"][0]
    assert inbound["port"] == 8445 and inbound["settings"]["clients"] == []
    reality = inbound["streamSettings"]["realitySettings"]
    assert reality["dest"] == "caddy:8444" and reality["serverNames"] == ["vpn.example.com"]
    assert reality["privateKey"] and len(reality["shortIds"][0]) == 8


async def test_remnawave_flow_is_idempotent_and_writes_the_node_key(tmp_path: Path) -> None:
    fake = FakePanel(PanelKind.REMNAWAVE)
    install = install_for(PanelKind.REMNAWAVE, tmp_path)
    first = await provision_panel(fake, install, _no_sleep)
    assert first.api_token == TOKEN and first.options == {"direct": True}
    assert (tmp_path / "rw-key").read_text(encoding="utf-8") == NODE_KEY
    assert "POST /api/auth/register" in fake.calls
    assert fake.state["hosts"][0]["port"] == 443 and fake.state["hosts"][0]["sni"] == "vpn.example.com"
    assert fake.state["nodes"][0]["address"] == "node"
    assert fake.state["squad_inbounds"] == [{"uuid": "i1"}]
    writes = [c for c in fake.calls if c.split()[0] in {"POST", "PATCH"}]
    fake.calls.clear()
    again = await provision_panel(fake, install_for(PanelKind.REMNAWAVE, tmp_path, existing_token=TOKEN), _no_sleep)
    assert again.api_token == TOKEN
    assert not [c for c in fake.calls if c.split()[0] in {"POST", "PATCH"}]
    assert len(fake.state["hosts"]) == len(fake.state["nodes"]) == len(fake.state["profiles"]) == 1
    assert writes  # the first run did change things


async def test_remnawave_logs_in_when_the_administrator_exists(tmp_path: Path) -> None:
    fake = FakePanel(PanelKind.REMNAWAVE, registered=True)
    await provision_panel(fake, install_for(PanelKind.REMNAWAVE, tmp_path), _no_sleep)
    assert "POST /api/auth/login" in fake.calls and "POST /api/auth/register" not in fake.calls


async def test_marzban_replaces_the_sample_inbound_once(tmp_path: Path) -> None:
    fake = FakePanel(PanelKind.MARZBAN)
    result = await provision_panel(fake, install_for(PanelKind.MARZBAN, tmp_path), _no_sleep)
    assert result.username == "admin1" and result.password == ADMIN_PASSWORD and result.api_token is None
    assert result.options == {"inbound_tags": [panel_setup.INBOUND_TAG]}
    tags = [i["tag"] for i in fake.state["config"]["inbounds"]]
    assert tags == [panel_setup.INBOUND_TAG]
    reality = fake.state["config"]["inbounds"][0]["streamSettings"]["realitySettings"]
    key = reality["privateKey"]
    assert reality["publicKey"] == panel_setup.reality_public_key(key)  # Marzban reads it instead of `xray x25519`
    fake.calls.clear()
    await provision_panel(fake, install_for(PanelKind.MARZBAN, tmp_path), _no_sleep)
    assert not [c for c in fake.calls if c.startswith("PUT")]
    assert fake.state["config"]["inbounds"][0]["streamSettings"]["realitySettings"]["privateKey"] == key
    host = fake.state["hosts"][panel_setup.INBOUND_TAG][0]
    assert (host["address"], host["port"], host["fingerprint"]) == ("203.0.113.5", 443, "firefox")


async def test_marzban_adds_the_public_key_to_an_older_inbound(tmp_path: Path) -> None:
    fake = FakePanel(PanelKind.MARZBAN)
    await provision_panel(fake, install_for(PanelKind.MARZBAN, tmp_path), _no_sleep)
    reality = fake.state["config"]["inbounds"][0]["streamSettings"]["realitySettings"]
    public = reality.pop("publicKey")
    await provision_panel(fake, install_for(PanelKind.MARZBAN, tmp_path), _no_sleep)
    assert fake.state["config"]["inbounds"][0]["streamSettings"]["realitySettings"]["publicKey"] == public


def test_reality_public_key_matches_the_generated_pair() -> None:
    private, public = panel_setup.reality_keypair()
    assert panel_setup.reality_public_key(private) == public


async def test_pasarguard_creates_group_node_host_and_key(tmp_path: Path) -> None:
    fake = FakePanel(PanelKind.PASARGUARD)
    result = await provision_panel(fake, install_for(PanelKind.PASARGUARD, tmp_path), _no_sleep)
    assert result.api_token == TOKEN and result.options == {"group_ids": [7]}
    assert fake.state["nodes"][0]["server_ca"].startswith("-----BEGIN CERTIFICATE")
    assert fake.state["hosts"][0]["address"] == ["203.0.113.5"]
    fake.calls.clear()
    again = await provision_panel(
        fake, install_for(PanelKind.PASARGUARD, tmp_path, existing_token=TOKEN, node_cert=None), _no_sleep
    )
    assert again.api_token == TOKEN
    assert not [c for c in fake.calls if c.split()[0] == "POST" and "token" not in c]


async def test_marzneshin_waits_for_the_node_inbound(tmp_path: Path) -> None:
    fake = FakePanel(PanelKind.MARZNESHIN)
    result = await provision_panel(fake, install_for(PanelKind.MARZNESHIN, tmp_path), _no_sleep)
    assert result.options == {"service_ids": [3]} and result.username == "admin1"
    assert fake.state["services"][0]["inbound_ids"] == [5]
    # only the public entry point is left: the default host with the internal port is gone
    assert [h["remark"] for h in fake.state["hosts"]] == [panel_setup.HOST_REMARK]
    fake.calls.clear()
    await provision_panel(fake, install_for(PanelKind.MARZNESHIN, tmp_path), _no_sleep)
    assert not [c for c in fake.calls if c.split()[0] in {"POST", "PUT"} and "token" not in c]


async def test_an_unreachable_panel_is_reported_without_secrets(tmp_path: Path) -> None:
    class Down:
        async def request(self, *args: Any, **kwargs: Any) -> tuple[int, Any]:
            return 0, None

        async def close(self) -> None:
            return None

    with pytest.raises(PanelSetupError) as info:
        await provision_panel(Down(), install_for(PanelKind.MARZBAN, tmp_path), _no_sleep)
    assert ADMIN_PASSWORD not in str(info.value)


# ---- registration through bootstrap_local ----------------------------------------------------------------------


@dataclass
class OneRegistry:
    kind: PanelKind
    session_factory: SessionFactory

    async def get(self, panel_id: int) -> Any:
        return FakeCapablePanel(kind=self.kind)


async def _register(session_factory: SessionFactory, fake: FakePanel, install: PanelInstall) -> Any:
    box = SecretBox("test-secret-key-0123456789abcdef")
    registry = OneRegistry(install.kind, session_factory)
    result = await bootstrap_local(
        session_factory, registry, box, LocalSetup(panel=install), sleep=_no_sleep, http_factory=lambda _i: fake
    )
    return result, box


async def test_remnawave_is_registered_once_with_its_token(session_factory: SessionFactory, tmp_path: Path) -> None:
    fake = FakePanel(PanelKind.REMNAWAVE)
    install = install_for(PanelKind.REMNAWAVE, tmp_path)
    result, box = await _register(session_factory, fake, install)
    again, _ = await _register(session_factory, fake, install)
    assert result.panel["panel"] == "created" and again.panel["panel"] == "unchanged"
    async with session_factory() as session:
        rows = list(await session.scalars(select(Panel)))
    assert len(rows) == 1 and rows[0].kind == PanelKind.REMNAWAVE
    assert rows[0].api_token and box.decrypt(rows[0].api_token) == TOKEN
    assert json.loads(rows[0].options or "{}")["direct"] is True
    dump = json.dumps(result.as_dict()) + json.dumps(again.as_dict())
    assert TOKEN not in dump and ADMIN_PASSWORD not in dump and NODE_KEY not in dump


async def test_marzban_is_registered_with_the_administrator_login(
    session_factory: SessionFactory, tmp_path: Path
) -> None:
    result, box = await _register(
        session_factory, FakePanel(PanelKind.MARZBAN), install_for(PanelKind.MARZBAN, tmp_path)
    )
    async with session_factory() as session:
        row = await session.scalar(select(Panel))
    assert row is not None and row.username == "admin1"
    assert row.password and box.decrypt(row.password) == ADMIN_PASSWORD
    assert json.loads(row.options or "{}")["inbound_tags"] == [panel_setup.INBOUND_TAG]
    assert ADMIN_PASSWORD not in json.dumps(result.as_dict())


async def test_a_failing_remnawave_setup_is_an_error(session_factory: SessionFactory, tmp_path: Path) -> None:
    class Down:
        async def request(self, *args: Any, **kwargs: Any) -> tuple[int, Any]:
            return 0, None

        async def close(self) -> None:
            return None

    box = SecretBox("test-secret-key-0123456789abcdef")
    install = install_for(PanelKind.REMNAWAVE, tmp_path)
    with pytest.raises(SetupError) as info:
        await bootstrap_local(
            session_factory,
            OneRegistry(install.kind, session_factory),
            box,
            LocalSetup(panel=install),
            sleep=_no_sleep,
            http_factory=lambda _i: Down(),
        )
    assert ADMIN_PASSWORD not in str(info.value)


async def test_pasarguard_entry_point_failures_are_warnings(session_factory: SessionFactory, tmp_path: Path) -> None:
    fake = FakePanel(PanelKind.PASARGUARD)
    install = install_for(PanelKind.PASARGUARD, tmp_path, node_cert=None, existing_token=TOKEN)
    result, _ = await _register(session_factory, fake, install)
    assert result.panel["entry_point"] == "manual"
    assert any("not created automatically" in w for w in result.warnings)
    assert result.panel["panel"] == "created"


def test_cli_builds_the_panel_setup(tmp_path: Path) -> None:
    password = tmp_path / "pw"
    password.write_text(ADMIN_PASSWORD + "\n", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "bootstrap-local",
            "--panel-kind", "marzban",
            "--panel-url", "http://marzban:8000",
            "--panel-user", "admin1",
            "--panel-pass-file", str(password),
            "--reality-port", "8445",
            "--reality-sni", "vpn.example.com",
            "--reality-dest", "caddy:8444",
            "--endpoint-address", "203.0.113.5",
        ]
    )  # fmt: skip
    setup = build_local_setup(args)
    assert setup.panel is not None and setup.panel.kind == PanelKind.MARZBAN
    assert setup.panel.admin_password == ADMIN_PASSWORD and setup.xui is None


def test_cli_requires_the_matching_options(tmp_path: Path) -> None:
    args = build_parser().parse_args(["bootstrap-local", "--panel-kind", "remnawave"])
    with pytest.raises(SystemExit) as info:
        build_local_setup(args)
    assert "--panel-url" in str(info.value)


async def test_command_output_has_no_secrets(
    session_factory: SessionFactory, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePanel(PanelKind.REMNAWAVE)
    install = install_for(PanelKind.REMNAWAVE, tmp_path)
    monkeypatch.setattr("app.services.local_setup._default_http", lambda _i: fake)
    # the command builds its own services; exercise the same code path with the fakes injected
    result, _ = await _register(session_factory, fake, install)
    assert cmd_bootstrap_local is not None
    printed = json.dumps(result.as_dict())
    assert TOKEN not in printed and NODE_KEY not in printed and ADMIN_PASSWORD not in printed
    assert capsys.readouterr().out == ""
