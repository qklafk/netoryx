"""Marzban adapter and adoption against the official Docker image; opt in with MARZBAN_INTEGRATION=1."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import Panel, PanelKind, Provision, Subscription, User
from app.db.session import SessionFactory
from app.services import imports
from app.services.panels.base import AccountSpec
from app.services.panels.marzban import MarzbanBackend
from app.utils.crypto import SecretBox

pytestmark = pytest.mark.skipif(
    os.environ.get("MARZBAN_INTEGRATION") != "1", reason="set MARZBAN_INTEGRATION=1 to run (needs Docker)"
)
IMAGE = os.environ.get("MARZBAN_IT_IMAGE", "gozargah/marzban:latest")
ADMIN = "integration_admin"
PASSWORD = "IntegrationAdminPassword2026"


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=600).stdout.strip()


@pytest.fixture(scope="module")
def panel_url() -> Iterator[str]:
    name = "vpnbot-marzban-it-" + uuid4().hex[:10]
    config = Path(__file__).with_name("xray_config.json").resolve()
    try:
        docker(
            "run",
            "-d",
            "--name",
            name,
            "-p",
            "127.0.0.1::8000",
            "-e",
            "UVICORN_HOST=0.0.0.0",
            "-e",
            "SQLALCHEMY_DATABASE_URL=sqlite:////tmp/marzban-test.db",
            "-e",
            f"SUDO_USERNAME={ADMIN}",
            "-e",
            f"SUDO_PASSWORD={PASSWORD}",
            "-e",
            "XRAY_SUBSCRIPTION_URL_PREFIX=https://panel.example",
            "-v",
            f"{config}:/code/xray_config.json:ro",
            IMAGE,
            "bash",
            "-c",
            "alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port 8000",
        )
        port = docker("port", name, "8000/tcp").rsplit(":", 1)[1]
        url = "http://127.0.0.1:" + port
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url + "/api/system", timeout=3):
                    break
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("Marzban did not become ready: " + docker("logs", "--tail", "30", name))
        docker("exec", name, "marzban-cli", "admin", "import-from-env", "--yes")

        async def configure_hosts() -> None:
            backend = MarzbanBackend(url, username=ADMIN, password=PASSWORD)
            try:
                await backend._request(
                    "PUT",
                    "/api/hosts",
                    json={"VLESS TCP": [{"remark": "Integration DE", "address": "de.example.com", "port": 10808}]},
                )
            finally:
                await backend.close()

        asyncio.run(configure_hosts())
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, check=False, timeout=60)


def desired(name: str) -> AccountSpec:
    return AccountSpec(
        email=name,
        uuid=str(uuid4()),
        enabled=True,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        device_limit=0,
        traffic_limit_bytes=1_500_000_000,
        tg_id=101,
        sub_id=uuid4().hex[:16],
    )


async def test_real_crud_links_and_monitor(panel_url: str) -> None:
    backend = MarzbanBackend(panel_url, username=ADMIN, password=PASSWORD, timeout=30)
    spec = desired("it_" + uuid4().hex[:10])
    try:
        assert await backend.ping()
        account = await backend.upsert_account(None, spec)
        assert account.uuid == spec.uuid and account.traffic_limit_bytes == 1_500_000_000
        old_url = account.subscription_url
        assert old_url and await backend.get_links(account)
        assert spec.expires_at is not None
        updated = await backend.upsert_account(None, replace(spec, expires_at=spec.expires_at + timedelta(days=1)))
        assert updated.subscription_url == old_url and updated.uuid == spec.uuid
        assert any(row.email == spec.email for row in await backend.list_accounts(None))
        await backend.reset_traffic(None, spec.email)
        assert await backend.get_used_bytes(None, spec.email) == 0
        disabled = await backend.upsert_account(None, replace(spec, enabled=False))
        assert not disabled.enabled
        assert (await backend.server_status()).core_version
        await backend.restart_core()
        await asyncio.sleep(0.5)
    finally:
        await backend.delete_account(None, spec.email, spec.uuid)
        assert await backend.get_account(None, spec.email) is None
        await backend.close()


@dataclass
class Registry:
    backend: MarzbanBackend

    async def get(self, panel_id: int) -> MarzbanBackend:
        return self.backend


async def test_real_import_retains_native_profile_and_url(
    panel_url: str, session_factory: SessionFactory, settings: Settings
) -> None:
    backend = MarzbanBackend(panel_url, username=ADMIN, password=PASSWORD, timeout=30)
    spec = desired("import_" + uuid4().hex[:10])
    box = SecretBox("integration-import-secret")
    try:
        before = await backend.upsert_account(None, spec)
        async with session_factory.begin() as session:
            session.add(User(tg_id=101, referral_code="r101", sub_token="original-token"))
            panel = Panel(name="Marzban", kind=PanelKind.MARZBAN, url=panel_url)
            session.add(panel)
            await session.flush()
            panel_id = panel.id
        registry = Registry(backend)
        preview = await imports.preview(session_factory, registry, panel_id)
        assert any(item["username"] == spec.email for item in preview["items"])
        result = await imports.apply(
            session_factory,
            registry,
            settings,
            box,
            panel_id,
            [imports.Selection(spec.email, 101)],
            "live-import-request",
        )
        assert result["imported"] == 1 and not result["errors"]
        after = await backend.get_account(None, spec.email)
        assert after and after.uuid == before.uuid and after.subscription_url == before.subscription_url
        async with session_factory() as session:
            sub = await session.scalar(select(Subscription))
            provision = await session.scalar(select(Provision))
            assert sub and provision and sub.traffic_limit_bytes == 1_500_000_000
            assert provision.remote_email == spec.email
    finally:
        await backend.delete_account(None, spec.email, spec.uuid)
        await backend.close()
