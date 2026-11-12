"""RemnawaveBackend against a real Remnawave panel running in Docker.

Skipped unless ``REMNAWAVE_INTEGRATION=1``. Requires Docker (compose v2) and ~1 GB of images.

    REMNAWAVE_INTEGRATION=1 .venv/Scripts/python -m pytest -q tests/integration/remnawave

The module fixture starts ``docker-compose.yml`` next to this file (backend + Postgres + Valkey),
registers an admin, issues a scoped API token, adds a host to the default squad, runs a webhook
receiver on the host, and finally removes everything with ``docker compose down -v``.

Optional environment:
* ``REMNAWAVE_IMAGE_TAG``     backend image tag (default ``3.4.8``)
* ``REMNAWAVE_IT_PORT``       host port of the panel API (default ``13100``)
* ``REMNAWAVE_IT_HOOK_PORT``  host port of the webhook receiver (default ``18199``)
* ``REMNAWAVE_IT_KEEP=1``     leave the containers running after the tests (for debugging)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from app.services.panels.base import AccountSpec, PanelAuthError, PanelError
from app.services.panels.remnawave import SIGNATURE_HEADER, RemnawaveBackend, parse_version

pytestmark = pytest.mark.skipif(
    os.environ.get("REMNAWAVE_INTEGRATION") != "1", reason="set REMNAWAVE_INTEGRATION=1 to run (needs Docker)"
)

HERE = Path(__file__).parent
COMPOSE = ["docker", "compose", "-f", str(HERE / "docker-compose.yml"), "-p", "vpnbot-remnawave-it"]
PORT = int(os.environ.get("REMNAWAVE_IT_PORT", "13100"))
HOOK_PORT = int(os.environ.get("REMNAWAVE_IT_HOOK_PORT", "18199"))
BASE_URL = f"http://127.0.0.1:{PORT}"
ADMIN_PASSWORD = "IntegrationAdminPassw0rd2026"  # 24+ chars, upper + lower + digit
HOST_ADDRESS = "it-de1.example.com"
BOT_SCOPES = [
    "users:*",
    "subscriptions:read",
    "internal-squads:read",
    "system:read",
    "hwid-user-devices:*",
    "nodes:*",
]
STARTUP_TIMEOUT = 240.0


def _webhook_secret() -> str:
    for line in (HERE / "remnawave.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("WEBHOOK_SECRET_HEADER="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("WEBHOOK_SECRET_HEADER missing in remnawave.env")


# ---------------------------------------------------------------------- environment


@dataclass
class Hooks:
    """Deliveries received by the webhook receiver: (headers, raw body)."""

    items: list[tuple[dict[str, str], bytes]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> list[tuple[dict[str, str], bytes]]:
        with self.lock:
            return list(self.items)


@dataclass(frozen=True)
class LivePanel:
    url: str
    token: str
    admin_jwt: str
    squad_uuids: list[str]
    hooks: Hooks
    webhook_secret: str


def _http(method: str, path: str, *, token: str | None = None, body: Any = None) -> Any:
    """Minimal sync JSON client for bootstrapping (adds the headers a direct call needs)."""
    headers = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Proto": "https",
        "Content-Type": "application/json",
        "X-Remnawave-Client-Type": "browser",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE_URL + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as resp:
        payload = resp.read()
    return json.loads(payload)["response"] if payload else None


def _compose(*args: str, timeout: float = 300) -> None:
    subprocess.run([*COMPOSE, *args], check=True, capture_output=True, timeout=timeout)


def _wait_until_ready() -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            _http("GET", "/api/auth/status")
            return
        except (OSError, urllib.error.URLError, ValueError) as exc:
            last_error = exc
            time.sleep(2)
    logs = subprocess.run([*COMPOSE, "logs", "--tail", "80", "remnawave"], capture_output=True, text=True).stdout
    raise RuntimeError(f"Remnawave did not start in {STARTUP_TIMEOUT}s: {last_error}\n{logs}")


# The default config profile only has a Shadowsocks inbound; add a VLESS one in its own squad.
VLESS_PROFILE = {
    "log": {"loglevel": "warning"},
    "inbounds": [
        {
            "tag": "VLESS_TCP",
            "port": 443,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {"network": "tcp", "security": "none"},
        }
    ],
    "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
}


def _add_host(jwt: str, inbound: dict[str, Any], remark: str, port: int) -> None:
    inbound_ref = {"configProfileUuid": inbound["profileUuid"], "configProfileInboundUuid": inbound["uuid"]}
    body = {"inbound": inbound_ref, "remark": remark, "address": HOST_ADDRESS, "port": port}
    _http("POST", "/api/hosts", token=jwt, body=body)


def _bootstrap() -> tuple[str, str, list[str]]:
    """Register the admin, issue the bot token, add squads + hosts.

    Returns ``(admin_jwt, api_token, [vless_squad_uuid, default_squad_uuid])``.
    """
    jwt = _http("POST", "/api/auth/register", body={"username": "admin", "password": ADMIN_PASSWORD})["accessToken"]
    token = _http(
        "POST", "/api/tokens", token=jwt, body={"name": "vpnbot-it", "expiresInDays": 1, "scopes": BOT_SCOPES}
    )["token"]
    default_squad = _http("GET", "/api/internal-squads", token=jwt)["internalSquads"][0]
    _add_host(jwt, default_squad["inbounds"][0], "IT-SS", 8388)
    profile = _http("POST", "/api/config-profiles", token=jwt, body={"name": "IT-VLESS", "config": VLESS_PROFILE})
    vless_inbound = profile["inbounds"][0]
    vless_squad = _http(
        "POST", "/api/internal-squads", token=jwt, body={"name": "IT-VLESS", "inbounds": [vless_inbound["uuid"]]}
    )
    _add_host(jwt, vless_inbound, "IT-DE-1", 443)
    return jwt, token, [vless_squad["uuid"], default_squad["uuid"]]


def _start_hook_receiver(hooks: Hooks) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with hooks.lock:
                hooks.items.append((dict(self.headers), body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("0.0.0.0", HOOK_PORT), Handler)  # reachable from the container
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture(scope="module")
def live() -> Iterator[LivePanel]:
    hooks = Hooks()
    receiver = _start_hook_receiver(hooks)
    os.environ.setdefault("REMNAWAVE_IT_PORT", str(PORT))
    os.environ.setdefault("REMNAWAVE_IT_HOOK_PORT", str(HOOK_PORT))
    _compose("down", "-v", "--remove-orphans")  # leftovers of an interrupted run
    try:
        _compose("up", "-d")
        _wait_until_ready()
        jwt, token, squad_uuids = _bootstrap()
        yield LivePanel(BASE_URL, token, jwt, squad_uuids, hooks, _webhook_secret())
    finally:
        receiver.shutdown()
        if os.environ.get("REMNAWAVE_IT_KEEP") != "1":
            _compose("down", "-v", "--remove-orphans")


def _backend(live: LivePanel, **kwargs: Any) -> RemnawaveBackend:
    params: dict[str, Any] = {"squad_uuids": live.squad_uuids, **kwargs}
    return RemnawaveBackend(live.url, params.pop("api_token", live.token), **params)


def _spec(tg_id: int, **overrides: Any) -> AccountSpec:
    base: dict[str, Any] = {
        "email": f"tg_{tg_id}",
        "uuid": f"0b9a3c0e-5f4e-4c7a-9a4b-{tg_id:012d}",
        "enabled": True,
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0),
        "device_limit": 3,
        "traffic_limit_bytes": 100 * 1024**3,
        "tg_id": tg_id,
        "sub_id": "abcdefghijklmnop",
        "comment": f"vpnbot it {tg_id}",
    }
    base.update(overrides)
    return AccountSpec(**base)


# ---------------------------------------------------------------------- tests


async def test_full_account_lifecycle(live: LivePanel) -> None:
    async with _backend(live) as rw:
        version = await rw.ping()
        assert parse_version(version) >= (3, 0, 0)
        assert set(live.squad_uuids) <= {s.uuid for s in await rw.list_squads()}

        # create
        spec = _spec(100001)
        created = await rw.upsert_account(None, spec)
        assert created.email == spec.email and created.uuid == spec.uuid
        assert created.enabled and created.expires_at == spec.expires_at
        assert created.device_limit == 3 and created.traffic_limit_bytes == spec.traffic_limit_bytes
        assert created.remote_id and created.short_id and created.subscription_url

        # get
        got = await rw.get_account(None, spec.email)
        assert got is not None and got.remote_id == created.remote_id and got.raw["telegramId"] == 100001
        assert {s["uuid"] for s in got.raw["activeInternalSquads"]} == set(live.squad_uuids)

        # update expiry + limits (idempotent: a second identical upsert is a no-op)
        assert spec.expires_at is not None
        new_expiry = spec.expires_at + timedelta(days=60)
        spec = replace(spec, expires_at=new_expiry, device_limit=5, traffic_limit_bytes=0)
        updated = await rw.upsert_account(None, spec)
        assert updated.expires_at == new_expiry and updated.device_limit == 5 and updated.traffic_limit_bytes == 0
        assert updated.remote_id == created.remote_id
        assert (await rw.upsert_account(None, spec)).raw["updatedAt"] == updated.raw["updatedAt"]

        # disable / enable
        disabled = await rw.upsert_account(None, replace(spec, enabled=False))
        assert disabled.enabled is False and disabled.raw["status"] == "DISABLED"
        enabled = await rw.upsert_account(None, spec)
        assert enabled.enabled is True and enabled.raw["status"] == "ACTIVE"

        # links (connection keys): our VLESS id on our host, no placeholders
        links = await rw.get_links(enabled)
        assert links, "expected at least one share link"
        assert any(link.startswith(f"vless://{spec.uuid}@{HOST_ADDRESS}:443") for link in links), links
        assert any(link.startswith("ss://") for link in links), links
        assert not any("0.0.0.0" in link for link in links)

        # proxied subscription
        assert enabled.short_id
        body, headers = await rw.fetch_subscription(
            enabled.short_id, {"User-Agent": "Happ/3.2.1", "x-remnawave-real-ip": "203.0.113.7"}
        )
        decoded = base64.b64decode(body + "=" * (-len(body) % 4)).decode()
        assert spec.uuid in decoded
        assert "subscription-userinfo" in headers

        # reset devices
        user_id = int(enabled.remote_id or 0)
        _http("POST", "/api/hwid/devices", token=live.admin_jwt, body={"hwid": "itdevice000001", "userId": user_id})
        assert _http("GET", f"/api/hwid/devices/{user_id}", token=live.admin_jwt)["total"] == 1
        await rw.reset_devices(enabled)
        assert _http("GET", f"/api/hwid/devices/{user_id}", token=live.admin_jwt)["total"] == 0

        # listing
        assert spec.email in [a.email for a in await rw.list_accounts(None)]

        # delete (twice: the second call is a no-op)
        await rw.delete_account(None, spec.email, spec.uuid)
        assert await rw.get_account(None, spec.email) is None
        await rw.delete_account(None, spec.email, spec.uuid)
        assert await rw.get_links(enabled) == []


async def test_devices_and_server_status(live: LivePanel) -> None:
    async with _backend(live) as rw:
        spec = _spec(100004)
        account = await rw.upsert_account(None, spec)
        user_id = int(account.remote_id or 0)
        try:
            assert await rw.list_devices(account) == []
            for hwid, platform in (("itdevice000101", "iOS"), ("itdevice000102", "Android")):
                _http(
                    "POST",
                    "/api/hwid/devices",
                    token=live.admin_jwt,
                    body={"hwid": hwid, "userId": user_id, "platform": platform, "deviceModel": "Test"},
                )
            devices = {d.id: d for d in await rw.list_devices(account)}
            assert set(devices) == {"itdevice000101", "itdevice000102"}
            assert devices["itdevice000101"].platform == "iOS" and devices["itdevice000101"].model == "Test"

            await rw.delete_device(account, "itdevice000101")
            assert [d.id for d in await rw.list_devices(account)] == ["itdevice000102"]
            await rw.delete_device(account, "itdevice000101")  # already gone: not an error
        finally:
            await rw.delete_account(None, spec.email, spec.uuid)

        # The test stack has no nodes: the list is empty, the panel host's stats come from system:read.
        status = await rw.server_status()
        assert status.nodes == ()
        assert status.mem_total and status.mem_total > 0 and status.cpu_cores
        await rw.restart_core()


async def test_never_expiring_and_past_expiry(live: LivePanel) -> None:
    async with _backend(live) as rw:
        spec = _spec(100002, expires_at=None)
        acc = await rw.upsert_account(None, spec)
        assert acc.expires_at is None
        # The API cannot set a past expiry on update: the date stays, the user is disabled.
        acc = await rw.upsert_account(None, replace(spec, expires_at=datetime.now(UTC) - timedelta(days=1)))
        assert acc.enabled is False and acc.raw["status"] == "DISABLED"
        # Renewal re-activates.
        acc = await rw.upsert_account(None, replace(spec, expires_at=datetime.now(UTC) + timedelta(days=7)))
        assert acc.enabled is True and acc.raw["status"] == "ACTIVE"
        await rw.delete_account(None, spec.email, spec.uuid)


async def test_adopts_existing_user_and_recreates_on_uuid_change(live: LivePanel) -> None:
    spec = _spec(100003)
    _http(
        "POST",
        "/api/users",
        token=live.admin_jwt,
        body={"username": spec.email, "expireAt": "2030-01-01T00:00:00.000Z", "vlessUuid": spec.uuid},
    )
    async with _backend(live) as rw:
        adopted = await rw.upsert_account(None, spec)
        assert adopted.device_limit == 3 and adopted.raw["description"] == spec.comment
        rotated = replace(spec, uuid="0b9a3c0e-5f4e-4c7a-9a4b-ffffffffffff")
        recreated = await rw.upsert_account(None, rotated)
        assert recreated.uuid == rotated.uuid and recreated.remote_id != adopted.remote_id
        await rw.delete_account(None, spec.email, rotated.uuid)


async def test_webhooks_are_signed(live: LivePanel) -> None:
    spec = _spec(100004)
    async with _backend(live) as rw:
        await rw.upsert_account(None, spec)
        await rw.upsert_account(None, replace(spec, enabled=False))
        await rw.delete_account(None, spec.email, spec.uuid)

    wanted = {"user.created", "user.disabled", "user.deleted"}
    seen: dict[str, bool] = {}
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and not wanted <= seen.keys():
        await asyncio.sleep(1)
        for headers, raw in live.hooks.snapshot():
            event = RemnawaveBackend.parse_webhook(raw)
            if event.username == spec.email:
                signature = {k.lower(): v for k, v in headers.items()}.get(SIGNATURE_HEADER.lower())
                seen[event.event] = RemnawaveBackend.verify_webhook(raw, signature, live.webhook_secret)
                assert event.telegram_id == 100004 and event.user_id is not None
    assert wanted <= seen.keys(), f"webhooks received: {sorted(seen)}"
    assert all(seen.values())


async def test_wrong_token_is_auth_error(live: LivePanel) -> None:
    async with _backend(live, api_token="not-a-token") as rw:
        with pytest.raises(PanelAuthError):
            await rw.ping()


async def test_missing_forwarded_headers_drop_the_connection(live: LivePanel) -> None:
    async with _backend(live, direct=False) as rw:
        rw.retry_delays = ()
        with pytest.raises(PanelError, match="direct mode"):
            await rw.ping()
