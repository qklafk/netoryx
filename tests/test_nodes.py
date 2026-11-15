"""Adding another VPN server with a one-time join code."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.__main__ import build_parser
from app.db.models import Endpoint, Inbound, Location, Panel, PanelKind
from app.db.session import SessionFactory
from app.services import nodes
from app.services.local_setup import LocalSetup, bootstrap_local
from app.utils.crypto import SecretBox
from app.utils.time import utcnow
from tests.test_install_script import BASH, ROOT
from tests.test_local_setup import LocalPanel, xui_setup

BOX = SecretBox("test-secret-key-0123456789abcdef")
JOIN = nodes.NodeJoin(
    panel_url="https://198-51-100-7.sslip.io:2096/abcdef",
    username="anode1",
    password="node-panel-password-1",
    address="198.51.100.7",
    reality_sni="www.samsung.com",
    location_name="DE",
    location_flag="🇩🇪",
)


@dataclass
class ByName:
    """Panels by record name, so the main server and the node get different fakes."""

    panels: dict[str, LocalPanel] = field(default_factory=dict)
    session_factory: SessionFactory | None = None

    async def get(self, panel_id: int) -> Any:
        assert self.session_factory is not None
        async with self.session_factory() as session:
            panel = await session.get(Panel, panel_id)
        assert panel is not None
        return self.panels.setdefault(panel.name, LocalPanel())


async def test_a_code_adds_the_server_next_to_the_main_one(session_factory: SessionFactory) -> None:
    registry = ByName(session_factory=session_factory)
    await bootstrap_local(session_factory, registry, BOX, LocalSetup(xui=xui_setup(socks_port=None)))
    token = await nodes.issue_token(session_factory)
    result = await nodes.join(session_factory, registry, BOX, token, JOIN)
    assert result.xui["reality"]["inbound"] == "created" and result.xui["reality"]["endpoint"] == "created"
    async with session_factory() as s:
        panels = {p.name: p for p in await s.scalars(select(Panel))}
        node = panels["3x-ui DE 198.51.100.7"]
        endpoint = await s.scalar(
            select(Endpoint).join(Inbound, Inbound.id == Endpoint.inbound_id).where(Inbound.panel_id == node.id)
        )
        location = await s.scalar(select(Location).where(Location.name == "DE"))
    assert set(panels) == {"3x-ui", "3x-ui DE 198.51.100.7"}  # the main panel is untouched
    assert node.kind == PanelKind.XUI and node.tls_verify and node.url == JOIN.panel_url
    assert node.password and BOX.decrypt(node.password) == JOIN.password
    assert endpoint is not None and (endpoint.address, endpoint.port) == ("198.51.100.7", 443)
    assert location is not None and location.flag == "🇩🇪"
    created = registry.panels["3x-ui DE 198.51.100.7"].calls
    assert ("create_reality_inbound", 443, "www.samsung.com") in created


async def test_a_code_works_once(session_factory: SessionFactory) -> None:
    registry = ByName(session_factory=session_factory)
    token = await nodes.issue_token(session_factory)
    await nodes.join(session_factory, registry, BOX, token, JOIN)
    with pytest.raises(nodes.JoinError, match="invalid, used or expired"):
        await nodes.join(session_factory, registry, BOX, token, replace(JOIN, address="198.51.100.8"))


async def test_an_expired_or_unknown_code_is_refused(session_factory: SessionFactory) -> None:
    registry = ByName(session_factory=session_factory)
    old = await nodes.issue_token(session_factory, now=utcnow() - nodes.TOKEN_TTL - timedelta(minutes=1))
    for token in (old, "x" * 32):
        with pytest.raises(nodes.JoinError):
            await nodes.join(session_factory, registry, BOX, token, JOIN)
    async with session_factory() as s:
        assert (await s.scalars(select(Panel))).first() is None


@pytest.mark.parametrize(
    "change",
    [
        {"panel_url": "http://198-51-100-7.sslip.io:2096/abc"},  # never send the panel password in clear text
        {"panel_url": "https://user:pw@example.com/abc"},
        {"address": "bad address"},
        {"reality_sni": "-x-"},
        {"location_name": " "},
    ],
)
async def test_malformed_requests_do_not_spend_the_code(
    session_factory: SessionFactory, change: dict[str, str]
) -> None:
    registry = ByName(session_factory=session_factory)
    token = await nodes.issue_token(session_factory)
    with pytest.raises(nodes.JoinError):
        await nodes.join(session_factory, registry, BOX, token, replace(JOIN, **change))
    await nodes.join(session_factory, registry, BOX, token, JOIN)  # still valid


def test_node_token_command_exists() -> None:
    assert build_parser().parse_args(["node-token"]).command == "node-token"


# --- the installer on the new server ----------------------------------------------------------------------------------
def _node_dry_run(*args: str) -> Any:
    import subprocess

    assert BASH is not None
    script = f"bash install.sh --dry-run --lang en {' '.join(args)}"
    return subprocess.run(
        [BASH, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env={
            "PATH": "/usr/bin:/bin",
            "NETORYX_OS_RELEASE": str(ROOT / "tests/fixtures/os-release/ubuntu-24.04"),
            "NETORYX_PUBLIC_IP": "203.0.113.10",
            "NETORYX_LOG": "/dev/null",
        },
    )


@pytest.mark.skipif(BASH is None, reason="a POSIX bash is needed to exercise install.sh")
def test_node_dry_run_plans_the_panel_firewall_and_join() -> None:
    done = _node_dry_run("--node", "--join", "vpn.example.com/" + "a" * 32)
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "allow from 198.51.100.1 to any port 2096" in out  # the panel is open to the main server only
    assert "https://vpn.example.com/api/node/join" in out
    assert "--bot-token" not in out and "app bootstrap-local" not in out  # no bot on a node


@pytest.mark.skipif(BASH is None, reason="a POSIX bash is needed to exercise install.sh")
@pytest.mark.parametrize("join", ["vpn.example.com", "not a domain/" + "a" * 32, "vpn.example.com/short"])
def test_node_needs_a_valid_join_string(join: str) -> None:
    done = _node_dry_run("--node", "--join", f"'{join}'")
    assert done.returncode != 0 and "netoryx node-token" in done.stderr


def test_node_files_pin_the_panel_and_keep_443_for_the_vpn(tmp_path: Path) -> None:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    block = text.split("node_write_files() {", 1)[1].split("node_firewall() {", 1)[0]
    assert "XUI_IMAGE:-$XUI_IMAGE" in block and "disable_tlsalpn_challenge" in block
    assert 'readonly XUI_IMAGE="ghcr.io/mhsanaei/3x-ui:v3.11.0"' in text


# --- the join endpoint ------------------------------------------------------------------------------------------------
async def test_join_endpoint_refuses_a_wrong_code_and_limits_attempts(tmp_path: Path) -> None:
    import httpx

    from app.core.modules import ModuleRegistry
    from app.modules import admin_api, admin_servers
    from app.web.app import create_web_app
    from tests.test_mod_webapi_support import build_ctx

    ctx, engine = await build_ctx(tmp_path)
    ctx.modules = ModuleRegistry([admin_api.module, admin_servers.module])
    body = {
        "token": "w" * 32,
        "panel_url": JOIN.panel_url,
        "username": JOIN.username,
        "password": JOIN.password,
        "address": JOIN.address,
        "reality_sni": JOIN.reality_sni,
        "location_name": "DE",
        "country": "DE",
    }
    try:
        app = create_web_app(ctx)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            codes = [(await client.post("/api/node/join", json=body)).status_code for _ in range(11)]
            refused = await client.post("/api/node/join", json={**body, "token": "short"})
    finally:
        await engine.dispose()
    assert codes[:10] == [400] * 10 and codes[10] == 429
    assert refused.status_code in (422, 429)
