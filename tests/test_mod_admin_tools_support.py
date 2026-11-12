"""Test harness for the admin and support screens: real i18n, settings provider, maintenance service and module
routers on a Dispatcher, with the Telegram API replaced by a recording fake."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Document, Message, PhotoSize, Update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.core.i18n import I18n
from app.core.modules import FeatureModule, ModuleRegistry
from app.core.settings_provider import SettingsProvider
from app.db.models import AdminRole
from app.db.session import SessionFactory
from app.runtime import detach_routers, include_module_routers
from app.services import users
from app.services.maintenance import MaintenanceService
from app.services.panels.base import PanelBackend
from app.utils.crypto import SecretBox
from tests.test_middlewares_support import FakeSession, make_bot, make_test_settings, tg_user

OWNER = 1000
_ids = iter(range(10_000, 10_000_000))


class FakePanels:
    """Stands in for :class:`app.services.panels.registry.PanelRegistry`."""

    def __init__(self) -> None:
        self.backends: dict[int, PanelBackend] = {}
        self.default: PanelBackend | None = None  # returned for panels without their own backend
        self.invalidated: list[int] = []

    async def get(self, panel_id: int) -> PanelBackend:
        backend = self.backends.get(panel_id, self.default)
        if backend is None:
            raise LookupError(f"panel {panel_id} not found")
        return backend

    async def invalidate(self, panel_id: int) -> None:
        self.invalidated.append(panel_id)

    async def close(self) -> None:
        return None


@dataclass(eq=False)
class Ctx:
    settings_provider: SettingsProvider
    session_factory: SessionFactory
    engine: AsyncEngine
    secret_box: SecretBox
    bot: Bot
    dispatcher: Dispatcher
    i18n: I18n
    modules: ModuleRegistry
    maintenance: MaintenanceService
    panels: FakePanels = field(default_factory=FakePanels)
    redis: Any = None
    locks: Any = None
    bot_username: str = "test_bot"
    role: str = "all"

    @property
    def settings(self) -> Settings:
        return self.settings_provider.current()


@dataclass
class Harness:
    ctx: Ctx
    api: FakeSession
    dp: Dispatcher

    async def feed(self, update: Update, *, role: AdminRole | None = AdminRole.OWNER, lang: str = "ru") -> None:
        settings = self.ctx.settings
        await self.dp.feed_update(
            self.ctx.bot,
            update,
            ctx=self.ctx,
            settings=settings,
            admin_role=role,
            lang=lang,
            _=self.ctx.i18n.translator(lang, settings.zone),
            db_user=None,
            user_created=False,
        )

    async def click(self, data: str, *, uid: int = OWNER, role: AdminRole | None = AdminRole.OWNER) -> None:
        await self.feed(callback(uid, data), role=role)

    async def say(self, text: str, *, uid: int = OWNER, role: AdminRole | None = AdminRole.OWNER) -> Message:
        update = text_message(uid, text)
        await self.feed(update, role=role)
        assert update.message is not None
        return update.message

    # --- what the bot showed ---------------------------------------------------------------------------------------
    def screens(self) -> list[str]:
        """Texts of sent and edited messages, in order."""
        return [c.text or "" for c in self.api.calls if isinstance(c, SendMessage | EditMessageText)]

    def last(self) -> str:
        texts = self.screens()
        assert texts, "the bot showed nothing"
        return texts[-1]

    def last_markup(self) -> Any:
        for call in reversed(self.api.calls):
            if isinstance(call, SendMessage | EditMessageText):
                return call.reply_markup
        raise AssertionError("no screen")

    def buttons(self) -> list[tuple[str, str | None]]:
        markup = self.last_markup()
        if markup is None:
            return []
        return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]

    def button(self, needle: str) -> str:
        """callback_data of the first button on the last screen whose text contains ``needle``."""
        for text, data in self.buttons():
            if needle in text and data:
                return data
        raise AssertionError(f"no button {needle!r} in {[t for t, _ in self.buttons()]}")

    def popups(self) -> list[str | None]:
        return [c.text for c in self.api.calls if isinstance(c, AnswerCallbackQuery)]

    def sent_to(self, chat_id: int) -> list[str]:
        return [c.text for c in self.api.of(SendMessage) if int(c.chat_id) == chat_id]


def callback(uid: int, data: str, *, chat_id: int | None = None) -> Update:
    message = Message(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id if chat_id is not None else uid, type="private"),
        text="screen",
    )
    query = CallbackQuery(id=f"cb{next(_ids)}", from_user=tg_user(uid), chat_instance="ci", data=data, message=message)
    return Update(update_id=next(_ids), callback_query=query)


def text_message(uid: int, text: str, **extra: Any) -> Update:
    message = Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=uid, type="private"),
        from_user=tg_user(uid),
        text=text,
        **extra,
    )
    return Update(update_id=next(_ids), message=message)


def media_message(
    uid: int,
    *,
    photo: str | None = None,
    document: str | None = None,
    caption: str | None = None,
    file_name: str = "file.bin",
    file_size: int = 10,
) -> Update:
    extra: dict[str, Any] = {}
    if photo:
        extra["photo"] = [PhotoSize(file_id=photo, file_unique_id=photo, width=10, height=10)]
    if document:
        extra["document"] = Document(
            file_id=document, file_unique_id=document, file_name=file_name, file_size=file_size
        )
    message = Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=uid, type="private"),
        from_user=tg_user(uid),
        caption=caption,
        **extra,
    )
    return Update(update_id=next(_ids), message=message)


async def make_harness(session_factory: SessionFactory, modules: Iterable[FeatureModule], **settings: Any) -> Harness:
    env = make_test_settings(**settings)
    box = SecretBox("test-secret-key-0123456789abcdef")
    provider = SettingsProvider(env, session_factory, box)
    await provider.load()
    i18n = I18n(session_factory)
    await i18n.load()
    bot, api = make_bot()
    dp = Dispatcher(storage=MemoryStorage())
    engine = session_factory.kw["bind"]
    registry = ModuleRegistry(list(modules))
    ctx = Ctx(
        settings_provider=provider,
        session_factory=session_factory,
        engine=engine,
        secret_box=box,
        bot=bot,
        dispatcher=dp,
        i18n=i18n,
        modules=registry,
        maintenance=MaintenanceService(session_factory, cache_ttl=0.0),
    )
    for item in registry:
        for router in [*item.admin_bot_routers, *item.bot_routers]:
            _detach(router)
    _ATTACHED.append((dp, include_module_routers(dp, registry)))
    return Harness(ctx, api, dp)


_ATTACHED: list[tuple[Dispatcher, list[Router]]] = []


@pytest.fixture(autouse=True)
def release_module_routers() -> Iterator[None]:
    """Detach module routers after each test, so the application can attach them again (other tests boot it)."""
    yield
    while _ATTACHED:
        dispatcher, guards = _ATTACHED.pop()
        detach_routers(dispatcher, guards)


def _detach(router: Router) -> None:
    """Module routers are singletons: take one away from the dispatcher of a previous test."""
    parent = router._parent_router
    if parent is not None:
        parent.sub_routers.remove(router)
        router._parent_router = None


async def add_user(
    session_factory: SessionFactory, settings: Settings, uid: int, *, lang: str = "ru", username: str | None = None
) -> None:
    async with session_factory.begin() as session:
        await users.get_or_create(session, settings, uid, username, "Ann", lang)


def module(name: str) -> FeatureModule:
    import importlib

    found = importlib.import_module(f"app.modules.{name}").module
    assert isinstance(found, FeatureModule)
    return found


def tmp_backup_dir(tmp_path: Path) -> str:
    folder = tmp_path / "backups"
    folder.mkdir(exist_ok=True)
    return str(folder)
