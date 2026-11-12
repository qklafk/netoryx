"""TelemtBackend and the Telegram proxy service against a real telemt container.

Skipped unless ``TGPROXY_INTEGRATION=1``. Requires Docker (pulls ~20 MB on first run).

    TGPROXY_INTEGRATION=1 .venv/Scripts/python -m pytest -q tests/integration/tgproxy

The module fixture writes a minimal config (Control API enabled, ``auth_header`` set, Fake-TLS mode, a placeholder
user because telemt keeps at least one) into a fresh Docker volume, starts ``ghcr.io/telemt/telemt`` on a random
localhost port and removes the container and the volume afterwards. The volume (not a bind mount) keeps the API's
atomic config writes working on every host OS.

Optional environment:
* ``TGPROXY_IT_IMAGE`` image override (default: the pinned release below)
* ``TGPROXY_IT_KEEP=1`` leave the container running (for debugging)
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app.db.models import Panel, PanelKind, User
from app.db.session import SessionFactory
from app.services import tg_proxy
from app.services.panels.base import AccountSpec, PanelAuthError
from app.services.panels.mtproto import ProxyUserSpec, fake_tls_secret, new_key, parse_secret
from app.services.panels.registry import PanelRegistry
from app.services.panels.telemt import TelemtBackend
from app.utils.crypto import SecretBox
from tests.conftest import make_settings

pytestmark = pytest.mark.skipif(
    os.environ.get("TGPROXY_INTEGRATION") != "1", reason="set TGPROXY_INTEGRATION=1 to run (needs Docker)"
)

IMAGE = os.environ.get("TGPROXY_IT_IMAGE", "ghcr.io/telemt/telemt:3.5.16")
STARTUP_TIMEOUT_SEC = 60
PUBLIC_HOST = "proxy.example.com"
TLS_DOMAIN = "petrovich.ru"
TAG = "0123456789abcdef0123456789abcdef"

CONFIG = """\
[general]
use_middle_proxy = false
log_level = "normal"

[general.modes]
classic = false
secure = false
tls = true

[general.links]
show = []
public_host = "{public_host}"
public_port = 443

[server]
port = 443

[server.api]
enabled = true
listen = "0.0.0.0:9091"
whitelist = ["0.0.0.0/0", "::/0"]
auth_header = "{auth}"

[censorship]
tls_domain = "{domain}"
mask = false
tls_emulation = false

[access.users]
keep = "{keep}"
"""


def _docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=600)
    return result.stdout


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class LiveTelemt:
    url: str
    auth: str

    def backend(self, **kw: object) -> TelemtBackend:
        return TelemtBackend(self.url, auth_header=self.auth, timeout=10, **kw)  # type: ignore[arg-type]


def _wait_ready(url: str, auth: str, container: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
    request = urllib.request.Request(f"{url}/v1/health", headers={"Authorization": auth})
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=3) as resp:
                if json.loads(resp.read()).get("ok") is True:
                    return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    logs = _docker("logs", "--tail", "50", container, check=False)
    raise RuntimeError(f"telemt did not become ready in {STARTUP_TIMEOUT_SEC}s:\n{logs}")


@pytest.fixture(scope="module")
def telemt(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveTelemt]:
    suffix = secrets.token_hex(4)
    name, volume = f"vpnbot-it-telemt-{suffix}", f"vpnbot-it-telemt-{suffix}"
    auth = f"Bearer {secrets.token_urlsafe(32)}"
    config: Path = tmp_path_factory.mktemp("telemt") / "config.toml"
    config.write_text(
        CONFIG.format(public_host=PUBLIC_HOST, auth=auth, domain=TLS_DOMAIN, keep=new_key()), encoding="utf-8"
    )
    port = _free_port()
    _docker("volume", "create", volume)
    try:
        _docker(
            "create",
            "--name",
            name,
            "--user",
            "0:0",  # the fresh volume is root-owned; the image runs as nonroot by default
            "-p",
            f"127.0.0.1:{port}:9091",
            "-v",
            f"{volume}:/etc/telemt",
            "--tmpfs",
            "/run/telemt:rw,mode=1777,size=4m",
            "-w",
            "/run/telemt",
            IMAGE,
            "/etc/telemt/config.toml",
        )
        _docker("cp", str(config), f"{name}:/etc/telemt/config.toml")
        _docker("start", name)
        url = f"http://127.0.0.1:{port}"
        _wait_ready(url, auth, name)
        yield LiveTelemt(url, auth)
    finally:
        if os.environ.get("TGPROXY_IT_KEEP") != "1":
            _docker("rm", "-f", "-v", name, check=False)
            _docker("volume", "rm", "-f", volume, check=False)


@pytest.fixture
async def backend(telemt: LiveTelemt) -> AsyncIterator[TelemtBackend]:
    instance = telemt.backend()
    yield instance
    await instance.close()


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def test_ping_and_auth(telemt: LiveTelemt, backend: TelemtBackend) -> None:
    version = await backend.ping()
    tag = IMAGE.rsplit(":", 1)[-1]
    assert version != "unknown" and (version == tag or not tag[:1].isdigit())
    wrong = TelemtBackend(telemt.url, auth_header="Bearer nope", timeout=10)
    try:
        with pytest.raises(PanelAuthError):
            await wrong.ping()
    finally:
        await wrong.close()


async def test_user_lifecycle_and_links(backend: TelemtBackend) -> None:
    name = f"it{secrets.token_hex(3)}"
    key = new_key()
    expires = (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0)
    created = await backend.upsert_proxy_user(
        None,
        ProxyUserSpec(
            name=name, enabled=True, key=key, expires_at=expires, max_unique_ips=2, rate_limit_bps=3_000_000, ad_tag=TAG
        ),
    )
    assert created.key == key and created.enabled and created.expires_at == expires
    assert (created.max_unique_ips, created.rate_limit_bps, created.ad_tag) == (2, 3_000_000, TAG)
    (link,) = created.links
    assert (link.server, link.port, link.secret) == (PUBLIC_HOST, 443, fake_tls_secret(key, TLS_DOMAIN))
    assert _query(link.tme_url) == {"server": PUBLIC_HOST, "port": "443", "secret": link.secret}
    assert link.tg_url.startswith("tg://proxy?") and link.tme_url.startswith("https://t.me/proxy?")

    # Idempotent: the same spec again changes nothing (no PATCH would be needed).
    same = await backend.upsert_proxy_user(
        None,
        ProxyUserSpec(
            name=name, enabled=True, key=key, expires_at=expires, max_unique_ips=2, rate_limit_bps=3_000_000, ad_tag=TAG
        ),
    )
    assert same.key == key and same.raw.get("expiration_rfc3339")

    # Update: disable, drop limits and the ad tag, remove the expiry.
    disabled = await backend.upsert_proxy_user(None, ProxyUserSpec(name=name, enabled=False, key=key))
    assert not disabled.enabled and disabled.expires_at is None
    assert (disabled.max_unique_ips, disabled.rate_limit_bps, disabled.ad_tag) == (0, 0, None)
    fetched = await backend.get_proxy_user(None, name)
    assert fetched is not None and not fetched.enabled

    await backend.set_enabled(name, True)
    enabled = await backend.get_proxy_user(None, name)
    assert enabled is not None and enabled.enabled and enabled.key == key

    # New key through upsert (the bot's rotation path) and through rotate-secret.
    key2 = new_key()
    moved = await backend.upsert_proxy_user(None, ProxyUserSpec(name=name, enabled=True, key=key2))
    assert moved.key == key2 and parse_secret(moved.links[0].secret).key == key2  # type: ignore[union-attr]
    rotated = await backend.rotate_secret(name)
    assert rotated.key not in (None, key, key2)
    assert (await backend.get_proxy_user(None, name)).key == rotated.key  # type: ignore[union-attr]

    assert name in [u.name for u in await backend.list_proxy_users(None)]
    await backend.reset_traffic(None, name)
    await backend.delete_proxy_user(None, name)
    await backend.delete_proxy_user(None, name)  # not found is success
    assert await backend.get_proxy_user(None, name) is None


async def test_generic_upsert_and_create_with_generated_secret(backend: TelemtBackend) -> None:
    name = f"it{secrets.token_hex(3)}"
    generated = await backend.upsert_proxy_user(None, ProxyUserSpec(name=name, enabled=False))
    assert generated.key is not None and not generated.enabled
    remote = await backend.upsert_account(
        None,
        AccountSpec(
            email=name,
            uuid="",
            enabled=True,
            expires_at=None,
            device_limit=1,
            traffic_limit_bytes=1024**3,
            tg_id=1,
            sub_id="",
        ),
    )
    assert remote.enabled and remote.device_limit == 1 and remote.traffic_limit_bytes == 1024**3
    assert remote.uuid == generated.key  # the secret is kept when no key is given
    assert (await backend.get_links(remote))[0].startswith(f"tg://proxy?server={PUBLIC_HOST}&port=443&secret=ee")
    await backend.reset_devices(remote)
    await backend.delete_account(None, name, "")
    assert await backend.get_account(None, name) is None


async def test_service_end_to_end(telemt: LiveTelemt, session_factory: SessionFactory) -> None:
    box = SecretBox("live-test-secret-key-0123456789")
    settings = make_settings(tg_proxy_free_rate_limit_mbps=2)
    async with session_factory.begin() as session:
        session.add(User(tg_id=4242, referral_code="r4242", sub_token="t4242"))
        panel = Panel(name="telemt", kind=PanelKind.TELEMT, url=telemt.url, api_token=box.encrypt(telemt.auth))
        session.add(panel)
        await session.flush()
        panel_id = panel.id
    registry = PanelRegistry(session_factory, settings, box)
    try:
        view = await tg_proxy.open_proxy(session_factory, registry, settings, box, 4242)
        (entry,) = view.ready
        (link,) = entry.links
        backend = await registry.get(panel_id)
        assert isinstance(backend, TelemtBackend)
        account = await backend.get_proxy_user(None, "tg4242")
        assert account is not None and account.enabled and account.rate_limit_bps == 2_000_000
        assert link.secret == fake_tls_secret(account.key or "", TLS_DOMAIN)

        await tg_proxy.rotate(session_factory, box, 4242)
        await tg_proxy.sync_user(session_factory, registry, settings, box, 4242)
        fresh = await tg_proxy.user_view(session_factory, settings, box, 4242)
        assert fresh.ready[0].links[0] != link

        async with session_factory.begin() as session:
            user = await session.get(User, 4242)
            assert user is not None
            user.is_banned = True
        await tg_proxy.reconcile(session_factory, registry, settings, box)
        banned = await backend.get_proxy_user(None, "tg4242")
        assert banned is not None and not banned.enabled

        async with session_factory.begin() as session:
            panel_row = await session.get(Panel, panel_id)
            assert panel_row is not None
            panel_row.is_active = False
        report = await tg_proxy.reconcile(session_factory, registry, settings, box)
        assert report.deleted == 1 and await backend.get_proxy_user(None, "tg4242") is None
    finally:
        await registry.close()
