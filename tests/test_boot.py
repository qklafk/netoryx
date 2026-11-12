"""End-to-end boot of the real application stack with only the Telegram Bot API replaced by a local fake.

Covers the command line in a clean environment (no ``.env``), migrations, and a full start with the real
i18n, middlewares, feature modules and web app, then ``/healthz`` over HTTP and in-process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import aiohttp
import httpx
from aiogram import Bot
from aiogram.methods import DeleteWebhook, GetMe, SetMyCommands

from app.core.i18n import I18n
from app.runtime import Factories, Runtime
from app.web.app import create_web_app
from tests.conftest import make_settings
from tests.test_runtime_boot import TOKEN, FakeTelegram

ROOT = Path(__file__).resolve().parents[1]


def _clean_env(db_path: Path) -> dict[str, str]:
    """Only what the app needs: no inherited configuration, no ``.env`` in the working directory."""
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME") if key in os.environ}
    env.update(
        {
            "BOT_TOKEN": "123:TEST",
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path.as_posix()}",
            "PUBLIC_BASE_URL": "https://example.com",
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def _cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app", *args],
        cwd=tmp_path,
        env=_clean_env(tmp_path / "t.db"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def test_cli_migrate_then_check_without_env_file(tmp_path: Path) -> None:
    migrate = _cli(tmp_path, "migrate")
    assert migrate.returncode == 0, migrate.stdout + migrate.stderr
    assert (tmp_path / "t.db").exists()

    check = _cli(tmp_path, "check", "--offline")
    assert check.returncode == 0, check.stdout + check.stderr
    assert "[OK  ] config" in check.stdout
    assert "[OK  ] migrations" in check.stdout
    assert "[FAIL]" not in check.stdout


async def test_full_stack_boot_and_healthz(tmp_path: Path) -> None:
    telegram = FakeTelegram()
    settings = make_settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'bot.db').as_posix()}",
        web_host="127.0.0.1",
        web_port=0,
        shutdown_timeout_sec=5,
    )
    runtime = Runtime(settings, factories=Factories(bot=lambda _settings: Bot(TOKEN, session=telegram)))
    try:
        ctx = await runtime.start()
        assert ctx.bot_username == "test_bot"
        assert isinstance(ctx.i18n, I18n)
        assert ctx.i18n.t("nav.back", "en") != "nav.back"
        assert "system" in ctx.modules
        assert telegram.of(GetMe)
        assert telegram.of(DeleteWebhook)  # polling mode drops a stale webhook
        assert telegram.of(SetMyCommands)
        assert ctx.dispatcher.update.outer_middleware  # the real middleware stack is installed

        # Over the network, through uvicorn.
        assert runtime.web is not None and runtime.web.port
        url = f"http://127.0.0.1:{runtime.web.port}/healthz"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http, http.get(url) as resp:
            assert resp.status == 200
            body: dict[str, Any] = await resp.json()
        assert body["db"] is True
        assert body["bot"] is True
        assert body["status"] == "ok"
        assert body["panels"] == {"total": 0, "healthy": 0}

        # In-process, against a freshly built app for the same context.
        transport = httpx.ASGITransport(app=create_web_app(ctx))
        async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
            resp2 = await client.get("/healthz")
            assert resp2.status_code == 200
            assert resp2.json()["db"] is True
            assert resp2.headers["cache-control"] == "no-store"
            assert (await client.get("/api/admin/auth/me")).status_code == 401
    finally:
        await runtime.stop()
