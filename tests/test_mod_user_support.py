"""Harness for the user-facing modules (main menu, subscription): the real middlewares, routers and texts of these
modules behind a recording Telegram API and an in-memory panel registry."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update

from app.config import Settings
from app.core.i18n import LOCALES_DIR, MODULES_DIR, I18n
from app.core.modules import FeatureModule, ModuleRegistry
from app.db.models import Plan, Subscription, SubscriptionStatus, TxKind, User
from app.db.session import SessionFactory
from app.middlewares import Middlewares, setup_middlewares
from app.modules.core_user import module as core_user_module
from app.modules.core_user.fallback import install_fallback
from app.modules.subscription import module as subscription_module
from app.runtime import detach_routers, include_module_routers
from app.services import notifications, wallet
from app.services.maintenance import MaintenanceService
from app.services.panels.base import PanelBackend
from app.utils.crypto import SecretBox
from tests.test_middlewares_support import FakeSession, make_bot, make_test_settings, tg_user

OWNER = 1000  # ADMIN_IDS
USER = 5001
USER_MODULES = ("core_user", "subscription")

_ids = iter(range(20_000, 10_000_000))


def make_i18n(tmp_path: Path) -> I18n:
    """Core texts plus the locale files of the user modules only."""
    modules = tmp_path / "modules"
    for name in USER_MODULES:
        shutil.copytree(MODULES_DIR / name / "locales", modules / name / "locales", dirs_exist_ok=True)
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


class Scheduler:
    def __init__(self) -> None:
        self.started: list[str] = []

    def is_scheduled(self, job_id: str) -> bool:
        return True

    def run_now(self, job_id: str) -> bool:
        self.started.append(job_id)
        return True


@dataclass(eq=False)
class UserCtx:
    current: Settings
    session_factory: SessionFactory
    bot: Bot
    i18n: I18n
    dispatcher: Dispatcher
    maintenance: MaintenanceService
    modules: ModuleRegistry
    panels: Registry = field(default_factory=Registry)
    gateways: Gateways = field(default_factory=Gateways)
    scheduler: Scheduler = field(default_factory=Scheduler)
    http: Any = None
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
    def __init__(self, ctx: UserCtx, api: FakeSession, mw: Middlewares, guards: list[Router]) -> None:
        self.ctx = ctx
        self.api = api
        self.mw = mw
        self.guards = guards

    @property
    def sf(self) -> SessionFactory:
        return self.ctx.session_factory

    async def close(self) -> None:
        detach_routers(self.ctx.dispatcher, self.guards)
        await notifications.close(self.ctx, drain_seconds=0.5)  # type: ignore[arg-type]

    async def feed(self, update: Update) -> None:
        await self.ctx.dispatcher.feed_update(self.ctx.bot, update)
        await self.mw.replayer.wait()

    async def send(self, text: str, uid: int = USER, *, first_name: str = "Ann", username: str | None = None) -> None:
        await self.feed(message(uid, text, first_name=first_name, username=username))

    async def press(self, data: CallbackData | str, uid: int = USER) -> None:
        await self.feed(callback(uid, data.pack() if isinstance(data, CallbackData) else data))

    # --- what the bot said -----------------------------------------------------------------------------------------
    def screens(self) -> list[tuple[str, InlineKeyboardMarkup | None]]:
        """Every text the bot sent or edited in, with its keyboard, in order."""
        result: list[tuple[str, InlineKeyboardMarkup | None]] = []
        for call in self.api.calls:
            if isinstance(call, SendMessage | EditMessageText):
                result.append((call.text, call.reply_markup))  # type: ignore[arg-type]
            elif isinstance(call, SendPhoto):
                result.append((call.caption or "", call.reply_markup))  # type: ignore[arg-type]
        return result

    def last_text(self) -> str:
        screens = self.screens()
        return screens[-1][0] if screens else ""

    def last_text_to(self, chat_id: int = USER) -> str:
        """The last text sent to one chat; admin notices are delivered in the background and may come later."""
        for call in reversed(self.api.calls):
            if isinstance(call, SendMessage | EditMessageText) and call.chat_id == chat_id:
                return call.text  # type: ignore[return-value]
        return ""

    def last_buttons(self) -> list[Any]:
        screens = self.screens()
        markup = screens[-1][1] if screens else None
        return [button for row in markup.inline_keyboard for button in row] if markup is not None else []

    def button_texts(self) -> list[str]:
        return [button.text for button in self.last_buttons()]

    def button(self, text_part: str) -> Any:
        for button in self.last_buttons():
            if text_part in button.text:
                return button
        raise AssertionError(f"no button with {text_part!r} in {self.button_texts()}")

    def popups(self) -> list[str]:
        return [call.text for call in self.api.of(AnswerCallbackQuery) if call.text]

    def t(self, key: str, lang: str = "ru", **kw: Any) -> str:
        return self.ctx.i18n.t(key, lang, **kw)


def make_harness(
    session_factory: SessionFactory,
    tmp_path: Path,
    *,
    modules: list[FeatureModule] | None = None,
    **settings: Any,
) -> Harness:
    bot, api = make_bot()
    registry = ModuleRegistry(modules or [core_user_module, subscription_module])
    ctx = UserCtx(
        current=make_test_settings(**settings),
        session_factory=session_factory,
        bot=bot,
        i18n=make_i18n(tmp_path),
        dispatcher=Dispatcher(storage=MemoryStorage()),
        maintenance=MaintenanceService(session_factory, cache_ttl=0.0),
        modules=registry,
    )
    ctx.dispatcher["ctx"] = ctx
    mw = setup_middlewares(ctx.dispatcher, ctx)  # type: ignore[arg-type]
    guards = include_module_routers(ctx.dispatcher, registry)
    return Harness(ctx, api, mw, guards)


async def install(harness: Harness) -> None:
    await install_fallback(harness.ctx)  # type: ignore[arg-type]


# --- updates ---------------------------------------------------------------------------------------------------------
def message(uid: int, text: str, *, first_name: str = "Ann", username: str | None = None) -> Update:
    msg = Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=uid, type="private"),
        from_user=tg_user(uid, username, first_name),
        text=text,
    )
    return Update(update_id=next(_ids), message=msg)


def callback(uid: int, data: str) -> Update:
    msg = Message(message_id=7, date=datetime.now(UTC), chat=Chat(id=uid, type="private"), text="screen")
    query = CallbackQuery(id=f"cb{next(_ids)}", from_user=tg_user(uid), chat_instance="ci", data=data, message=msg)
    return Update(update_id=next(_ids), callback_query=query)


# --- data ------------------------------------------------------------------------------------------------------------
async def add_plan(sf: SessionFactory, **values: Any) -> int:
    data: dict[str, Any] = {
        "title": "1 месяц",
        "duration_days": 30,
        "price_kop": 15_000,
        "device_limit": 3,
        "traffic_limit_gb": 0,
        "sort_order": 0,
    }
    data.update(values)
    async with sf.begin() as session:
        plan = Plan(**data)
        session.add(plan)
        await session.flush()
        return plan.id


async def add_subscription(
    sf: SessionFactory, uid: int = USER, *, days: float = 10, plan_id: int | None = None, **values: Any
) -> int:
    data: dict[str, Any] = {
        "user_id": uid,
        "client_uuid": f"00000000-0000-0000-0000-{uid:012d}",
        "status": SubscriptionStatus.ACTIVE,
        "expires_at": (datetime.now(UTC) + timedelta(days=days)).replace(microsecond=0),
        "device_limit": 3,
        "plan_id": plan_id,
    }
    data.update(values)
    async with sf.begin() as session:
        sub = Subscription(**data)
        session.add(sub)
        await session.flush()
        return sub.id


async def get_user(sf: SessionFactory, uid: int = USER) -> User:
    async with sf() as session:
        user = await session.get(User, uid)
        assert user is not None
        return user


async def get_sub(sf: SessionFactory, uid: int = USER) -> Subscription | None:
    from sqlalchemy import select

    async with sf() as session:
        return await session.scalar(select(Subscription).where(Subscription.user_id == uid))


async def top_up(sf: SessionFactory, uid: int, kop: int) -> None:
    async with sf.begin() as session:
        await wallet.apply(session, uid, kop, TxKind.ADMIN_ADJUST, f"test-{next(_ids)}")
