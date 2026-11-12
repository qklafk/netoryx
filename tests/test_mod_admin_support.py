"""Harness for the bot admin module tests: the real middlewares and admin routers behind a recording Telegram API."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update

from app.config import Settings
from app.core.i18n import LOCALES_DIR, MODULES_DIR, I18n
from app.core.modules import FeatureModule, ModuleRegistry, load_modules
from app.db.models import Admin, AdminRole, Subscription, SubscriptionStatus, User
from app.db.session import SessionFactory
from app.middlewares import Middlewares, setup_middlewares
from app.modules.admin.common import reset_once
from app.runtime import detach_routers, include_module_routers
from app.services.maintenance import MaintenanceService
from app.services.panels.base import PanelBackend
from app.utils.crypto import SecretBox
from tests.fakes import FakePanel
from tests.test_middlewares_support import FakeSession, make_bot, make_test_settings, tg_user

OWNER = 1000  # ADMIN_IDS
USER = 5001
ADMIN_MODULES = (
    "admin",
    "admin_dashboard",
    "admin_users",
    "admin_subscriptions",
    "admin_plans",
    "admin_payments",
    "admin_promo",
    "admin_withdrawals",
    "admin_campaigns",
)

_ids = iter(range(10_000, 10_000_000))


def make_i18n(tmp_path: Path) -> I18n:
    """Core texts plus the locale files of the admin modules only."""
    modules = tmp_path / "modules"
    for name in ADMIN_MODULES:
        source = MODULES_DIR / name / "locales"
        if source.is_dir():
            shutil.copytree(source, modules / name / "locales", dirs_exist_ok=True)
    i18n = I18n(None, locales_dir=LOCALES_DIR, modules_dir=modules)
    i18n.load_files()
    return i18n


class Registry:
    """Panel registry stand-in: backends by panel id."""

    def __init__(self) -> None:
        self.backends: dict[int, PanelBackend] = {}

    async def get(self, panel_id: int) -> PanelBackend:
        try:
            return self.backends[panel_id]
        except KeyError:
            raise LookupError(panel_id) from None


class Gateways:
    def __init__(self) -> None:
        self.items: dict[str, Any] = {}

    def get(self, key: str) -> Any:
        return self.items.get(key)

    def enabled(self) -> list[Any]:
        return list(self.items.values())

    def as_dict(self) -> dict[str, Any]:
        return dict(self.items)


@dataclass(eq=False)
class AdminCtx:
    current: Settings
    session_factory: SessionFactory
    bot: Bot
    i18n: I18n
    dispatcher: Dispatcher
    maintenance: MaintenanceService
    modules: ModuleRegistry
    panels: Registry = field(default_factory=Registry)
    gateways: Gateways = field(default_factory=Gateways)
    redis: Any = None
    locks: Any = None
    bot_username: str = "test_bot"
    credited: list[Any] = field(default_factory=list)
    secret_box: SecretBox = field(default_factory=lambda: SecretBox("test-secret"))

    @property
    def settings(self) -> Settings:
        return self.current

    async def on_payment_credited(self, result: Any) -> None:
        self.credited.append(result)


class Harness:
    def __init__(self, ctx: AdminCtx, api: FakeSession, mw: Middlewares, guards: list[Router]) -> None:
        self.ctx = ctx
        self.api = api
        self.mw = mw
        self.guards = guards

    def close(self) -> None:
        """Detach the (module-level) routers so the next test can include them again."""
        detach_routers(self.ctx.dispatcher, self.guards)

    @property
    def sf(self) -> SessionFactory:
        return self.ctx.session_factory

    async def feed(self, update: Update) -> Any:
        result = await self.ctx.dispatcher.feed_update(self.ctx.bot, update)
        await self.mw.replayer.wait()
        return result

    async def message(self, text: str, uid: int = OWNER, **kw: Any) -> Any:
        return await self.feed(message(uid, text, **kw))

    async def press(self, data: str, uid: int = OWNER) -> Any:
        return await self.feed(callback(uid, data))

    def calls(self, method: type[TelegramMethod[Any]]) -> list[Any]:
        return self.api.of(method)

    def last_text(self) -> str:
        """Text of the last screen sent or edited."""
        for call in reversed(self.api.calls):
            if isinstance(call, SendMessage | EditMessageText):
                return str(call.text)
        raise AssertionError("no screen was shown")

    def last_markup(self) -> Any:
        for call in reversed(self.api.calls):
            if isinstance(call, SendMessage | EditMessageText):
                return call.reply_markup
        raise AssertionError("no screen was shown")

    def buttons(self) -> dict[str, str]:
        """Button text -> callback data (or URL) of the last screen."""
        markup = self.last_markup()
        result: dict[str, str] = {}
        if markup is None:
            return result
        for row in markup.inline_keyboard:
            for button in row:
                result[button.text] = button.callback_data or button.url or ""
        return result

    def popups(self) -> list[str | None]:
        return [c.text for c in self.api.of(AnswerCallbackQuery)]

    def reset(self) -> None:
        self.api.calls.clear()


def build(
    session_factory: SessionFactory, tmp_path: Path, *, modules: list[FeatureModule] | None = None, **settings: Any
) -> Harness:
    bot, api = make_bot()
    registry = ModuleRegistry(modules if modules is not None else load_modules(ADMIN_MODULES))
    dispatcher = Dispatcher(storage=MemoryStorage())
    ctx = AdminCtx(
        current=make_test_settings(**settings),
        session_factory=session_factory,
        bot=bot,
        i18n=make_i18n(tmp_path),
        dispatcher=dispatcher,
        maintenance=MaintenanceService(session_factory, cache_ttl=0.0),
        modules=registry,
    )
    mw = setup_middlewares(dispatcher, ctx)  # type: ignore[arg-type]
    guards = include_module_routers(dispatcher, registry)
    return Harness(ctx, api, mw, guards)


class Builder:
    """Fixture helper: builds harnesses and detaches their routers at teardown."""

    def __init__(self, session_factory: SessionFactory, tmp_path: Path) -> None:
        self.session_factory = session_factory
        self.tmp_path = tmp_path
        self.built: list[Harness] = []
        reset_once()

    def __call__(self, *, modules: list[FeatureModule] | None = None, **settings: Any) -> Harness:
        for harness in self.built:  # one dispatcher at a time owns the routers
            harness.close()
        harness = build(self.session_factory, self.tmp_path, modules=modules, **settings)
        self.built.append(harness)
        return harness

    def close(self) -> None:
        for harness in self.built:
            harness.close()


# --- updates ---------------------------------------------------------------------------------------------------------


def message(uid: int, text: str | None = "hi", **fields: Any) -> Update:
    msg = Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=uid, type="private"),
        from_user=tg_user(uid, first_name=f"U{uid}"),
        text=text,
        **fields,
    )
    return Update(update_id=next(_ids), message=msg)


def callback(uid: int, data: str) -> Update:
    msg = Message(message_id=7, date=datetime.now(UTC), chat=Chat(id=uid, type="private"), text="screen")
    query = CallbackQuery(id=f"cb{next(_ids)}", from_user=tg_user(uid), chat_instance="ci", data=data, message=msg)
    return Update(update_id=next(_ids), callback_query=query)


# --- data ------------------------------------------------------------------------------------------------------------


async def add_user(sf: SessionFactory, uid: int = USER, **fields: Any) -> User:
    values: dict[str, Any] = {
        "tg_id": uid,
        "username": f"user{uid}",
        "first_name": f"User {uid}",
        "referral_code": f"R{uid}",
        "sub_token": f"token-{uid}",
    }
    values.update(fields)
    async with sf.begin() as s:
        user = User(**values)
        s.add(user)
    return user


async def add_subscription(sf: SessionFactory, uid: int = USER, **fields: Any) -> Subscription:
    from datetime import timedelta

    values: dict[str, Any] = {
        "user_id": uid,
        "client_uuid": f"00000000-0000-0000-0000-{uid:012d}",
        "status": SubscriptionStatus.ACTIVE,
        "expires_at": datetime.now(UTC).replace(microsecond=0) + timedelta(days=10),
        "device_limit": 3,
    }
    values.update(fields)
    async with sf.begin() as s:
        sub = Subscription(**values)
        s.add(sub)
    return sub


async def add_admin(sf: SessionFactory, uid: int, role: AdminRole) -> None:
    async with sf.begin() as s:
        s.add(Admin(tg_id=uid, role=role))


def fake_panel() -> FakePanel:
    return FakePanel()
