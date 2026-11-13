"""Process startup/shutdown per role, development tunnel discovery, logging and the command line."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
import types
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    DeleteWebhook,
    GetMe,
    GetUpdates,
    SetChatMenuButton,
    SetMyCommands,
    SetWebhook,
    TelegramMethod,
)
from aiogram.types import BotCommandScopeChat
from aiogram.types import User as TgUser
from aiohttp import web
from fastapi import FastAPI
from sqlalchemy import func, select

from app import __main__ as cli
from app.config import Settings
from app.core.context import AppContext
from app.db.models import Plan, User
from app.runtime import (
    Factories,
    JsonFormatter,
    LogBuffer,
    Runtime,
    SecretMaskFilter,
    create_bot,
    discover_tunnel_url,
    head_revision,
    run_checks,
    setup_logging,
)
from app.services import kv
from tests.conftest import make_settings

TOKEN = "123456:TEST"


class FakeTelegram(BaseSession):
    """Answers Bot API calls locally and records them."""

    def __init__(self, *, fail_get_me: bool = False) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.fail_get_me = fail_get_me

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:  # noqa: ASYNC109
        self.calls.append(method)
        if isinstance(method, GetMe):
            if self.fail_get_me:
                raise RuntimeError("telegram unreachable")
            return TgUser(id=42, is_bot=True, first_name="Bot", username="test_bot")
        if isinstance(method, GetUpdates):
            await asyncio.sleep(0.02)
            return []
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - unused
        yield b""

    async def close(self) -> None:
        return None

    def of(self, kind: type[Any]) -> list[Any]:
        return [c for c in self.calls if isinstance(c, kind)]


class FakeI18n:
    def t(self, key: str, lang: str, /, **kw: Any) -> str:
        return key


def web_app(ctx: AppContext) -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"bot": ctx.bot_username}

    return app


def factories(telegram: FakeTelegram, calls: list[str]) -> Factories:
    async def i18n(session_factory: Any) -> Any:
        return FakeI18n()

    async def middlewares(dispatcher: Dispatcher, ctx: AppContext) -> None:
        calls.append("middlewares")

    return Factories(
        bot=lambda settings: Bot(TOKEN, session=telegram),
        i18n=i18n,
        web_app=web_app,
        middlewares=middlewares,
    )


def settings_for(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}",
        "core_state_db": str(tmp_path / "core.db"),
        "web_host": "127.0.0.1",
        "web_port": 0,
        "shutdown_timeout_sec": 5,
    }
    base.update(kw)
    return make_settings(**base)


@pytest.fixture
async def stopper() -> AsyncIterator[list[Runtime]]:
    runtimes: list[Runtime] = []
    yield runtimes
    for runtime in runtimes:
        await runtime.stop()


async def test_role_all_polling(tmp_path: Path, stopper: list[Runtime]) -> None:
    telegram = FakeTelegram()
    calls: list[str] = []
    runtime = Runtime(settings_for(tmp_path), factories=factories(telegram, calls))
    stopper.append(runtime)
    ctx = await runtime.start()

    assert calls == ["middlewares"]
    assert ctx.bot_username == "test_bot" and ctx.role == "all" and ctx.webhook_secret is None
    assert ctx.dispatcher["ctx"] is ctx
    assert "system" in ctx.modules
    assert ctx.scheduler.is_scheduled("system.maintenance")
    async with ctx.session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Plan)) == 4  # default plans seeded
    assert runtime.web is not None and runtime.web.port
    async with (
        aiohttp.ClientSession() as http,
        http.get(f"http://127.0.0.1:{runtime.web.port}/ping") as resp,
    ):
        assert await resp.json() == {"bot": "test_bot"}

    for _ in range(100):
        if telegram.of(GetUpdates):
            break
        await asyncio.sleep(0.02)
    assert telegram.of(GetUpdates), "polling did not start"
    assert telegram.of(DeleteWebhook)
    admin_commands = [c for c in telegram.of(SetMyCommands) if isinstance(c.scope, BotCommandScopeChat)]
    assert [c.scope.chat_id for c in admin_commands] == [1000]
    admin_command_names = [b.command for b in admin_commands[0].commands]
    assert "status" in admin_command_names
    assert len(admin_command_names) == len(set(admin_command_names))
    (menu,) = telegram.of(SetChatMenuButton)
    assert menu.menu_button.web_app.url == "https://bot.example.com/app/"

    runtime.request_stop()
    assert await runtime.wait() == 0
    await runtime.stop()
    assert runtime._components == []
    assert not ctx.scheduler.is_scheduled("system.maintenance")

    # Routers can be attached again by a new runtime in the same process.
    again = Runtime(settings_for(tmp_path), role="worker", factories=factories(FakeTelegram(), []))
    stopper.append(again)
    await again.start()


async def test_role_web_with_webhook(tmp_path: Path, stopper: list[Runtime]) -> None:
    telegram = FakeTelegram()
    settings = settings_for(tmp_path, bot_mode="webhook", role="web", app_mode="personal")
    runtime = Runtime(settings, factories=factories(telegram, []))
    stopper.append(runtime)
    ctx = await runtime.start()
    assert ctx.webhook_secret
    async with ctx.session_factory() as s:
        assert await kv.get(s, "telegram_webhook_secret") == ctx.webhook_secret
        assert await s.scalar(select(func.count()).select_from(Plan)) == 0  # personal mode: no plans
    (hook,) = telegram.of(SetWebhook)
    assert hook.url == "https://bot.example.com/telegram/webhook"
    assert hook.secret_token == ctx.webhook_secret
    assert not telegram.of(GetUpdates) and not telegram.of(DeleteWebhook)
    assert ctx.scheduler.job_ids == []  # jobs run in the worker
    assert runtime.web is not None
    assert "polling" not in runtime._tasks


async def test_role_worker_runs_only_jobs(tmp_path: Path, stopper: list[Runtime]) -> None:
    telegram = FakeTelegram()
    runtime = Runtime(settings_for(tmp_path, role="worker"), factories=factories(telegram, []))
    stopper.append(runtime)
    ctx = await runtime.start()
    assert runtime.web is None
    assert ctx.scheduler.is_scheduled("system.maintenance")
    assert not telegram.of(SetMyCommands) and not telegram.of(DeleteWebhook)


async def test_failed_start_cleans_up(tmp_path: Path) -> None:
    runtime = Runtime(settings_for(tmp_path), factories=factories(FakeTelegram(fail_get_me=True), []))
    with pytest.raises(RuntimeError, match="unreachable"):
        await runtime.start()
    assert runtime._components == []


async def test_wait_reports_crashed_component(tmp_path: Path) -> None:
    runtime = Runtime(settings_for(tmp_path))

    async def crash() -> None:
        raise RuntimeError("polling died")

    runtime._tasks["polling"] = asyncio.ensure_future(crash())
    assert await runtime.wait() == 1


async def test_run_until_stopped(tmp_path: Path) -> None:
    runtime = Runtime(settings_for(tmp_path, role="worker"), factories=factories(FakeTelegram(), []))
    task = asyncio.create_task(runtime.run())
    for _ in range(200):
        if runtime.ctx is not None:
            break
        await asyncio.sleep(0.02)
    runtime.request_stop()
    assert await asyncio.wait_for(task, 10) == 0
    assert runtime._components == []


async def test_dev_tunnel_discovery(tmp_path: Path, stopper: list[Runtime]) -> None:
    hits: list[int] = []

    async def quicktunnel(request: web.Request) -> web.Response:
        hits.append(1)
        if len(hits) < 2:
            return web.Response(status=503)
        return web.json_response({"hostname": "abc.trycloudflare.com"})

    app = web.Application()
    app.router.add_get("/quicktunnel", quicktunnel)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    metrics = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as http:
            assert await discover_tunnel_url(http, metrics, delay=0) == "https://abc.trycloudflare.com"
            assert await discover_tunnel_url(http, "http://127.0.0.1:1", attempts=2, delay=0) is None
        settings = settings_for(tmp_path, dev_mode=True, public_base_url=None, dev_tunnel_metrics_url=metrics)
        runtime = Runtime(settings, role="worker", factories=factories(FakeTelegram(), []))
        stopper.append(runtime)
        ctx = await runtime.start()
        assert ctx.settings.public_base_url == "https://abc.trycloudflare.com"
        assert ctx.gateways.get("test") is not None
    finally:
        await runner.cleanup()


def test_create_bot_options() -> None:
    bot = create_bot(make_settings(telegram_api_url="http://bot-api:8081"))
    assert bot.session.api.base == "http://bot-api:8081/bot{token}/{method}"
    assert bot.default.parse_mode == "HTML" and bot.default.link_preview_is_disabled
    try:
        import aiohttp_socks  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="aiohttp-socks"):
            create_bot(make_settings(bot_proxy_url="socks5://127.0.0.1:1080"))


async def test_run_checks(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, auto_migrate=False, public_base_url=None, dev_mode=True, admin_ids=[])
    results = {r.name: r for r in await run_checks(settings, telegram=False)}
    assert results["database"].status == "ok"
    assert results["migrations"].status == "fail"
    assert results["public url"].status == "warn"
    assert results["dev mode"].status == "warn"
    assert results["admins"].status == "warn"


# ---------------------------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def test_logging_masks_secrets_and_buffers(restore_logging: None) -> None:
    stream = io.StringIO()
    handles = setup_logging(make_settings(log_json=True), stream=stream)
    logging.getLogger("test").warning("token is %s", TOKEN)
    try:
        raise ValueError("bad")
    except ValueError:
        logging.getLogger("test").exception("failed")
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0]["msg"] == "token is ***" and lines[0]["level"] == "WARNING"
    assert "ValueError" in lines[1]["exc"]
    assert any("token is ***" in line for line in handles.buffer.lines())
    assert TOKEN not in stream.getvalue()


def test_log_buffer_and_formatters() -> None:
    buffer = LogBuffer(capacity=2)
    for i in range(3):
        buffer.emit(logging.LogRecord("x", logging.INFO, __file__, 1, f"line {i}", None, None))
    assert buffer.lines() == ["line 1", "line 2"]
    assert buffer.lines(1) == ["line 2"]
    mask = SecretMaskFilter(["short", "long-secret-value"])
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "a %s b", ("long-secret-value",), None)
    assert mask.filter(record) and record.getMessage() == "a *** b"
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "short", None, None)
    mask.filter(record)
    assert record.getMessage() == "short"  # too short to be masked safely
    assert json.loads(JsonFormatter().format(record))["msg"] == "short"


# ---------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "BOT_TOKEN": TOKEN,
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}",
        "ADMIN_IDS": "1000",
        "PUBLIC_BASE_URL": "https://bot.example.com",
        "SECRET_KEY": "cli-secret-key-0123456789",
    }.items():
        monkeypatch.setenv(key, value)
    return tmp_path


def test_cli_migrate_and_check(env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--role", "worker", "check", "--offline"]) == 0  # migrated on start (AUTO_MIGRATE)
    assert "[WARN] migrations" in capsys.readouterr().out
    monkeypatch.setenv("AUTO_MIGRATE", "false")
    assert cli.main(["check", "--offline"]) == 1
    assert "[FAIL] migrations" in capsys.readouterr().out
    assert cli.main(["migrate"]) == 0
    assert f"revision {head_revision()}" in capsys.readouterr().out
    assert cli.main(["check", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "[OK  ] database" in out and f"[OK  ] migrations: at {head_revision()}" in out


def test_cli_invalid_config(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Mars/Olympus")
    with pytest.raises(SystemExit) as info:
        cli.main(["check", "--offline"])
    assert "Invalid configuration" in str(info.value) and "TZ" in str(info.value)


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    from app.core import __version__

    assert f"Netoryx {__version__}" in capsys.readouterr().out


def test_cli_admin_link(env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["migrate"]) == 0
    auth = types.ModuleType("app.web.auth")
    seen: dict[str, Any] = {}

    async def create_login_link(session_factory: Any, settings: Settings, tg_id: int) -> str:
        seen["tg_id"] = tg_id
        return f"{settings.public_base_url}/admin/login#token=abc"

    auth.create_login_link = create_login_link  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.web.auth", auth)

    assert cli.main(["create-admin-link"]) == 0
    assert seen["tg_id"] == 1000
    assert "https://bot.example.com/admin/login#token=abc" in capsys.readouterr().out
    assert cli.main(["create-admin-link", "--tg-id", "7"]) == 0 and seen["tg_id"] == 7
    monkeypatch.setenv("ADMIN_IDS", "")
    assert cli.main(["create-admin-link"]) == 2


async def _count_users(url: str) -> int:
    from app.db.session import create_engine

    engine = create_engine(url)
    try:
        async with engine.connect() as conn:
            return int(await conn.scalar(select(func.count()).select_from(User)) or 0)
    finally:
        await engine.dispose()


async def _add_user(url: str, tg_id: int) -> None:
    from app.db.session import create_engine, create_session_factory

    engine = create_engine(url)
    try:
        async with create_session_factory(engine).begin() as s:
            s.add(User(tg_id=tg_id, first_name="U", referral_code=f"R{tg_id}", sub_token=f"token{tg_id}"))
    finally:
        await engine.dispose()


@pytest.mark.parametrize("password", [None, "backup-pass"])
def test_cli_backup_and_restore_into_a_new_database(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], password: str | None
) -> None:
    """The real backup module: back up one database, restore it into a fresh one (no SECRET_KEY set)."""
    monkeypatch.delenv("SECRET_KEY")
    monkeypatch.setenv("BACKUP_DIR", str(env / "backups"))
    if password:
        monkeypatch.setenv("BACKUP_PASSWORD", password)
    source = f"sqlite+aiosqlite:///{env / 'source.db'}"
    monkeypatch.setenv("DATABASE_URL", source)
    assert cli.main(["migrate"]) == 0
    asyncio.run(_add_user(source, 77))
    capsys.readouterr()
    assert cli.main(["backup"]) == 0
    (file,) = list((env / "backups").iterdir())
    assert str(file) in capsys.readouterr().out
    assert file.name.endswith(".enc") == bool(password)

    target = f"sqlite+aiosqlite:///{env / 'target.db'}"
    monkeypatch.setenv("DATABASE_URL", target)
    assert cli.main(["migrate"]) == 0
    assert cli.main(["restore", str(env / "missing.gz"), "--yes"]) == 2
    assert cli.main(["restore", str(file)]) == 2  # not confirmed
    assert asyncio.run(_count_users(target)) == 0
    assert cli.main(["restore", str(file), "--yes"]) == 0
    assert "restored" in capsys.readouterr().out
    assert asyncio.run(_count_users(target)) == 1
