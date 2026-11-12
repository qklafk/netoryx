"""Process runtime: builds the application context, starts bot / web server / jobs and shuts them down cleanly."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import logging
import re
import signal
import sys
import threading
from collections import deque
from collections.abc import Awaitable, Callable, Generator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION, TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, MenuButtonWebApp, WebAppInfo
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.config import Settings
from app.core import __version__, ui
from app.core.context import AppContext, GatewayRegistry
from app.core.locks import LockManager, set_default_manager
from app.core.modules import FeatureModule, ModuleRegistry, guard_router, load_modules
from app.core.settings_provider import SettingsProvider
from app.core.settings_registry import register as register_settings
from app.db.models import Admin
from app.db.session import SessionFactory, create_engine, create_session_factory
from app.scheduler import Scheduler
from app.services import kv, notifications, plans, users
from app.services.maintenance import MaintenanceService
from app.services.panels import inventory
from app.services.panels.registry import PanelRegistry
from app.utils.crypto import SecretBox
from app.utils.security import random_token

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_INI = ROOT_DIR / "alembic.ini"
MIGRATIONS_DIR = ROOT_DIR / "migrations"
WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_SECRET_KEY = "telegram_webhook_secret"
SETTINGS_POLL_INTERVAL_SEC = 15.0
SEED_LOCK = "startup:seed"
UPDATE_DRAIN_SEC = 10.0  # polling: how long shutdown waits for updates still being handled
MIGRATION_LOCK_ID = 7_316_402_115  # arbitrary constant for PostgreSQL advisory / MySQL named locks
MYSQL_LOCK_NAME = "vpnbot_migrations"
LOG_BUFFER_SIZE = 1000


# =============================================================================================
# Logging
# =============================================================================================
class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message (+ exception)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = record.exc_text or self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# Telegram bot tokens look the same everywhere (also inside URLs of failed requests).
_BOT_TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")


class SecretMaskFilter(logging.Filter):
    """Replaces known secret values (bot token ...) in log messages and exception texts with ``***``."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 8]

    def add(self, secret: str | None) -> None:
        if secret and len(secret) >= 8 and secret not in self._secrets:
            self._secrets.append(secret)

    def _mask(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return _BOT_TOKEN.sub("***", text)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        masked = self._mask(message)
        if masked != message:
            record.msg, record.args = masked, None
        if record.exc_info and not record.exc_text:
            # The formatters append the traceback themselves; hand them the masked text instead.
            trace = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self._mask(trace)
        return True


class LogBuffer(logging.Handler):
    """Keeps the last N formatted log lines in memory (admin "System" page)."""

    def __init__(self, capacity: int = LOG_BUFFER_SIZE) -> None:
        super().__init__()
        self._lines: deque[str] = deque(maxlen=capacity)
        self._lock_buffer = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._lock_buffer:
            self._lines.append(line)

    def lines(self, limit: int = 200) -> list[str]:
        with self._lock_buffer:
            items = list(self._lines)
        return items[-limit:] if limit > 0 else items


@dataclass(slots=True)
class LoggingHandles:
    buffer: LogBuffer
    mask: SecretMaskFilter


def setup_logging(settings: Settings, *, stream: Any = None) -> LoggingHandles:
    """Configure the root logger (text or JSON), secret masking and the in-memory buffer."""
    formatter: logging.Formatter
    if settings.log_json:
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    mask = SecretMaskFilter()
    if settings.bot_token is not None:
        mask.add(settings.bot_token.get_secret_value())
    if settings.secret_key is not None:
        mask.add(settings.secret_key.get_secret_value())
    console = logging.StreamHandler(stream or sys.stderr)
    console.setFormatter(formatter)
    console.addFilter(mask)
    buffer = LogBuffer()
    buffer.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    buffer.addFilter(mask)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(console)
    root.addHandler(buffer)
    root.setLevel(settings.log_level.upper())
    for noisy in ("aiogram.event", "apscheduler.executors.default", "uvicorn.access", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return LoggingHandles(buffer=buffer, mask=mask)


def init_sentry(settings: Settings) -> bool:
    if not settings.sentry_dsn:
        return False
    try:
        sentry_sdk = importlib.import_module("sentry_sdk")
    except ImportError:
        log.warning("SENTRY_DSN is set but sentry-sdk is not installed")
        return False
    sentry_sdk.init(dsn=settings.sentry_dsn, release=f"netoryx@{__version__}", send_default_pii=False)
    return True


# =============================================================================================
# Database migrations
# =============================================================================================
def alembic_config() -> AlembicConfig:
    cfg = AlembicConfig(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def _run_upgrade(connection: Connection, revision: str) -> None:
    tables = set(sa_inspect(connection).get_table_names())
    if "alembic_version" not in tables and "users" in tables:
        raise RuntimeError(
            "the database already has tables but no migration history; if it was created by this version, "
            "mark it as up to date with `alembic stamp head`"
        )
    cfg = alembic_config()
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, revision)


async def _lock_migrations(conn: AsyncConnection) -> None:
    dialect = conn.dialect.name
    if dialect == "postgresql":
        await conn.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": MIGRATION_LOCK_ID})
    elif dialect in ("mysql", "mariadb"):
        got = await conn.scalar(text("SELECT GET_LOCK(:name, 300)"), {"name": MYSQL_LOCK_NAME})
        if got != 1:
            raise RuntimeError("timed out waiting for another process to finish database migrations")


async def _unlock_migrations(conn: AsyncConnection) -> None:
    if conn.dialect.name in ("mysql", "mariadb"):
        await conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": MYSQL_LOCK_NAME})
        await conn.commit()


async def upgrade_database(engine: AsyncEngine, revision: str = "head") -> str | None:
    """Apply migrations (one process at a time); returns the resulting revision."""
    async with engine.connect() as conn:
        await _lock_migrations(conn)
        try:
            await conn.run_sync(_run_upgrade, revision)
            await conn.commit()
        finally:
            await _unlock_migrations(conn)
    current, _head = await migration_status(engine)
    return current


async def migration_status(engine: AsyncEngine) -> tuple[str | None, str | None]:
    """(current revision of the database, latest revision available)."""

    def current(connection: Connection) -> str | None:
        return MigrationContext.configure(connection).get_current_revision()

    async with engine.connect() as conn:
        revision = await conn.run_sync(current)
    return revision, head_revision()


# =============================================================================================
# Telegram
# =============================================================================================
def create_bot(settings: Settings) -> Bot:
    """Bot with HTML parse mode, no link previews, optional outbound proxy and self-hosted Bot API server."""
    if settings.bot_token is None or not settings.bot_token.get_secret_value():
        raise RuntimeError("BOT_TOKEN is required when the Telegram bot runtime is enabled")
    if settings.bot_proxy_url:
        try:
            importlib.import_module("aiohttp_socks")
        except ImportError as exc:
            raise RuntimeError("BOT_PROXY_URL needs the aiohttp-socks package") from exc
    api: TelegramAPIServer = (
        TelegramAPIServer.from_base(settings.telegram_api_url) if settings.telegram_api_url else PRODUCTION
    )
    session = AiohttpSession(proxy=settings.bot_proxy_url, api=api)
    return Bot(
        token=settings.bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def payment_return_url(settings: Settings, bot_username: str) -> str | None:
    """Where payment providers send customers: the bot, or a neutral page when the web cabinet is on."""
    base = (settings.public_base_url or "").rstrip("/")
    if settings.cabinet_enabled and base.startswith("https://"):
        return f"{base}/pay/return"
    return f"https://t.me/{bot_username}" if bot_username else None


def create_redis(url: str | None) -> Any | None:
    if not url:
        return None
    try:
        redis_asyncio = importlib.import_module("redis.asyncio")
    except ImportError as exc:
        raise RuntimeError("REDIS_URL needs the redis package (install the 'redis' extra)") from exc
    return redis_asyncio.Redis.from_url(url)


def create_storage(redis: Any | None) -> BaseStorage:
    if redis is None:
        return MemoryStorage()
    from aiogram.fsm.storage.redis import RedisStorage

    return RedisStorage(redis=redis)


async def discover_tunnel_url(
    http: aiohttp.ClientSession, metrics_url: str, *, attempts: int = 30, delay: float = 1.0
) -> str | None:
    """Public URL of a cloudflared quick tunnel, read from its metrics endpoint (``/quicktunnel``)."""
    url = metrics_url.rstrip("/") + "/quicktunnel"
    for attempt in range(1, attempts + 1):
        try:
            async with http.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    hostname = str(data.get("hostname") or "").strip() if isinstance(data, dict) else ""
                    if hostname:
                        return hostname if hostname.startswith("https://") else f"https://{hostname}"
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            log.debug("tunnel discovery attempt %d failed: %s", attempt, exc)
        if attempt < attempts:
            await asyncio.sleep(delay)
    return None


async def get_webhook_secret(session_factory: SessionFactory, settings: Settings) -> str:
    """``TELEGRAM_WEBHOOK_SECRET`` or a generated one persisted in the database."""
    if settings.telegram_webhook_secret is not None and settings.telegram_webhook_secret.get_secret_value():
        return settings.telegram_webhook_secret.get_secret_value()
    async with session_factory.begin() as session:
        existing = await kv.get(session, WEBHOOK_SECRET_KEY)
        if existing:
            return existing
        candidate = random_token(32)
        if await kv.add(session, WEBHOOK_SECRET_KEY, candidate):
            return candidate
        winner = await kv.get(session, WEBHOOK_SECRET_KEY, fresh=True)
    return winner or candidate


def include_module_routers(dispatcher: Dispatcher, modules: ModuleRegistry) -> list[Router]:
    """Include every module's routers (admin routers first), each behind a module switch."""
    guards: list[Router] = []
    ordered = [(m, r) for m in modules for r in m.admin_bot_routers] + [(m, r) for m in modules for r in m.bot_routers]
    for module, router in ordered:
        guard = guard_router(Router(name=f"module:{module.name}:{router.name}"), module.name)
        guard.include_router(router)
        dispatcher.include_router(guard)
        guards.append(guard)
    return guards


def detach_routers(dispatcher: Dispatcher, guards: list[Router]) -> None:
    """Undo :func:`include_module_routers` so module routers can be attached again (tests, restarts)."""
    for guard in guards:
        for child in list(guard.sub_routers):
            guard.sub_routers.remove(child)
            child._parent_router = None
        if guard in dispatcher.sub_routers:
            dispatcher.sub_routers.remove(guard)
        guard._parent_router = None


async def admin_chat_ids(session_factory: SessionFactory, settings: Settings) -> list[int]:
    async with session_factory() as session:
        extra = list(await session.scalars(select(Admin.tg_id)))
    return list(dict.fromkeys([*settings.admin_ids, *extra]))


async def setup_bot_ui(ctx: AppContext) -> None:
    """Command menus (users / admins, RU / EN) and the Mini App menu button. Best effort."""
    settings = ctx.settings
    bot = ctx.bot
    try:
        user_commands = ctx.modules.commands(settings, admin=False)
        admin_commands = ctx.modules.commands(settings, admin=True)
        for lang in users.enabled_languages(settings):
            code = None if lang == settings.default_language else lang
            if user_commands:
                await bot.set_my_commands(
                    [BotCommand(command=c.command, description=c.description(lang)) for c in user_commands],
                    scope=BotCommandScopeDefault(),
                    language_code=code,
                )
        if admin_commands:
            commands = [
                BotCommand(command=c.command, description=c.description(settings.default_language))
                for c in admin_commands
            ]
            for chat_id in await admin_chat_ids(ctx.session_factory, settings):
                await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=chat_id))
    except Exception as exc:
        log.warning("failed to set bot commands: %s", exc)
    base = settings.public_base_url or ""
    if settings.miniapp_enabled and base.startswith("https://"):
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text=settings.brand_name[:32], web_app=WebAppInfo(url=f"{base}/app/"))
            )
        except Exception as exc:
            log.warning("failed to set the Mini App menu button: %s", exc)


# =============================================================================================
# Pluggable parts (replaced in tests)
# =============================================================================================
async def default_i18n(session_factory: SessionFactory) -> Any:
    i18n_module = importlib.import_module("app.core.i18n")
    i18n = i18n_module.I18n(session_factory)
    await i18n.load()
    return i18n


def default_web_app(ctx: AppContext) -> Any:
    web = importlib.import_module("app.web.app")
    return web.create_web_app(ctx)


async def default_middlewares(dispatcher: Dispatcher, ctx: AppContext) -> Any:
    middlewares = importlib.import_module("app.middlewares")
    setup = getattr(middlewares, "setup_middlewares", None)
    if setup is None:
        log.warning("app.middlewares.setup_middlewares is missing: updates are handled without middlewares")
        return None
    result = setup(dispatcher, ctx)
    if inspect.isawaitable(result):
        result = await result
    return result


@dataclass(slots=True)
class Factories:
    bot: Callable[[Settings], Bot] = create_bot
    i18n: Callable[[SessionFactory], Awaitable[Any]] = default_i18n
    web_app: Callable[[AppContext], Any] = default_web_app
    middlewares: Callable[[Dispatcher, AppContext], Awaitable[Any]] = default_middlewares
    modules: Callable[[], list[FeatureModule]] = load_modules


def graceful_shutdown_sec(settings: Settings) -> int:
    """How long the web server waits for open connections on shutdown (a quarter of the stop timeout, 1..5 s)."""
    return max(1, min(5, settings.shutdown_timeout_sec // 4))


class _UvicornServer:
    """uvicorn.Server that leaves signal handling to the runtime."""

    def __init__(self, app: Any, settings: Settings) -> None:
        import uvicorn

        class Server(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self) -> Generator[None]:
                yield

        config = uvicorn.Config(
            app,
            host=settings.web_host,
            port=settings.web_port,
            log_config=None,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            lifespan="on",
            # Long-lived responses (progress streams) are cut after this, so the lifespan shutdown (which drains
            # webhook updates) still runs within the runtime's own stop timeout.
            timeout_graceful_shutdown=graceful_shutdown_sec(settings),
        )
        self.server = Server(config)
        self.task: asyncio.Task[None] | None = None

    async def start(self, wait_sec: float = 15.0) -> None:
        self.task = asyncio.create_task(self.server.serve(), name="web-server")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_sec
        while not self.server.started:
            if self.task.done():
                exc = self.task.exception() if not self.task.cancelled() else None
                raise RuntimeError(f"web server failed to start: {exc!r}")
            if loop.time() > deadline:
                raise RuntimeError("web server did not start in time")
            await asyncio.sleep(0.05)

    @property
    def port(self) -> int | None:
        servers = getattr(self.server, "servers", None) or []
        for srv in servers:
            for sock in srv.sockets:
                return int(sock.getsockname()[1])
        return None

    async def stop(self) -> None:
        self.server.should_exit = True
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)


# =============================================================================================
# Runtime
# =============================================================================================
Cleanup = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class _Component:
    name: str
    cleanup: Cleanup


@dataclass
class Runtime:
    """Starts the components needed by the process role and stops them in reverse order."""

    env_settings: Settings
    role: str | None = None
    factories: Factories = field(default_factory=Factories)
    log_buffer: LogBuffer | None = None
    ctx: AppContext | None = None
    web: _UvicornServer | None = None
    _components: list[_Component] = field(default_factory=list)
    _tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _guards: list[Router] = field(default_factory=list)
    _replayer: Any = None  # replays of intercepted /start updates (from the middlewares)

    def __post_init__(self) -> None:
        self.role = self.role or self.env_settings.role

    @property
    def runs_bot(self) -> bool:
        return self.role in ("all", "bot")

    @property
    def runs_jobs(self) -> bool:
        return self.role in ("all", "worker")

    def runs_web(self, settings: Settings) -> bool:
        return self.role in ("all", "web") or (self.role == "bot" and settings.bot_mode == "webhook")

    def _push(self, name: str, cleanup: Cleanup) -> None:
        self._components.append(_Component(name, cleanup))

    # --- startup ---------------------------------------------------------------------------------
    async def start(self) -> AppContext:
        try:
            return await self._start()
        except BaseException:
            await self.stop()
            raise

    async def _start(self) -> AppContext:
        env = self.env_settings
        from app.core.plugins.ownership import CoreOwnership

        core_ownership = CoreOwnership(Path(env.core_state_db))
        if self.runs_jobs:  # only the process that applies plugin state owns it; web/bot processes just read
            core_ownership.acquire()

        async def release_core_ownership() -> None:
            core_ownership.release()

        self._push("core ownership", release_core_ownership)
        log.info("starting Netoryx %s (role=%s)", __version__, self.role)
        if env.dev_mode:
            log.warning("DEV_MODE is on: test payments are enabled. Never use it in production.")

        engine = create_engine(env.database_url)
        self._push("database", engine.dispose)
        if env.auto_migrate:
            revision = await upgrade_database(engine)
            log.info("database schema at revision %s", revision)
        else:
            current, head = await migration_status(engine)
            if current != head:
                log.warning("database schema is at %s, latest is %s: run `python -m app migrate`", current, head)
        session_factory = create_session_factory(engine)

        secret_box = SecretBox(await kv.get_or_create_secret(session_factory, env))
        provider = SettingsProvider(env, session_factory, secret_box)
        await provider.load()
        ui.configure_single_screen(lambda: provider.current().bot_single_screen)

        http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self._push("http", http.close)
        await self._apply_dev_tunnel(provider, http)
        settings = provider.current()
        from app.core.plugins import Capability, CoreRuntime, ServiceRegistry
        from app.core.plugins.backups import BACKUP_SERVICE, BackupOperations
        from app.core.plugins.scheduler import SCHEDULER_CONTRIBUTIONS, SchedulerContributions

        plugin_services = ServiceRegistry()
        plugin_scheduler = SchedulerContributions()
        plugin_services.register(Capability(SCHEDULER_CONTRIBUTIONS, "runtime"), plugin_scheduler)
        backup_operations = BackupOperations(engine=engine, settings_provider=provider)
        plugin_services.register(Capability(BACKUP_SERVICE, "runtime"), backup_operations)
        plugin_core = CoreRuntime(Path(settings.core_state_db), services=plugin_services)

        async def close_plugin_core() -> None:
            plugin_core.close()

        self._push("plugin core", close_plugin_core)
        if settings.dev_mode and settings.bot_mode == "webhook":
            host = (settings.public_base_url or "").split("://")[-1]
            if not host.endswith(".trycloudflare.com"):
                log.error("DEV_MODE with webhook mode on %s: test payments are reachable publicly!", host)

        redis = create_redis(settings.redis_url)
        if redis is not None:
            self._push("redis", redis.aclose)
        locks = LockManager(session_factory, redis)
        set_default_manager(locks)

        panels = PanelRegistry(session_factory, settings, secret_box)
        self._push("panels", panels.close)

        modules = ModuleRegistry(self.factories.modules())
        for module in modules:
            register_settings(module.settings)

        bot = self.factories.bot(settings)
        self._push("bot session", bot.session.close)
        me = await bot.get_me()
        bot_username = me.username or ""

        storage = create_storage(redis)
        self._push("fsm storage", storage.close)
        dispatcher = Dispatcher(storage=storage)

        i18n = await self.factories.i18n(session_factory)
        if hasattr(i18n, "default_lang"):
            i18n.default_lang = settings.default_language

            async def sync_language(current: Settings) -> None:
                i18n.default_lang = current.default_language

            provider.subscribe(sync_language)
        maintenance = MaintenanceService(session_factory)

        gateways = GatewayRegistry(
            provider,
            return_url=payment_return_url(settings, bot_username),
            session_factory=session_factory,
        )
        await gateways.rebuild()
        self._push("payment gateways", gateways.close)
        provider.subscribe(gateways.on_settings_changed)

        webhook_secret = await get_webhook_secret(session_factory, settings) if settings.bot_mode == "webhook" else None
        ctx = AppContext(
            settings_provider=provider,
            session_factory=session_factory,
            engine=engine,
            secret_box=secret_box,
            panels=panels,
            gateways=gateways,
            bot=bot,
            dispatcher=dispatcher,
            i18n=i18n,
            http=http,
            redis=redis,
            bot_username=bot_username,
            started_at=datetime.now(UTC),
            modules=modules,
            maintenance=maintenance,
            locks=locks,
            role=self.role or "all",
            webhook_secret=webhook_secret,
            log_buffer=self.log_buffer,
            plugin_core=plugin_core,
        )
        ctx.scheduler = Scheduler(ctx)
        self.ctx = ctx
        dispatcher["ctx"] = ctx
        self._push("admin notifications", lambda: notifications.close(ctx))

        await self._seed(ctx)
        middlewares = await self.factories.middlewares(dispatcher, ctx)
        self._replayer = getattr(middlewares, "replayer", None)
        self._guards = include_module_routers(dispatcher, modules)
        await self._run_hooks(ctx, startup=True)
        self._push("module hooks", lambda: self._run_hooks(ctx, startup=False))

        if self.runs_web(settings):
            await self._start_web(ctx)
        if self.runs_jobs:
            ctx.scheduler.register_modules(modules)
            plugin_scheduler.bind(ctx.scheduler)
            backup_operations.runtime_context = ctx
            await plugin_core.reconcile_runtime()
            await ctx.scheduler.start()
            self._push("scheduler", lambda: ctx.scheduler.shutdown(wait_sec=settings.shutdown_timeout_sec / 2))
        if self.runs_bot or (settings.bot_mode == "webhook" and self.runs_web(settings)):
            await setup_bot_ui(ctx)
        if settings.bot_mode == "webhook" and self.runs_web(settings):
            await self._set_webhook(ctx, webhook_secret or "")
        elif settings.bot_mode == "polling" and self.runs_bot:
            await self._start_polling(ctx)

        self._spawn("settings poller", self._poll_settings(provider, i18n))
        log.info(
            "started: @%s, legacy modules: %s, plugins: %s",
            bot_username,
            ", ".join(m.name for m in modules.enabled(settings)),
            ", ".join(item.identifier for item in plugin_core.registry.available()),
        )
        return ctx

    async def _apply_dev_tunnel(self, provider: SettingsProvider, http: aiohttp.ClientSession) -> None:
        settings = provider.current()
        if not (settings.dev_mode and not settings.public_base_url and settings.dev_tunnel_metrics_url):
            return
        url = await discover_tunnel_url(http, settings.dev_tunnel_metrics_url)
        if url:
            provider.set_ephemeral("public_base_url", url)
            log.warning("development tunnel: %s", url)
        else:
            log.warning("development tunnel URL not found at %s", settings.dev_tunnel_metrics_url)

    async def _seed(self, ctx: AppContext) -> None:
        settings = ctx.settings
        async with ctx.locks.job_lock(SEED_LOCK, 300) as acquired:
            if acquired and settings.app_mode == "shop":
                async with ctx.session_factory.begin() as session:
                    await plans.seed_defaults(session, settings)
        # Independent of the seed lock (another process may have won it): the bootstrap has its own lock and
        # does nothing once panels exist.
        if self.runs_jobs and (settings.xui_host or settings.remnawave_url):
            self._spawn("panel bootstrap", self._bootstrap_panels(ctx))

    async def _bootstrap_panels(self, ctx: AppContext) -> None:
        async with ctx.locks.job_lock("startup:panels", 600) as acquired:
            if not acquired:
                return
            try:
                result = await inventory.bootstrap_from_env(
                    ctx.session_factory, ctx.settings, ctx.secret_box, ctx.panels
                )
            except Exception:
                log.exception("panel bootstrap from environment failed")
                return
        if result is not None and result.errors:
            log.warning("panel bootstrap finished with errors: %s", result.errors)

    async def _run_hooks(self, ctx: AppContext, *, startup: bool) -> None:
        modules = list(ctx.modules) if startup else list(reversed(list(ctx.modules)))
        for module in modules:
            for hook in module.on_startup if startup else module.on_shutdown:
                try:
                    await hook(ctx)
                except Exception:
                    if startup:
                        raise
                    log.exception("module %s: shutdown hook failed", module.name)

    async def _start_web(self, ctx: AppContext) -> None:
        app = self.factories.web_app(ctx)
        state = getattr(app, "state", None)
        if state is not None and getattr(state, "ctx", None) is None:
            state.ctx = ctx
        self.web = _UvicornServer(app, ctx.settings)
        await self.web.start()
        self._push("web server", self.web.stop)
        assert self.web.task is not None
        self._tasks["web server"] = self.web.task
        log.info("web server listening on %s:%s", ctx.settings.web_host, self.web.port)

    async def _set_webhook(self, ctx: AppContext, secret: str) -> None:
        base = ctx.settings.public_base_url or ""
        await ctx.bot.set_webhook(
            url=base + WEBHOOK_PATH,
            secret_token=secret,
            allowed_updates=ctx.dispatcher.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        log.info("Telegram webhook set to %s%s", base, WEBHOOK_PATH)

    async def _start_polling(self, ctx: AppContext) -> None:
        await ctx.bot.delete_webhook(drop_pending_updates=False)
        dispatcher = ctx.dispatcher
        task = asyncio.create_task(
            dispatcher.start_polling(
                ctx.bot,
                handle_signals=False,
                close_bot_session=False,
                allowed_updates=dispatcher.resolve_used_update_types(),
            ),
            name="polling",
        )
        self._tasks["polling"] = task

        async def stop_polling() -> None:
            if not task.done():
                with contextlib.suppress(RuntimeError):
                    await dispatcher.stop_polling()
                await asyncio.gather(task, return_exceptions=True)
            # Telegram already considers fetched updates delivered: let the handlers finish before the components
            # they use (database, panels, HTTP sessions) are stopped.
            await self._drain_updates(dispatcher, min(UPDATE_DRAIN_SEC, ctx.settings.shutdown_timeout_sec * 0.75))

        self._push("polling", stop_polling)

    async def _drain_updates(self, dispatcher: Dispatcher, grace_sec: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_sec
        while True:
            pending: set[asyncio.Task[Any]] = set(getattr(dispatcher, "_handle_update_tasks", ()))
            if self._replayer is not None:
                pending |= set(self._replayer.pending)
            pending = {t for t in pending if not t.done()}
            if not pending:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.warning("shutdown: %d updates are still being handled and will be interrupted", len(pending))
                return
            await asyncio.wait(pending, timeout=remaining)

    def _spawn(self, name: str, coro: Awaitable[Any]) -> None:
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        self._tasks[name] = task

        async def cancel() -> None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self._push(name, cancel)

    async def _poll_settings(self, provider: SettingsProvider, i18n: Any) -> None:
        """Pick up settings and text changes made by other processes (web admin in another container etc.)."""
        poll_texts = getattr(i18n, "poll_changes", None)
        while True:
            await asyncio.sleep(SETTINGS_POLL_INTERVAL_SEC)
            try:
                await provider.poll_changes()
            except Exception:
                log.exception("settings reload failed")
            if poll_texts is not None:
                try:
                    await poll_texts()
                except Exception:
                    log.exception("texts reload failed")

    # --- running / stopping ----------------------------------------------------------------------
    def request_stop(self) -> None:
        self._stop.set()

    async def wait(self) -> int:
        """Block until a stop is requested (0) or a vital component dies (1)."""
        vital = [t for name, t in self._tasks.items() if name in ("polling", "web server")]
        stop_waiter = asyncio.ensure_future(self._stop.wait())
        try:
            done, _ = await asyncio.wait([stop_waiter, *vital], return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_waiter.cancel()
        if stop_waiter in done:
            return 0
        for task in done:
            name = next((n for n, t in self._tasks.items() if t is task), "component")
            exc = None if task.cancelled() else task.exception()
            log.error("%s stopped unexpectedly: %r", name, exc)
        return 1

    async def stop(self) -> None:
        timeout = self.env_settings.shutdown_timeout_sec
        while self._components:
            component = self._components.pop()
            try:
                await asyncio.wait_for(component.cleanup(), timeout=timeout)
            except TimeoutError:
                log.error("timed out stopping %s", component.name)
            except Exception:
                log.exception("error while stopping %s", component.name)
        if self.ctx is not None and self._guards:
            detach_routers(self.ctx.dispatcher, self._guards)
            self._guards = []
        self._tasks.clear()
        set_default_manager(None)

    async def run(self) -> int:
        """Start, wait for SIGINT/SIGTERM (or a crash) and stop. Returns the process exit code."""
        await self.start()
        restore = install_signal_handlers(self.request_stop)
        try:
            code = await self.wait()
        finally:
            log.info("shutting down")
            await self.stop()
            restore()
        log.info("stopped")
        return code


def install_signal_handlers(callback: Callable[[], None]) -> Callable[[], None]:
    """Call ``callback`` on SIGINT/SIGTERM (SIGBREAK on Windows); returns a function restoring the old handlers."""
    loop = asyncio.get_running_loop()
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    previous: dict[int, Any] = {}

    def handler(signum: int, frame: Any) -> None:
        loop.call_soon_threadsafe(callback)

    for sig in signals:
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):
            continue

    def restore() -> None:
        for sig, old in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, old)

    return restore


# =============================================================================================
# Diagnostics (`python -m app check`)
# =============================================================================================
@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str = ""


async def run_checks(settings: Settings, *, telegram: bool = True) -> list[CheckResult]:
    """Validate configuration, database, migrations, optional dependencies and (optionally) the bot token."""
    results: list[CheckResult] = [CheckResult("config", "ok", f"mode={settings.app_mode}, role={settings.role}")]
    engine = create_engine(settings.database_url)
    try:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            results.append(CheckResult("database", "ok", engine.dialect.name))
        except Exception as exc:
            results.append(CheckResult("database", "fail", f"{type(exc).__name__}: {exc}"))
        else:
            try:
                current, head = await migration_status(engine)
            except Exception as exc:
                results.append(CheckResult("migrations", "fail", f"{type(exc).__name__}: {exc}"))
            else:
                if current == head:
                    results.append(CheckResult("migrations", "ok", f"at {current}"))
                else:
                    status = "warn" if settings.auto_migrate else "fail"
                    results.append(CheckResult("migrations", status, f"database {current}, latest {head}"))
    finally:
        await engine.dispose()

    for enabled, package, name in (
        (bool(settings.redis_url), "redis", "redis"),
        (bool(settings.bot_proxy_url), "aiohttp_socks", "bot proxy"),
        (bool(settings.sentry_dsn), "sentry_sdk", "sentry"),
    ):
        if not enabled:
            continue
        try:
            importlib.import_module(package)
            results.append(CheckResult(name, "ok", f"{package} installed"))
        except ImportError:
            results.append(CheckResult(name, "fail", f"python package {package} is not installed"))

    if not settings.public_base_url:
        results.append(
            CheckResult("public url", "warn", "PUBLIC_BASE_URL is empty: subscription links and webhooks are off")
        )
    if settings.dev_mode:
        results.append(CheckResult("dev mode", "warn", "DEV_MODE is on (test payments)"))
    if not settings.admin_ids:
        results.append(CheckResult("admins", "warn", "ADMIN_IDS is empty: nobody can open the admin panel"))

    if telegram:
        try:
            bot = create_bot(settings)
        except Exception as exc:
            results.append(CheckResult("telegram", "fail", str(exc)))
        else:
            try:
                me = await bot.get_me()
                results.append(CheckResult("telegram", "ok", f"@{me.username}"))
            except Exception as exc:
                results.append(CheckResult("telegram", "fail", f"{type(exc).__name__}: {exc}"))
            finally:
                await bot.session.close()
    return results
