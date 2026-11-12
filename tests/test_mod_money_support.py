"""Shared harness for the money module tests: a real dispatcher with the middleware stack and the wallet, payments,
referral, promo and gifts routers, real texts, fake payment gateways and a recording Telegram API."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import CreateInvoiceLink, EditMessageText, SendMessage, SendPhoto, TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    PreCheckoutQuery,
    RefundedPayment,
    SuccessfulPayment,
    Update,
)

from app.config import Settings
from app.core.i18n import LOCALES_DIR, MODULES_DIR, I18n
from app.core.modules import FeatureModule, ModuleRegistry, load_modules
from app.db.models import LedgerAccount, Payment, PaymentStatus, TxKind, User
from app.db.session import SessionFactory
from app.middlewares import Middlewares, setup_middlewares
from app.modules.payments import confirm
from app.runtime import detach_routers, include_module_routers
from app.services import hooks, notifications, provisioning, users, wallet
from app.services.maintenance import MaintenanceService
from app.services.payments.base import Invoice, ProviderStatus, WebhookRejected, WebhookResult
from app.services.payments.service import CreditResult
from app.services.payments.stars import StarsGateway
from tests.test_middlewares_support import FakeSession, make_test_settings, tg_user

MONEY_MODULES = ("wallet", "payments", "referral", "promo", "gifts")
BOT_ID = 42


class MoneyApi(FakeSession):
    """Recording Bot API that also answers invoice links and photos."""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, CreateInvoiceLink):
            self.calls.append(method)
            return "https://t.me/$invoice-link"
        if isinstance(method, SendPhoto):
            self.calls.append(method)
            self._next_id += 1
            return Message(
                message_id=self._next_id, date=datetime.now(UTC), chat=Chat(id=int(method.chat_id), type="private")
            )
        return await super().make_request(bot, method, timeout)

    def screens(self) -> list[str]:
        """Texts of sent and edited messages, in order."""
        return [c.text or "" for c in self.calls if isinstance(c, SendMessage | EditMessageText)]

    def last_markup(self) -> InlineKeyboardMarkup:
        for call in reversed(self.calls):
            markup = getattr(call, "reply_markup", None)
            if isinstance(markup, InlineKeyboardMarkup):
                return markup
        raise AssertionError("no keyboard was sent")


def buttons(markup: InlineKeyboardMarkup) -> list[Any]:
    return [button for row in markup.inline_keyboard for button in row]


def callback_datas(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data for b in buttons(markup) if b.callback_data]


# --- gateways ---------------------------------------------------------------------------------------------------------
@dataclass(eq=False)
class FakeGateway:
    """A provider with a payment page; ``status`` is what polling returns."""

    key: str = "cryptobot"
    title: str = "Fake provider"
    supports_polling: bool = True
    min_amount_kop: int = 100
    status: PaymentStatus = PaymentStatus.PENDING
    created: list[str] = field(default_factory=list)
    webhook: WebhookResult | Exception | None = None
    webhook_query_fields: bool = False

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        self.created.append(description)
        return Invoice(f"prov-{payment.id[:8]}", f"https://pay.example.com/{payment.id}", "1", "RUB")

    async def fetch(self, payment: Payment) -> ProviderStatus:
        return ProviderStatus(self.status, provider_payment_id=payment.provider_payment_id)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if isinstance(self.webhook, Exception):
            raise self.webhook
        if self.webhook is None:
            raise WebhookRejected("bad signature")
        return self.webhook

    async def close(self) -> None:
        return None


class FakeGateways:
    def __init__(self, gateways: list[Any]) -> None:
        self._gateways = {g.key: g for g in gateways}

    def get(self, key: str) -> Any:
        return self._gateways.get(key)

    def enabled(self) -> list[Any]:
        return list(self._gateways.values())

    def as_dict(self) -> dict[str, Any]:
        return dict(self._gateways)


# --- context ----------------------------------------------------------------------------------------------------------
@dataclass(eq=False)
class MoneyCtx:
    current: Settings
    session_factory: SessionFactory
    bot: Bot
    i18n: I18n
    dispatcher: Dispatcher
    maintenance: MaintenanceService
    modules: ModuleRegistry
    gateways: Any
    panels: Any = None
    redis: Any = None
    bot_username: str = "test_bot"
    run_hook: bool = True
    credited: list[CreditResult] = field(default_factory=list)

    @property
    def settings(self) -> Settings:
        return self.current

    async def on_payment_credited(self, result: CreditResult) -> None:
        self.credited.append(result)
        if self.run_hook:
            await hooks.on_payment_credited(self, result)  # type: ignore[arg-type]


def money_i18n(tmp_path: Path) -> I18n:
    """Real core texts plus the money modules' texts (other modules are left out)."""
    modules_dir = tmp_path / "modules"
    for name in MONEY_MODULES:
        shutil.copytree(MODULES_DIR / name / "locales", modules_dir / name / "locales")
    i18n = I18n(None, locales_dir=LOCALES_DIR, modules_dir=modules_dir)
    i18n.load_files()
    return i18n


def money_modules() -> list[FeatureModule]:
    return load_modules(MONEY_MODULES)


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "throttle_rate_limit": 1000,
        "stars_enabled": True,
        "referral_invitee_bonus_rub": 0,
        "referral_inviter_bonus_rub": 0,
    }
    return make_test_settings(**(base | overrides))


@dataclass(eq=False)
class Harness:
    ctx: MoneyCtx
    api: MoneyApi
    mw: Middlewares
    gateway: FakeGateway
    guards: list[Router] = field(default_factory=list)
    admin_notes: list[tuple[str, str]] = field(default_factory=list)
    synced: list[int] = field(default_factory=list)

    async def feed(self, update: Update) -> Any:
        result = await self.ctx.dispatcher.feed_update(self.ctx.bot, update)
        await self.mw.replayer.wait()
        return result

    async def press(self, uid: int, data: str) -> Any:
        return await self.feed(callback_update(uid, data))

    async def say(self, uid: int, text: str) -> Any:
        return await self.feed(message_update(uid, text))

    def set(self, **overrides: Any) -> None:
        self.ctx.current = self.ctx.current.model_copy(update=overrides)


def make_harness(
    session_factory: SessionFactory,
    tmp_path: Path,
    *,
    gateways: list[Any] | None = None,
    **settings: Any,
) -> Harness:
    api = MoneyApi()
    bot = Bot(token="42:TEST", session=api)
    current = make_settings(**settings)
    gateway = FakeGateway()
    ctx = MoneyCtx(
        current=current,
        session_factory=session_factory,
        bot=bot,
        i18n=money_i18n(tmp_path),
        dispatcher=Dispatcher(storage=MemoryStorage()),
        maintenance=MaintenanceService(session_factory, cache_ttl=0.0),
        modules=ModuleRegistry(money_modules()),
        gateways=FakeGateways(gateways if gateways is not None else [gateway, StarsGateway(current)]),
    )
    mw = setup_middlewares(ctx.dispatcher, ctx)  # type: ignore[arg-type]
    guards = include_module_routers(ctx.dispatcher, ctx.modules)
    confirm.install(ctx.dispatcher)
    return Harness(ctx, api, mw, gateway, guards)


@pytest.fixture
def money(
    session_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Harness]]:
    """``money(**settings)`` builds a harness; admin notices and panel syncs are recorded instead of sent."""
    made: list[Harness] = []
    notes: list[tuple[str, str]] = []
    synced: list[int] = []

    def notify(ctx: Any, event: Any, text: str, **kwargs: Any) -> bool:
        notes.append((str(event), text))
        return True

    async def sync(session_factory: Any, registry: Any, settings: Any, sub_id: int, **kwargs: Any) -> Any:
        synced.append(sub_id)
        return provisioning.SyncResult(subscription_id=sub_id)

    monkeypatch.setattr(notifications, "notify", notify)
    monkeypatch.setattr(provisioning, "sync_subscription", sync)

    def factory(*, gateways: list[Any] | None = None, **settings: Any) -> Harness:
        harness = make_harness(session_factory, tmp_path / f"h{len(made)}", gateways=gateways, **settings)
        harness.admin_notes = notes
        harness.synced = synced
        made.append(harness)
        return harness

    yield factory
    for harness in made:  # module routers are module-level objects: release them for the next dispatcher
        detach_routers(harness.ctx.dispatcher, harness.guards)


# --- data -------------------------------------------------------------------------------------------------------------
async def register(ctx: MoneyCtx, uid: int, *, balance_kop: int = 0, referral_kop: int = 0) -> User:
    async with ctx.session_factory.begin() as session:
        user, _ = await users.get_or_create(session, ctx.settings, uid, f"user{uid}", "Ann", "ru")
        if balance_kop:
            await wallet.apply(session, uid, balance_kop, TxKind.ADMIN_ADJUST, f"seed:{uuid.uuid4().hex}")
        if referral_kop:
            await wallet.apply(
                session,
                uid,
                referral_kop,
                TxKind.REFERRAL_COMMISSION,
                f"seed:{uuid.uuid4().hex}",
                account=LedgerAccount.REFERRAL,
            )
        return user


async def balance(ctx: MoneyCtx, uid: int) -> tuple[int, int]:
    async with ctx.session_factory() as session:
        user = await session.get(User, uid)
        assert user is not None
        return user.balance_kop, user.referral_balance_kop


# --- updates ----------------------------------------------------------------------------------------------------------
_ids = iter(range(1, 10_000_000))


def message_update(uid: int, text: str) -> Update:
    message = Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=uid, type="private"),
        from_user=tg_user(uid),
        text=text,
    )
    return Update(update_id=next(_ids), message=message)


def callback_update(uid: int, data: str) -> Update:
    message = Message(message_id=7, date=datetime.now(UTC), chat=Chat(id=uid, type="private"), text="screen")
    query = CallbackQuery(id=f"cb{next(_ids)}", from_user=tg_user(uid), chat_instance="ci", data=data, message=message)
    return Update(update_id=next(_ids), callback_query=query)


def pre_checkout_update(uid: int, payload: str, stars: int, currency: str = "XTR") -> Update:
    query = PreCheckoutQuery(
        id=f"pc{next(_ids)}", from_user=tg_user(uid), currency=currency, total_amount=stars, invoice_payload=payload
    )
    return Update(update_id=next(_ids), pre_checkout_query=query)


def successful_payment_update(
    uid: int, payload: str, stars: int, charge_id: str, *, recurring: bool = False, first: bool = False
) -> Update:
    paid = SuccessfulPayment(
        currency="XTR",
        total_amount=stars,
        invoice_payload=payload,
        telegram_payment_charge_id=charge_id,
        provider_payment_charge_id="",
        is_recurring=recurring or None,
        is_first_recurring=first or None,
    )
    message = Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=uid, type="private"),
        from_user=tg_user(uid),
        successful_payment=paid,
    )
    return Update(update_id=next(_ids), message=message)


def refunded_payment_update(uid: int, payload: str, stars: int, charge_id: str) -> Update:
    refund = RefundedPayment(
        currency="XTR", total_amount=stars, invoice_payload=payload, telegram_payment_charge_id=charge_id
    )
    message = Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=uid, type="private"),
        from_user=tg_user(uid),
        refunded_payment=refund,
    )
    return Update(update_id=next(_ids), message=message)
