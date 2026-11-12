"""XuiBackend against real 3x-ui panels running in Docker (both client-API generations).

Skipped unless ``XUI_INTEGRATION=1``. Requires Docker; pulls ~250 MB per image on first run.

    XUI_INTEGRATION=1 .venv/Scripts/python -m pytest -q tests/integration/xui

For each image (default: ``ghcr.io/mhsanaei/3x-ui:latest`` - the modern v3.1+ API with CSRF and Bearer
tokens - and ``ghcr.io/mhsanaei/3x-ui:v2.9.4`` - the legacy API) the module fixture starts a container on
a random localhost port, reads the generated admin credentials from the log (v3; v2 uses admin/admin),
creates VLESS inbounds (TCP/XHTTP/gRPC + REALITY with locally generated x25519 keys, WS without TLS) and
removes the container afterwards.

Optional environment:
* ``XUI_IT_IMAGES``  comma-separated image list overriding the default two
* ``XUI_IT_KEEP=1``  leave the containers running (for debugging)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app.services.panels.base import AccountSpec, PanelAuthError, PanelUnsupportedError
from app.services.panels.xui import ApiGeneration, XuiApiError, XuiBackend

pytestmark = pytest.mark.skipif(
    os.environ.get("XUI_INTEGRATION") != "1", reason="set XUI_INTEGRATION=1 to run (needs Docker)"
)

DEFAULT_IMAGES = ("ghcr.io/mhsanaei/3x-ui:latest", "ghcr.io/mhsanaei/3x-ui:v2.9.4")
IMAGES = [i.strip() for i in os.environ.get("XUI_IT_IMAGES", ",".join(DEFAULT_IMAGES)).split(",") if i.strip()]
STARTUP_TIMEOUT_SEC = 120
CREDENTIALS_RE = re.compile(r"Username:\s*(\S+)\s+Password:\s*(\S+)")
SHORT_ID = "6ba85179e30d4fc2"
SNI = "www.google.com"


# ---------------------------------------------------------------------------------------------
# Container + inbound setup
# ---------------------------------------------------------------------------------------------
def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=600).stdout


def x25519_keypair() -> tuple[str, str]:
    """REALITY keys as xray expects them: raw 32-byte keys, base64url without padding."""
    key = X25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return b64(private), b64(public)


def reality_stream(network: str, private_key: str, public_key: str, **transport: Any) -> dict[str, Any]:
    return {
        "network": network,
        "security": "reality",
        "externalProxy": [],
        "realitySettings": {
            "show": False,
            "xver": 0,
            "target": f"{SNI}:443",
            "serverNames": [SNI],
            "privateKey": private_key,
            "minClientVer": "",
            "maxClientVer": "",
            "maxTimediff": 0,
            "shortIds": [SHORT_ID],
            "mldsa65Seed": "",
            "settings": {
                "publicKey": public_key,
                "fingerprint": "chrome",
                "serverName": "",
                "spiderX": "/",
                "mldsa65Verify": "",
            },
        },
        **transport,
    }


@dataclass
class LivePanel:
    image: str
    url: str
    username: str
    password: str
    public_key: str = ""
    inbounds: dict[str, int] = field(default_factory=dict)  # role -> remote inbound id

    @property
    def modern(self) -> bool:
        """v3.1+ images (``latest`` or ``v3.x``); ``v2.x`` / ``v3.0.x`` tags speak the legacy client API."""
        tag = self.image.rsplit(":", 1)[-1].removeprefix("v")
        return not (tag.startswith("2.") or tag.startswith("3.0."))

    def backend(self, **overrides: Any) -> XuiBackend:
        kwargs: dict[str, Any] = {"username": self.username, "password": self.password, "timeout": 20}
        kwargs.update(overrides)
        return XuiBackend(self.url, **kwargs)


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


def _credentials(container: str, image: str) -> tuple[str, str]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True, check=False)
        if match := CREDENTIALS_RE.search(logs.stdout + logs.stderr):
            return match.group(1), match.group(2)
        time.sleep(1)
    return "admin", "admin"  # v2.x panels ship with default credentials


INBOUND_SPECS: dict[str, tuple[int, str]] = {
    # role: (port, network)
    "tcp": (40001, "tcp"),
    "xhttp": (40002, "xhttp"),
    "grpc": (40003, "grpc"),
    "ws": (40004, "ws"),
    "lifecycle": (40005, "tcp"),
}


def _inbound_body(role: str, private_key: str, public_key: str) -> dict[str, Any]:
    port, network = INBOUND_SPECS[role]
    if network == "tcp":
        tcp = {"tcpSettings": {"acceptProxyProtocol": False, "header": {"type": "none"}}}
        stream = reality_stream("tcp", private_key, public_key, **tcp)
    elif network == "xhttp":
        xhttp = {
            "path": "/api/v1",
            "host": "",
            "mode": "auto",
            "xPaddingBytes": "100-1000",
            "scMaxEachPostBytes": "1000000",  # xray default: 3x-ui drops it from `extra`
            "headers": {},
            "noSSEHeader": False,
        }
        stream = reality_stream("xhttp", private_key, public_key, xhttpSettings=xhttp)
    elif network == "grpc":
        grpc = {"serviceName": "grpc-svc", "authority": "", "multiMode": True}
        stream = reality_stream("grpc", private_key, public_key, grpcSettings=grpc)
    else:
        ws = {"acceptProxyProtocol": False, "path": "/ws", "host": "cdn.example.ru", "headers": {}}
        stream = {"network": "ws", "security": "none", "externalProxy": [], "wsSettings": ws}
    settings = {"clients": [], "decryption": "none", "encryption": "none", "fallbacks": []}
    sniffing = {"enabled": True, "destOverride": ["http", "tls", "quic"], "metadataOnly": False, "routeOnly": False}
    return {
        "up": 0,
        "down": 0,
        "total": 0,
        "remark": f"it-{role}",
        "enable": True,
        "expiryTime": 0,
        "listen": "",
        "port": port,
        "protocol": "vless",
        # JSON strings: the only form both API generations accept.
        "settings": json.dumps(settings),
        "streamSettings": json.dumps(stream),
        "sniffing": json.dumps(sniffing),
    }


async def _api(backend: XuiBackend, method: str, path: str, body: Any = None) -> Any:
    """Raw authenticated API call through the backend's transport (handles login/CSRF)."""
    envelope = await backend._t.request(method, path, json_body=body)
    assert envelope is not None and envelope.get("success"), envelope
    return envelope.get("obj")


async def _create_inbounds(panel: LivePanel) -> None:
    private_key, panel.public_key = x25519_keypair()
    backend = panel.backend()
    try:
        for role in INBOUND_SPECS:
            created = await _api(
                backend, "POST", "/panel/api/inbounds/add", _inbound_body(role, private_key, panel.public_key)
            )
            panel.inbounds[role] = int(created["id"])
    finally:
        await backend.close()


@pytest.fixture(scope="module", params=IMAGES, ids=lambda image: image.rsplit(":", 1)[-1])
def live_panel(request: pytest.FixtureRequest) -> Iterator[LivePanel]:
    image: str = request.param
    container = f"vpnbot-xui-it-{re.sub(r'[^a-z0-9]+', '-', image.rsplit(':', 1)[-1].lower())}-{os.getpid()}"
    _docker("run", "-d", "--name", container, "-p", "127.0.0.1:0:2053", image)
    try:
        host_port = _docker("port", container, "2053/tcp").splitlines()[0].strip().rsplit(":", 1)[-1]
        url = f"http://127.0.0.1:{host_port}"
        _wait_http(url + "/")
        username, password = _credentials(container, image)
        panel = LivePanel(image=image, url=url, username=username, password=password)
        asyncio.run(_create_inbounds(panel))
        yield panel
    finally:
        if os.environ.get("XUI_IT_KEEP") != "1":
            subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True, check=False)


@pytest.fixture
async def backend(live_panel: LivePanel) -> Any:
    backend = live_panel.backend()
    yield backend
    await backend.close()


def account_spec(email: str, **overrides: Any) -> AccountSpec:
    values: dict[str, Any] = {
        "email": email,
        "uuid": "11111111-2222-4333-8444-555555555555",
        "enabled": True,
        "expires_at": datetime.now(UTC).replace(microsecond=0) + timedelta(days=30),
        "device_limit": 2,
        "traffic_limit_bytes": 10 * 1024**3,
        "tg_id": 123456789,
        "sub_id": hashlib.sha256(email.encode()).hexdigest()[:16],
        "comment": "vpn-bot integration",
        "flow": "xtls-rprx-vision",
    }
    values.update(overrides)
    return AccountSpec(**values)


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------
async def test_ping_detects_version_and_generation(live_panel: LivePanel, backend: XuiBackend) -> None:
    version = await backend.ping()
    print(f"\n{live_panel.image}: panel version {version}, {backend.generation} client API")
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    expected = ApiGeneration.MODERN if live_panel.modern else ApiGeneration.LEGACY
    assert backend.generation == expected
    assert int(version.split(".")[0]) == (3 if live_panel.modern else 2)


async def test_list_inbounds_normalizes_link_params(live_panel: LivePanel, backend: XuiBackend) -> None:
    infos = {i.remote_id: i for i in await backend.list_inbounds()}
    reality = {
        "security": "reality",
        "sni": SNI,
        "pbk": live_panel.public_key,
        "sid": SHORT_ID,
        "fp": "chrome",
        "spx": "/",
    }

    tcp = infos[live_panel.inbounds["tcp"]]
    assert (tcp.protocol, tcp.port, tcp.enabled, tcp.remark) == ("vless", 40001, True, "it-tcp")
    assert tcp.params == {"type": "tcp", "encryption": "none", **reality, "flow_allowed": True}

    xhttp = infos[live_panel.inbounds["xhttp"]].params
    assert xhttp == {
        "type": "xhttp",
        "encryption": "none",
        "path": "/api/v1",
        "host": "",
        "mode": "auto",
        "x_padding_bytes": "100-1000",
        "extra": {"mode": "auto", "xPaddingBytes": "100-1000"},
        **reality,
        "flow_allowed": False,
    }

    grpc = infos[live_panel.inbounds["grpc"]].params
    assert grpc == {
        "type": "grpc",
        "encryption": "none",
        "serviceName": "grpc-svc",
        "authority": "",
        "mode": "multi",
        **reality,
        "flow_allowed": False,
    }

    ws = infos[live_panel.inbounds["ws"]].params
    assert ws == {
        "type": "ws",
        "encryption": "none",
        "path": "/ws",
        "host": "cdn.example.ru",
        "security": "none",
        "flow_allowed": False,
    }


def _query(link: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(link)
    return f"{parts.hostname}:{parts.port}", dict(parse_qsl(parts.query, keep_blank_values=True))


def _derive_spider_x(seed: str, sub_id: str) -> str:
    """3x-ui v3.11 ``deriveSpiderX``: a stable per-client path derived from the seed and the subId."""
    path = "/" + hashlib.sha256(f"{seed}|{sub_id}".encode()).hexdigest()[:15]
    _, _, query = seed.partition("?")
    return f"{path}?{query}" if query else path


@pytest.mark.parametrize("role", ["tcp", "xhttp", "grpc", "ws"])
async def test_locally_built_links_match_panel_links(live_panel: LivePanel, backend: XuiBackend, role: str) -> None:
    if not live_panel.modern:
        pytest.skip("/panel/api/clients/links exists only on 3x-ui v3.1+")
    links = pytest.importorskip("app.services.links")
    inbound_id = live_panel.inbounds[role]
    info = next(i for i in await backend.list_inbounds() if i.remote_id == inbound_id)
    flow = "xtls-rprx-vision" if info.params["flow_allowed"] else ""
    spec = account_spec(f"it-link-{role}", uuid="33333333-2222-4333-8444-555555555555", flow=flow)
    await backend.upsert_account(inbound_id, spec)
    try:
        (panel_link,) = await _api(backend, "GET", f"/panel/api/clients/links/{spec.email}")
        address, panel_query = _query(panel_link)
        host, port = address.rsplit(":", 1)
        ours = links.build_vless(spec.uuid, host, int(port), {**info.params, "port": info.port}, "x", None, flow)
        _, our_query = _query(ours)
        if "spx" in panel_query:  # per-client SpiderX on v3.11; we cache the seed
            assert panel_query.pop("spx") == _derive_spider_x(our_query.pop("spx"), spec.sub_id)
        assert our_query == panel_query
        assert unquote(urlsplit(panel_link).netloc.split("@")[0]) == spec.uuid
    finally:
        await backend.delete_account(inbound_id, spec.email, spec.uuid)


async def test_account_lifecycle(live_panel: LivePanel, backend: XuiBackend) -> None:
    inbound_id = live_panel.inbounds["lifecycle"]
    first = account_spec("tg101-it")
    second = account_spec("tg102-it", uuid="22222222-2222-4333-8444-555555555555", tg_id=0, flow="")

    created = await backend.upsert_account(inbound_id, first)
    assert (created.email, created.uuid, created.enabled) == (first.email, first.uuid, True)
    await backend.upsert_account(inbound_id, first)  # idempotent
    await backend.upsert_account(inbound_id, second)

    got = await backend.get_account(inbound_id, first.email)
    assert got is not None
    assert (got.uuid, got.enabled, got.expires_at) == (first.uuid, True, first.expires_at)
    assert (got.device_limit, got.traffic_limit_bytes, got.used_bytes) == (2, 10 * 1024**3, 0)
    assert int(got.raw["tgId"]) == 123456789
    assert got.raw["subId"] == first.sub_id
    assert got.raw["flow"] == "xtls-rprx-vision"

    listed = {a.email: a for a in await backend.list_accounts(inbound_id)}
    assert set(listed) == {first.email, second.email}
    assert listed[second.email].uuid == second.uuid

    # disable / enable
    await backend.upsert_account(inbound_id, replace(first, enabled=False))
    assert (await backend.get_account(inbound_id, first.email)).enabled is False  # type: ignore[union-attr]
    await backend.upsert_account(inbound_id, first)
    assert (await backend.get_account(inbound_id, first.email)).enabled is True  # type: ignore[union-attr]

    # renew: new expiry and limits; nothing else may be wiped by the full-object update
    assert first.expires_at is not None
    renewed = replace(first, expires_at=first.expires_at + timedelta(days=60), device_limit=5)
    await backend.upsert_account(inbound_id, renewed)
    got = await backend.get_account(inbound_id, first.email)
    assert got is not None
    assert (got.expires_at, got.device_limit) == (renewed.expires_at, 5)
    assert (got.raw["flow"], got.raw["subId"], got.raw["comment"]) == (
        "xtls-rprx-vision",
        first.sub_id,
        "vpn-bot integration",
    )

    # never expires, unlimited traffic
    await backend.upsert_account(inbound_id, replace(renewed, expires_at=None, traffic_limit_bytes=0))
    got = await backend.get_account(inbound_id, first.email)
    assert got is not None and (got.expires_at, got.traffic_limit_bytes) == (None, 0)

    # uuid rotation keeps the email
    rotated = replace(renewed, uuid="44444444-2222-4333-8444-555555555555")
    await backend.upsert_account(inbound_id, rotated)
    got = await backend.get_account(inbound_id, first.email)
    assert got is not None and got.uuid == rotated.uuid

    await backend.reset_devices(got)

    # delete a non-last client, then the last one (legacy panels refuse that via delClient)
    await backend.delete_account(inbound_id, second.email, second.uuid)
    assert await backend.get_account(inbound_id, second.email) is None
    await backend.delete_account(inbound_id, first.email, rotated.uuid)
    assert await backend.get_account(inbound_id, first.email) is None
    assert await backend.list_accounts(inbound_id) == []
    await backend.delete_account(inbound_id, first.email, rotated.uuid)  # missing -> success


async def test_server_status_devices_reality_inbound_and_restart(live_panel: LivePanel, backend: XuiBackend) -> None:
    status = await backend.server_status()
    assert status.cpu_cores and status.cpu_cores > 0
    assert status.mem_total and status.mem_total > 0
    assert status.disk_total and status.disk_total > 0
    assert status.core_state is not None and status.core_version

    # A fresh client has no recorded IPs; the route exists on both API generations.
    inbound_id = live_panel.inbounds["lifecycle"]
    spec = account_spec("tg103-it", uuid="33333333-2222-4333-8444-555555555555")
    account = await backend.upsert_account(inbound_id, spec)
    try:
        assert await backend.list_devices(account) == []
    finally:
        await backend.delete_account(inbound_id, spec.email, spec.uuid)

    # A Reality inbound created from the bot gets usable link parameters; a taken port is refused.
    created = await backend.create_reality_inbound(40010, SNI, remark="it-created")
    assert created.remote_id > 0 and created.port == 40010
    assert created.params["security"] == "reality"
    assert created.params["pbk"] and created.params["sid"] and created.params["sni"] == SNI
    assert created.params.get("flow") == "xtls-rprx-vision"
    assert any(i.remote_id == created.remote_id for i in await backend.list_inbounds())
    with pytest.raises(XuiApiError):
        await backend.create_reality_inbound(40010, SNI, remark="it-duplicate")
    await _api(backend, "POST", f"/panel/api/inbounds/del/{created.remote_id}")

    await backend.restart_core()
    assert await backend.ping()  # the panel keeps answering while Xray restarts


async def test_unknown_inbound_is_an_api_error(backend: XuiBackend) -> None:
    with pytest.raises(XuiApiError, match="not found"):
        await backend.upsert_account(999, account_spec("tg999-it"))


async def test_expired_session_is_renewed(backend: XuiBackend) -> None:
    await backend.ping()
    backend._t._session().cookie_jar.clear()  # what a 6-hour session expiry looks like to the client
    assert await backend.list_inbounds()


async def test_wrong_password_is_rejected(live_panel: LivePanel) -> None:
    backend = live_panel.backend(password="definitely-wrong")
    try:
        with pytest.raises(PanelAuthError):
            await backend.ping()
    finally:
        await backend.close()


async def test_api_tokens(live_panel: LivePanel, backend: XuiBackend) -> None:
    if not live_panel.modern:
        token_only = live_panel.backend(username=None, password=None, api_token="anything")
        with_fallback = live_panel.backend(api_token="anything")
        try:
            with pytest.raises(PanelUnsupportedError):
                await token_only.ping()
            assert await with_fallback.ping()  # falls back to the session login
        finally:
            await token_only.close()
            await with_fallback.close()
        return

    created = await _api(
        backend, "POST", "/panel/api/setting/apiTokens/create", {"name": "vpn-bot-it", "scope": "admin"}
    )
    token_backend = live_panel.backend(username=None, password=None, api_token=created["token"])
    bad = live_panel.backend(username=None, password=None, api_token="not-a-token")
    inbound_id = live_panel.inbounds["lifecycle"]
    spec = account_spec("tg201-it")
    try:
        assert await token_backend.ping() == await backend.ping()
        await token_backend.upsert_account(inbound_id, spec)
        got = await token_backend.get_account(inbound_id, spec.email)
        assert got is not None and got.uuid == spec.uuid
        await token_backend.delete_account(inbound_id, spec.email, spec.uuid)
        assert await token_backend.get_account(inbound_id, spec.email) is None
        with pytest.raises(PanelAuthError):
            await bad.ping()
    finally:
        await token_backend.close()
        await bad.close()
