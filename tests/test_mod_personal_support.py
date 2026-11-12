"""Shared harness for the invites, members, servers, cost sharing, Telegram proxy and public web module tests.

A context with the real middlewares, real i18n (core texts + the tested modules' locale files) and a recording
Telegram API; module routers are attached behind their module switches exactly as at runtime.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage, SendPhoto
from aiogram.types import InlineKeyboardMarkup

from app.config import Settings
from app.core.i18n import I18n
from app.core.modules import FeatureModule, ModuleRegistry
from app.db.models import User
from app.db.session import SessionFactory
from app.middlewares import Middlewares, setup_middlewares
from app.runtime import detach_routers, include_module_routers
from app.services.maintenance import MaintenanceService
from app.utils.crypto import SecretBox
from tests.test_middlewares_support import FakeSession, make_bot, make_test_settings

APP_DIR = Path(__file__).resolve().parents[1] / "app"
OWNER = 1000
BOX = SecretBox("test-secret-key-0123456789abcdef")


@dataclass
class StubRegistry:
    """Panel registry stand-in: ``panels[id]`` -> backend; ``broken`` ids raise LookupError."""

    panels: dict[int, Any] = field(default_factory=dict)
    broken: set[int] = field(default_factory=set)
    invalidated: list[int] = field(default_factory=list)

    async def get(self, panel_id: int) -> Any:
        if panel_id in self.broken or panel_id not in self.panels:
            raise LookupError(f"panel {panel_id} is not available")
        return self.panels[panel_id]

    async def invalidate(self, panel_id: int) -> None:
        self.invalidated.append(panel_id)


@dataclass(eq=False)
class ModCtx:
    """The parts of :class:`app.core.context.AppContext` the modules use."""

    current: Settings
    session_factory: SessionFactory
    bot: Any
    i18n: I18n
    dispatcher: Dispatcher
    maintenance: MaintenanceService
    modules: ModuleRegistry
    panels: StubRegistry = field(default_factory=StubRegistry)
    secret_box: SecretBox = BOX
    http: Any = None
    redis: Any = None
    engine: Any = None
    bot_username: str = "test_bot"
    role: str = "all"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def settings(self) -> Settings:
        return self.current


def module_i18n(tmp_path: Path, names: Iterable[str]) -> I18n:
    """Core texts plus the locale files of the given modules only (other modules may be work in progress)."""
    modules_dir = tmp_path / "i18n_modules"
    for name in names:
        source = APP_DIR / "modules" / name / "locales"
        if source.is_dir():
            shutil.copytree(source, modules_dir / name / "locales", dirs_exist_ok=True)
    i18n = I18n(None, locales_dir=APP_DIR / "locales", modules_dir=modules_dir)
    i18n.load_files()
    return i18n


class Harness:
    def __init__(self, ctx: ModCtx, api: FakeSession) -> None:
        self.ctx = ctx
        self.api = api
        self.guards: list[Router] = include_module_routers(ctx.dispatcher, ctx.modules)
        self.mw: Middlewares = setup_middlewares(ctx.dispatcher, ctx)  # type: ignore[arg-type]

    async def feed(self, update: Any) -> Any:
        result = await self.ctx.dispatcher.feed_update(self.ctx.bot, update)
        await self.mw.replayer.wait()
        return result

    def close(self) -> None:
        detach_routers(self.ctx.dispatcher, self.guards)

    # --- what the user saw -------------------------------------------------------------------------
    def last_text(self) -> str:
        """Text of the last screen (sent, edited or photo caption)."""
        for call in reversed(self.api.calls):
            text = getattr(call, "text", None) or getattr(call, "caption", None)
            if isinstance(text, str) and type(call).__name__ in ("SendMessage", "EditMessageText", "SendPhoto"):
                return text
        raise AssertionError("no screen was shown")

    def last_markup(self) -> InlineKeyboardMarkup:
        for call in reversed(self.api.calls):
            markup = getattr(call, "reply_markup", None)
            if isinstance(markup, InlineKeyboardMarkup):
                return markup
        raise AssertionError("no keyboard was shown")

    def buttons(self) -> list[Any]:
        return [button for row in self.last_markup().inline_keyboard for button in row]

    def button_data(self) -> list[str]:
        return [b.callback_data for b in self.buttons() if b.callback_data]

    def find(self, prefix: str) -> str:
        """``callback_data`` of the first button whose data starts with ``prefix``."""
        for data in self.button_data():
            if data.startswith(prefix):
                return data
        raise AssertionError(f"no button {prefix!r} in {self.button_data()}")

    def sent_to(self, chat_id: int) -> list[str]:
        return [c.text for c in self.api.of(SendMessage) if int(c.chat_id) == chat_id]

    def photos(self) -> list[Any]:
        return self.api.of(SendPhoto)

    def popups(self) -> list[str | None]:
        return self.api.popups()


def make_ctx(
    session_factory: SessionFactory,
    tmp_path: Path,
    modules: Iterable[FeatureModule],
    **settings: Any,
) -> tuple[ModCtx, FakeSession]:
    modules = list(modules)
    settings.setdefault("throttle_rate_limit", 10_000)
    bot, api = make_bot()
    ctx = ModCtx(
        current=make_test_settings(**settings),
        session_factory=session_factory,
        bot=bot,
        i18n=module_i18n(tmp_path, [m.name for m in modules]),
        dispatcher=Dispatcher(storage=MemoryStorage()),
        maintenance=MaintenanceService(session_factory, cache_ttl=0.0),
        modules=ModuleRegistry(modules),
    )
    return ctx, api


@asynccontextmanager
async def harnesses(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Callable[..., Harness]]:
    """``factory(*modules, **settings) -> Harness``; every harness is detached on exit.

    Test modules wrap it in a ``harness_factory`` fixture.
    """
    made: list[Harness] = []

    def factory(*modules: FeatureModule, **settings: Any) -> Harness:
        ctx, api = make_ctx(session_factory, tmp_path, modules, **settings)
        harness = Harness(ctx, api)
        made.append(harness)
        return harness

    try:
        yield factory
    finally:
        for harness in made:
            harness.close()


async def add_user(session_factory: SessionFactory, tg_id: int, **fields: Any) -> User:
    fields.setdefault("first_name", f"User{tg_id}")
    async with session_factory.begin() as session:
        user = User(tg_id=tg_id, referral_code=f"r{tg_id}", sub_token=f"tok{tg_id}", **fields)
        session.add(user)
    return user
