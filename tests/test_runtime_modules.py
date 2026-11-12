"""Feature modules, module switches, payment gateway registry and the application context."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser
from fastapi import HTTPException

from app.config import Settings
from app.core.context import DEV_GATEWAY_KEY, AppContext, DevGateway, GatewayRegistry
from app.core.modules import (
    SHOP_ONLY_MODULES,
    AdminMenuItem,
    BotCommandSpec,
    FeatureModule,
    JobSpec,
    ModuleEnabled,
    ModuleRegistry,
    guard_router,
    load_modules,
    module_enabled,
    require_module,
)
from app.core.permissions import Permission
from app.core.settings_provider import SettingsProvider
from app.db.models import AdminRole, Payment, PaymentStatus, User
from app.db.session import SessionFactory
from app.services.maintenance import MaintenanceService
from app.services.payments.base import GatewayError, WebhookRejected
from app.utils.crypto import SecretBox
from tests.conftest import make_settings


def mod(name: str, **kw: Any) -> FeatureModule:
    return FeatureModule(name=name, title_ru=name, title_en=name, **kw)


# ---------------------------------------------------------------------------------------------
# Loading & enablement
# ---------------------------------------------------------------------------------------------
def test_load_modules_skips_missing_packages() -> None:
    modules = load_modules(["system", "definitely_missing"])
    assert [m.name for m in modules] == ["system"]
    system = modules[0]
    assert system.required and system.jobs and system.admin_bot_routers
    assert any(m.name == "system" for m in load_modules())


def test_load_modules_rejects_bad_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("app.modules.fake_bad")
    fake.module = "nope"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.modules.fake_bad", fake)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    with pytest.raises(TypeError):
        load_modules(["fake_bad"])
    fake.module = mod("other")  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="expected"):
        load_modules(["fake_bad"])


def test_module_enabled_rules() -> None:
    shop = make_settings()
    personal = make_settings(app_mode="personal")
    assert module_enabled(mod("wallet"), shop)
    for name in SHOP_ONLY_MODULES:
        assert not module_enabled(mod(name), personal)
    assert not module_enabled(mod("custom", shop_only=True), personal)
    assert module_enabled(mod("tickets"), personal)
    off = make_settings(disabled_modules="tickets, gifts")
    assert off.disabled_modules == ["tickets", "gifts"]
    assert not module_enabled(mod("tickets"), off)
    assert module_enabled(mod("tickets", required=True), off)
    assert not module_enabled(
        mod("referral", enabled=lambda s: s.referral_enabled), make_settings(referral_enabled=False)
    )

    def broken(_: Settings) -> bool:
        raise RuntimeError("boom")

    assert not module_enabled(mod("x", enabled=broken), shop)


def test_registry_menu_and_commands() -> None:
    a = mod(
        "a",
        order=2,
        admin_menu=[AdminMenuItem("a", "a.title", "A", Permission.USERS_VIEW, order=5)],
        commands=[BotCommandSpec("start", "старт", "start"), BotCommandSpec("admin", "адм", "adm", admin=True)],
    )
    b = mod(
        "b",
        order=1,
        admin_menu=[AdminMenuItem("b", "b.title", "B", Permission.ADMINS, order=1)],
        commands=[BotCommandSpec("start", "дубль", "dup")],
    )
    registry = ModuleRegistry([a, b])
    assert [m.name for m in registry] == ["b", "a"]
    assert len(registry) == 2 and "a" in registry and registry.get("zzz") is None
    settings = make_settings()
    assert [i.key for _, i in registry.admin_menu(settings, AdminRole.OWNER)] == ["b", "a"]
    assert [i.key for _, i in registry.admin_menu(settings, AdminRole.SUPPORT)] == ["a"]
    assert registry.admin_menu(settings, None) == []
    assert [c.command for c in registry.commands(settings, admin=False)] == ["start"]
    assert registry.commands(settings, admin=False)[0].description("en") == "dup"
    assert [c.command for c in registry.commands(settings, admin=True)] == ["start", "admin"]
    assert not registry.is_enabled("missing", settings)
    with pytest.raises(ValueError, match="duplicate"):
        ModuleRegistry([mod("a"), mod("a")])


def test_job_spec_schedule() -> None:
    async def job(ctx: Any) -> None:
        return None

    static = JobSpec("j", job, kwargs={"seconds": 5})
    dynamic = JobSpec(
        "d", job, schedule=lambda s: {"minutes": s.reconcile_interval_min}, enabled=lambda s: s.backup_enabled
    )
    settings = make_settings(reconcile_interval_min=7)
    assert static.trigger_kwargs(settings) == {"seconds": 5} and static.is_enabled(settings)
    assert dynamic.trigger_kwargs(settings) == {"minutes": 7}
    assert not dynamic.is_enabled(settings)  # backups are off in test settings


# ---------------------------------------------------------------------------------------------
# Filters / dependencies
# ---------------------------------------------------------------------------------------------
class SettingsBox:
    def __init__(self, settings: Settings) -> None:
        self.value = settings

    def current(self) -> Settings:
        return self.value


class _Ctx(SimpleNamespace):
    @property
    def settings(self) -> Settings:
        return self.box.current()


def make_ctx(session_factory: SessionFactory, modules: list[FeatureModule], settings: Settings) -> Any:
    return _Ctx(
        modules=ModuleRegistry(modules), maintenance=MaintenanceService(session_factory), box=SettingsBox(settings)
    )


async def test_module_enabled_filter(session_factory: SessionFactory) -> None:
    ctx = make_ctx(session_factory, [mod("gifts")], make_settings())
    f = ModuleEnabled("gifts")
    user = SimpleNamespace(id=5)
    assert await f(object(), ctx, event_from_user=user)
    await ctx.maintenance.set_disabled_modules(1, ["gifts"])
    assert not await f(object(), ctx, event_from_user=user)
    assert await f(object(), ctx, admin_role=AdminRole.SUPPORT, event_from_user=user)
    ctx.box.value = make_settings(disabled_modules=["gifts"])
    assert not await f(object(), ctx, admin_role=AdminRole.OWNER, event_from_user=user)
    assert not await ModuleEnabled("unknown")(object(), ctx)
    assert str(f) == "ModuleEnabled('gifts')"


async def test_require_module_dependency(session_factory: SessionFactory) -> None:
    ctx = make_ctx(session_factory, [mod("gifts")], make_settings())
    request: Any = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=ctx)))
    dependency = require_module("gifts")
    assert dependency.__name__ == "require_module_gifts"
    await dependency(request)
    ctx.box.value = make_settings(gifts_enabled=False, disabled_modules=["gifts"])
    with pytest.raises(HTTPException) as info:
        await dependency(request)
    assert info.value.status_code == 404
    with pytest.raises(HTTPException):
        await dependency(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))  # type: ignore[arg-type]


class NullSession(BaseSession):
    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:  # noqa: ASYNC109
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - unused
        yield b""

    async def close(self) -> None:
        return None


async def test_guarded_router_toggles_without_restart(session_factory: SessionFactory) -> None:
    ctx = make_ctx(session_factory, [mod("gifts")], make_settings())
    router = Router()
    sub = Router()
    router.include_router(sub)
    handled: list[str] = []

    @sub.message()
    async def handler(message: Message) -> None:
        handled.append(message.text or "")

    guard = guard_router(Router(), "gifts")
    guard.include_router(router)
    dispatcher = Dispatcher()
    dispatcher.include_router(guard)
    dispatcher["ctx"] = ctx
    bot = Bot("123456:TEST", session=NullSession())

    def update(n: int) -> Update:
        user = TgUser(id=5, is_bot=False, first_name="U")
        message = Message(
            message_id=n, date=datetime.now(UTC), chat=Chat(id=5, type="private"), from_user=user, text=f"m{n}"
        )
        return Update(update_id=n, message=message)

    await dispatcher.feed_update(bot, update(1))
    ctx.box.value = make_settings(disabled_modules=["gifts"])
    await dispatcher.feed_update(bot, update(2))
    ctx.box.value = make_settings()
    await dispatcher.feed_update(bot, update(3))
    assert handled == ["m1", "m3"]


# ---------------------------------------------------------------------------------------------
# Payment gateways
# ---------------------------------------------------------------------------------------------
async def provider(session_factory: SessionFactory, **env: Any) -> SettingsProvider:
    p = SettingsProvider(make_settings(**env), session_factory, SecretBox("k"))
    await p.load()
    return p


async def test_gateway_registry_modes(session_factory: SessionFactory) -> None:
    p = await provider(session_factory, stars_enabled=True, dev_mode=True)
    registry = GatewayRegistry(p, return_url="https://t.me/bot", close_delay=0)
    await registry.rebuild()
    assert [g.key for g in registry.enabled()] == ["stars", DEV_GATEWAY_KEY]
    assert registry.get("stars") is not None and registry.get("nope") is None
    assert set(registry.as_dict()) == {"stars", DEV_GATEWAY_KEY}

    await p.set("brand_name", "Other", actor_id=1)
    before = registry.enabled()
    await registry.on_settings_changed(p.current())
    assert registry.enabled() == before  # unrelated change: no rebuild

    await p.set("app_mode", "personal", actor_id=1)
    await registry.on_settings_changed(p.current())
    assert [g.key for g in registry.enabled()] == [DEV_GATEWAY_KEY]
    await registry.close()
    assert registry.enabled() == []


async def test_gateway_registry_without_dev_mode(session_factory: SessionFactory) -> None:
    p = await provider(session_factory)
    registry = GatewayRegistry(p, close_delay=0)
    await registry.rebuild()
    assert registry.enabled() == []
    p.subscribe(registry.on_settings_changed)
    await p.set("stars_enabled", True, actor_id=1)
    assert [g.key for g in registry.enabled()] == ["stars"]
    await registry.close()


async def test_dev_gateway() -> None:
    gateway = DevGateway("https://abc.trycloudflare.com/")
    payment = Payment(id="p-1", provider="test", user_id=1, amount_kop=15050, status=PaymentStatus.CREATED)
    invoice = await gateway.create(payment, User(tg_id=1), "desc")
    assert invoice.url == "https://abc.trycloudflare.com/dev/pay/p-1"
    assert invoice.provider_amount == "150.5" and invoice.provider_payment_id == "test-p-1"
    with pytest.raises(GatewayError):
        await gateway.fetch(payment)
    with pytest.raises(WebhookRejected):
        await gateway.parse_webhook(b"", {}, None)
    assert (await DevGateway(None).create(payment, User(tg_id=1), "d")).url == "http://localhost:8080/dev/pay/p-1"


# ---------------------------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------------------------
async def test_context_settings_and_payment_hook(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = await provider(session_factory, brand_name="Ctx")
    fields: dict[str, Any] = {name: None for name in AppContext.__dataclass_fields__ if name != "scheduler"}
    fields.update(settings_provider=p, bot_username="bot", started_at=datetime.now(UTC))
    ctx = AppContext(**fields)
    assert ctx.settings.brand_name == "Ctx"

    seen: list[Any] = []
    hooks = types.ModuleType("app.services.hooks")

    async def on_payment_credited(c: Any, result: Any) -> None:
        seen.append((c, result))
        raise RuntimeError("boom")  # logged, never raised to the payment flow

    hooks.on_payment_credited = on_payment_credited  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.services.hooks", hooks)
    result: Any = SimpleNamespace(payment_id="p")
    await ctx.on_payment_credited(result)
    assert seen == [(ctx, result)]
