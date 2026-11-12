"""Telegram proxy clients on a real 3x-ui ``mtproto`` inbound (v3.5+).

Skipped unless ``TGPROXY_INTEGRATION=1``. Requires Docker and pulls the 3x-ui image (~250 MB) on first run.

    TGPROXY_INTEGRATION=1 .venv/Scripts/python -m pytest -q tests/integration/tgproxy

Exercises only the panel API (client CRUD, secrets read back, inventory import); whether the panel's mtg sidecar
actually serves the inbound is out of scope.

Optional environment:
* ``TGPROXY_IT_XUI_IMAGE`` image override (default ``ghcr.io/mhsanaei/3x-ui:latest``)
* ``TGPROXY_IT_KEEP=1`` leave the container running (for debugging)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import Inbound, Panel, PanelKind
from app.db.session import SessionFactory
from app.services.panels.inventory import refresh_inbounds, tg_proxy_enabled
from app.services.panels.mtproto import ProxyUserSpec, fake_tls_secret, new_key
from app.services.panels.xui import XuiBackend

pytestmark = pytest.mark.skipif(
    os.environ.get("TGPROXY_INTEGRATION") != "1", reason="set TGPROXY_INTEGRATION=1 to run (needs Docker)"
)

IMAGE = os.environ.get("TGPROXY_IT_XUI_IMAGE", "ghcr.io/mhsanaei/3x-ui:latest")
STARTUP_TIMEOUT_SEC = 120
CREDENTIALS_RE = re.compile(r"Username:\s*(\S+)\s+Password:\s*(\S+)")
DOMAIN = "petrovich.ru"
TAG = "0123456789abcdef0123456789abcdef"


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=600).stdout


def _wait_http(url: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(1)
    raise TimeoutError(f"3x-ui at {url} did not come up in {STARTUP_TIMEOUT_SEC}s")


def _credentials(container: str) -> tuple[str, str]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True, check=False)
        if match := CREDENTIALS_RE.search(logs.stdout + logs.stderr):
            return match.group(1), match.group(2)
        time.sleep(1)
    raise RuntimeError("3x-ui did not print its generated credentials")


@dataclass
class LiveXui:
    url: str
    username: str
    password: str
    inbound_id: int = 0
    vless_inbound_id: int = 0

    def backend(self) -> XuiBackend:
        return XuiBackend(self.url, username=self.username, password=self.password, timeout=20)


async def _api(backend: XuiBackend, method: str, path: str, body: Any = None) -> Any:
    envelope = await backend._t.request(method, path, json_body=body)
    assert envelope is not None and envelope.get("success"), envelope
    return envelope.get("obj")


def _inbound(protocol: str, port: int, settings: dict[str, Any]) -> dict[str, Any]:
    stream = {"network": "tcp", "security": "none", "tcpSettings": {"header": {"type": "none"}}}
    return {
        "up": 0,
        "down": 0,
        "total": 0,
        "remark": f"it-{protocol}",
        "enable": True,
        "expiryTime": 0,
        "listen": "",
        "port": port,
        "protocol": protocol,
        "settings": json.dumps(settings),
        "streamSettings": json.dumps(stream) if protocol != "mtproto" else "",
        "sniffing": json.dumps({"enabled": False, "destOverride": []}),
    }


async def _setup(panel: LiveXui) -> None:
    backend = panel.backend()
    try:
        created = await _api(
            backend,
            "POST",
            "/panel/api/inbounds/add",
            _inbound("mtproto", 40443, {"fakeTlsDomain": DOMAIN, "clients": []}),
        )
        panel.inbound_id = int(created["id"])
        vless = {"clients": [], "decryption": "none", "encryption": "none", "fallbacks": []}
        created = await _api(backend, "POST", "/panel/api/inbounds/add", _inbound("vless", 40444, vless))
        panel.vless_inbound_id = int(created["id"])
    finally:
        await backend.close()


@pytest.fixture(scope="module")
def live_xui() -> Iterator[LiveXui]:
    container = f"vpnbot-it-xui-mtproto-{secrets.token_hex(4)}"
    _docker("run", "-d", "--name", container, "-p", "127.0.0.1:0:2053", IMAGE)
    try:
        host_port = _docker("port", container, "2053/tcp").splitlines()[0].strip().rsplit(":", 1)[-1]
        url = f"http://127.0.0.1:{host_port}"
        _wait_http(url + "/")
        username, password = _credentials(container)
        panel = LiveXui(url, username, password)
        asyncio.run(_setup(panel))
        yield panel
    finally:
        if os.environ.get("TGPROXY_IT_KEEP") != "1":
            subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True, check=False)


@pytest.fixture
async def backend(live_xui: LiveXui) -> AsyncIterator[XuiBackend]:
    instance = live_xui.backend()
    yield instance
    await instance.close()


async def test_mtproto_client_lifecycle(live_xui: LiveXui, backend: XuiBackend) -> None:
    target = live_xui.inbound_id
    name = f"tg{secrets.randbelow(10**6)}-{target}-tg"
    key = new_key()
    expires = (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0)
    spec = ProxyUserSpec(
        name=name, enabled=True, key=key, expires_at=expires, max_unique_ips=2, ad_tag=TAG, tls_domain=DOMAIN, tg_id=42
    )
    created = await backend.upsert_proxy_user(target, spec)
    assert created.key == key and created.tls_secret == fake_tls_secret(key, DOMAIN)
    assert created.enabled and created.expires_at == expires and created.max_unique_ips == 2 and created.ad_tag == TAG

    again = await backend.upsert_proxy_user(target, spec)
    assert again.tls_secret == created.tls_secret

    disabled = await backend.upsert_proxy_user(
        target, ProxyUserSpec(name=name, enabled=False, key=key, tls_domain=DOMAIN)
    )
    assert not disabled.enabled and disabled.key == key and disabled.ad_tag is None

    key2 = new_key()
    rotated = await backend.upsert_proxy_user(
        target, ProxyUserSpec(name=name, enabled=True, key=key2, tls_domain=DOMAIN)
    )
    assert rotated.key == key2 and rotated.enabled

    kept = await backend.upsert_proxy_user(target, ProxyUserSpec(name=name, enabled=True, tls_domain=DOMAIN))
    assert kept.key == key2  # no key in the spec: the panel keeps the current secret

    listed = await backend.list_proxy_users(target)
    assert name in [a.name for a in listed]
    assert await backend.list_proxy_users(live_xui.vless_inbound_id) == []
    await backend.delete_proxy_user(target, name)
    await backend.delete_proxy_user(target, name)
    assert await backend.get_proxy_user(target, name) is None


async def test_panel_generates_secret_when_none_given(live_xui: LiveXui, backend: XuiBackend) -> None:
    name = f"tg{secrets.randbelow(10**6)}-{live_xui.inbound_id}-tg"
    account = await backend.upsert_proxy_user(live_xui.inbound_id, ProxyUserSpec(name=name, enabled=True))
    assert account.key is not None and account.tls_secret == fake_tls_secret(account.key, DOMAIN)
    await backend.delete_proxy_user(live_xui.inbound_id, name)


async def test_inventory_imports_mtproto_inbound(live_xui: LiveXui, session_factory: SessionFactory) -> None:
    async with session_factory.begin() as session:
        panel = Panel(name="xui", kind=PanelKind.XUI, url=live_xui.url, username=live_xui.username)
        panel.password = live_xui.password
        session.add(panel)
        await session.flush()
        panel_id = panel.id

    class Registry:
        def __init__(self) -> None:
            self.backend = live_xui.backend()

        async def get(self, _: int) -> XuiBackend:
            return self.backend

    registry = Registry()
    try:
        result = await refresh_inbounds(session_factory, registry, panel_id, activate_new=True)
    finally:
        await registry.backend.close()
    async with session_factory() as session:
        rows = {r.remote_id: r for r in await session.scalars(select(Inbound).where(Inbound.panel_id == panel_id))}
    mt = rows[live_xui.inbound_id]
    assert result.mtproto == [mt.id] and not mt.is_active and tg_proxy_enabled(mt)
    params = json.loads(mt.params or "")
    assert (params["protocol"], params["fake_tls_domain"], params["port"], params["local"]) == (
        "mtproto",
        DOMAIN,
        40443,
        True,
    )
