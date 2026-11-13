"""Shared fakes for the middleware, hook and admin tests: a recording Telegram API, a context and update builders."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    GetChatMember,
    GetMe,
    SendDocument,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
    Update,
    User,
)

from app.config import Settings
from app.core.i18n import I18n
from app.db.session import SessionFactory
from app.services.maintenance import MaintenanceService

BOT_ID = 42


def make_test_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "bot_token": "42:TEST",
        "admin_ids": [1000],
        "database_url": "sqlite+aiosqlite:///:memory:",
        "public_base_url": "https://bot.example.com",
        "secret_key": "test-secret-key-0123456789abcdef",
        "brand_name": "TestVPN",
        "backup_enabled": False,
        "admin_notify_mute": [],
        # The Telegram proxies are off by default; the tests of their code turn them on explicitly.
        "tg_proxy_access": "everyone",
        "tg_socks_enabled": True,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


# --- fake Telegram API ------------------------------------------------------------------------------------------------
class FakeSession(BaseSession):
    """Records every Bot API call and answers with plausible results (or a queued exception)."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.errors: dict[type[TelegramMethod[Any]], list[BaseException]] = {}
        self.members: dict[int, str] = {}  # user id -> chat member status for GetChatMember
        self._next_id = 100

    def fail(self, method: type[TelegramMethod[Any]], *errors: BaseException) -> None:
        self.errors.setdefault(method, []).extend(errors)

    def of(self, method: type[TelegramMethod[Any]]) -> list[Any]:
        return [c for c in self.calls if isinstance(c, method)]

    def texts(self) -> list[str]:
        return [c.text for c in self.of(SendMessage)]

    def popups(self) -> list[str | None]:
        return [c.text for c in self.of(AnswerCallbackQuery)]

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:  # noqa: ASYNC109
        self.calls.append(method)
        queued = self.errors.get(type(method))
        if queued:
            raise queued.pop(0)
        if isinstance(method, SendMessage | SendDocument):
            self._next_id += 1
            return Message(
                message_id=self._next_id,
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private" if int(method.chat_id) > 0 else "supergroup"),
                text=getattr(method, "text", None),
            )
        if isinstance(method, GetChatMember):
            user = User(id=method.user_id, is_bot=False, first_name="u")
            if self.members.get(method.user_id) == "member":
                return ChatMemberMember(user=user)
            return ChatMemberLeft(user=user)
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="bot", username="test_bot")
        return True  # answer*/delete/edit methods

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:  # pragma: no cover
        yield b""

    async def close(self) -> None:
        return None


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    return Bot(token="42:TEST", session=session), session


# --- i18n -------------------------------------------------------------------------------------------------------------
TEXTS_RU = """
[fmt]
money = "{amount} ₽"
date = "{day} {month} {year}"
datetime = "{date}, {time}"
[fmt.month]
1 = "января"
2 = "февраля"
3 = "марта"
4 = "апреля"
5 = "мая"
6 = "июня"
7 = "июля"
8 = "августа"
9 = "сентября"
10 = "октября"
11 = "ноября"
12 = "декабря"
[plural.days]
one = "{n} день"
few = "{n} дня"
many = "{n} дней"
[plural.months]
one = "{n} месяц"
few = "{n} месяца"
many = "{n} месяцев"
[plural.years]
one = "{n} год"
few = "{n} года"
many = "{n} лет"
[nav]
menu = "🏠 Главное меню"
[error]
generic = "ERR generic"
alert = "ERR alert"
unavailable = "ERR unavailable"
[access]
banned = "BANNED"
throttled = "SLOW DOWN"
maintenance = "MAINT"
maintenance_eta = "MAINT until {eta}"
maintenance_alert = "MAINT popup"
maintenance_until = "until {eta}"
invite_only = "INVITE ONLY"
invite_invalid = "INVITE INVALID"
[access.channel]
text = "JOIN CHANNEL"
subscribe = "Open channel"
check = "I joined"
not_yet = "NOT YET"
thanks = "THANKS"
[consent]
text = "ACCEPT TERMS"
terms = "Terms"
privacy = "Privacy"
accept = "Accept"
accepted = "ACCEPTED"
required = "Accept first"
[paid]
topped_up = "TOPPED {amount} balance {balance}"
plan_activated = "ACTIVATED {plan} until {expires}"
gift_ready = "GIFT {plan} code {code} link {link}"
donation_thanks = "THANKS {amount}"
plan_failed = "FAILED {plan} {amount} balance {balance}"
partial = "PARTIAL {amount} of {expected} balance {balance}"
[reward]
money = "REWARD {amount} level {level}"
partner = "PARTNER {amount} level {level}"
days = "REWARD DAYS {days} level {level}"
[admin_notify]
payment = "PAYMENT {amount} via {provider} from {user_id} {username}: {what}"
what_topup = "top-up"
what_plan = "plan {plan}"
what_gift = "gift {plan}"
what_donation = "donation"
plan_failed = "plan not bought: {reason}"
channel_check_failed = "cannot check channel {channel_id}"
error = "ERROR {signature} in update {update_id} ({update_type}) from {user_id}: {error} {trace}"
"""


def make_i18n(tmp_path: Path) -> I18n:
    for lang in ("ru", "en"):
        folder = tmp_path / "locales" / lang
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "all.toml").write_text(TEXTS_RU, encoding="utf-8")
    i18n = I18n(None, locales_dir=tmp_path / "locales", modules_dir=None)
    i18n.load_files()
    return i18n


# --- context ----------------------------------------------------------------------------------------------------------
@dataclass(eq=False)
class FakeCtx:
    """The parts of the application context used by middlewares and hooks."""

    current: Settings
    session_factory: SessionFactory
    bot: Bot
    i18n: I18n
    dispatcher: Dispatcher
    maintenance: MaintenanceService
    panels: Any = None
    redis: Any = None
    bot_username: str = "test_bot"

    @property
    def settings(self) -> Settings:
        return self.current


def make_ctx(session_factory: SessionFactory, tmp_path: Path, **settings: Any) -> tuple[FakeCtx, FakeSession]:
    bot, api = make_bot()
    ctx = FakeCtx(
        current=make_test_settings(**settings),
        session_factory=session_factory,
        bot=bot,
        i18n=make_i18n(tmp_path),
        dispatcher=Dispatcher(storage=MemoryStorage()),
        maintenance=MaintenanceService(session_factory, cache_ttl=0.0),
    )
    return ctx, api


# --- updates ----------------------------------------------------------------------------------------------------------
_update_ids = iter(range(1, 10_000_000))


def tg_user(uid: int, username: str | None = None, first_name: str = "Ann", lang: str = "ru") -> User:
    return User(id=uid, is_bot=False, first_name=first_name, username=username, language_code=lang)


def message_update(
    uid: int,
    text: str = "hi",
    *,
    chat_id: int | None = None,
    chat_type: str = "private",
    username: str | None = None,
    first_name: str = "Ann",
) -> Update:
    chat = Chat(id=chat_id if chat_id is not None else uid, type=chat_type)
    message = Message(
        message_id=next(_update_ids),
        date=datetime.now(UTC),
        chat=chat,
        from_user=tg_user(uid, username, first_name),
        text=text,
    )
    return Update(update_id=next(_update_ids), message=message)


def callback_update(uid: int, data: str) -> Update:
    message = Message(message_id=7, date=datetime.now(UTC), chat=Chat(id=uid, type="private"), text="screen")
    query = CallbackQuery(
        id=f"cb{next(_update_ids)}", from_user=tg_user(uid), chat_instance="ci", data=data, message=message
    )
    return Update(update_id=next(_update_ids), callback_query=query)


def pre_checkout_update(uid: int) -> Update:
    query = PreCheckoutQuery(
        id=f"pc{next(_update_ids)}", from_user=tg_user(uid), currency="XTR", total_amount=10, invoice_payload="p"
    )
    return Update(update_id=next(_update_ids), pre_checkout_query=query)


def successful_payment_update(uid: int) -> Update:
    payment = SuccessfulPayment(
        currency="XTR",
        total_amount=10,
        invoice_payload="p",
        telegram_payment_charge_id="charge",
        provider_payment_charge_id="prov",
    )
    message = Message(
        message_id=next(_update_ids),
        date=datetime.now(UTC),
        chat=Chat(id=uid, type="private"),
        from_user=tg_user(uid),
        successful_payment=payment,
    )
    return Update(update_id=next(_update_ids), message=message)


@dataclass
class Recorder:
    """Handler stand-in: remembers the injected data of every update that reached it."""

    seen: list[dict[str, Any]] = field(default_factory=list)
    raise_with: Callable[[], BaseException] | None = None

    async def handle(self, event: Any, **data: Any) -> None:
        self.seen.append(data | {"event": event})
        if self.raise_with is not None:
            raise self.raise_with()
