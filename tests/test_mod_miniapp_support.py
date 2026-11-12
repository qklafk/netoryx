"""Helpers for the Mini App API tests: an application context with fakes, signed initData and an HTTP client."""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.core.i18n import LOCALES_DIR, MODULES_DIR, I18n
from app.core.modules import FeatureModule, ModuleRegistry
from app.db.models import Payment, PaymentStatus, Subscription, SubscriptionStatus, User
from app.db.session import SessionFactory
from app.modules import miniapp_api
from app.modules.miniapp_api import deps as miniapp_deps
from app.services import users
from app.services.payments.base import Invoice, ProviderStatus, WebhookResult
from app.services.payments.stars import StarsGateway
from app.utils.crypto import SecretBox
from app.web.app import create_web_app
from app.web.tma import sign_init_data
from tests.conftest import make_settings
from tests.fakes import FakePanel

BOT_TOKEN = "123456:TEST"
USER_ID = 4242
BASE = "/api/app"


class SettingsBox:
    """Mutable settings (the real provider is re-read on every request too, so changes apply at once)."""

    def __init__(self, **overrides: Any) -> None:
        self._overrides = {"bot_token": BOT_TOKEN, **overrides}
        self.value = make_settings(**self._overrides)

    def update(self, **changes: Any) -> None:
        self._overrides.update(changes)
        self.value = make_settings(**self._overrides)


@dataclass
class FakeGateway:
    key: str = "cryptobot"
    title: str = "💎 CryptoBot"
    supports_polling: bool = True
    min_amount_kop: int = 100
    fail_create: bool = False
    status: PaymentStatus = PaymentStatus.PENDING
    created: list[str] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        if self.fail_create:
            from app.services.payments.base import GatewayError

            raise GatewayError("provider is down")
        self.created.append(description)
        return Invoice(f"inv-{payment.id}", f"https://pay.example/{payment.id}", "1.00", "RUB")

    async def fetch(self, payment: Payment) -> ProviderStatus:
        self.fetched.append(payment.id)
        return ProviderStatus(self.status, paid_amount_kop=payment.amount_kop)

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class FakeGateways:
    def __init__(self, *gateways: Any) -> None:
        self._gateways = {g.key: g for g in gateways}

    def get(self, key: str) -> Any:
        return self._gateways.get(key)

    def enabled(self) -> list[Any]:
        return list(self._gateways.values())


class FakePanels:
    def __init__(self) -> None:
        self.panels: dict[int, FakePanel] = {}

    async def get(self, panel_id: int) -> FakePanel:
        try:
            return self.panels[panel_id]
        except KeyError:
            raise LookupError(panel_id) from None


@dataclass
class FakeBot:
    invoice_links: list[dict[str, Any]] = field(default_factory=list)

    async def create_invoice_link(self, **params: Any) -> str:
        self.invoice_links.append(params)
        return f"https://t.me/$invoice{len(self.invoice_links)}"


@dataclass(eq=False)
class MiniAppCtx:
    box: SettingsBox
    session_factory: SessionFactory
    i18n: I18n
    modules: ModuleRegistry
    gateways: FakeGateways
    panels: FakePanels = field(default_factory=FakePanels)
    bot: FakeBot = field(default_factory=FakeBot)
    secret_box: SecretBox = field(default_factory=lambda: SecretBox("test-secret"))
    maintenance: Any = None
    http: Any = None
    redis: Any = None
    bot_username: str = "test_vpn_bot"
    webhook_secret: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    credited: list[Any] = field(default_factory=list)

    @property
    def settings(self) -> Settings:
        return self.box.value

    async def on_payment_credited(self, result: Any) -> None:
        self.credited.append(result)


def build_i18n(tmp_path: Path) -> I18n:
    """Core texts and this module's texts only (other modules' locale files are not needed here)."""
    modules_dir = tmp_path / "i18n_modules"
    shutil.copytree(MODULES_DIR / "miniapp_api" / "locales", modules_dir / "miniapp_api" / "locales")
    catalog = I18n(None, locales_dir=LOCALES_DIR, modules_dir=modules_dir)
    catalog.load_files()
    return catalog


def init_data(user_id: int = USER_ID, *, username: str = "ann", language: str = "ru") -> str:
    fields = {
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": "Ann", "username": username, "language_code": language}),
        "auth_date": str(int(datetime.now(UTC).timestamp())),
    }
    fields["hash"] = sign_init_data(fields, BOT_TOKEN)
    return urlencode(fields)


def auth(user_id: int = USER_ID, **kw: Any) -> dict[str, str]:
    return {"authorization": f"tma {init_data(user_id, **kw)}"}


async def add_user(session_factory: SessionFactory, settings: Settings, user_id: int = USER_ID, **values: Any) -> None:
    async with session_factory.begin() as session:
        user, _ = await users.get_or_create(session, settings, user_id, "ann", "Ann", "ru")
        for name, value in values.items():
            setattr(user, name, value)


async def add_member(session_factory: SessionFactory, user_id: int = USER_ID) -> None:
    """Give an existing user a subscription (a member in personal mode)."""
    async with session_factory.begin() as session:
        session.add(
            Subscription(
                user_id=user_id,
                client_uuid=f"member-{user_id}",
                status=SubscriptionStatus.ACTIVE,
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
        )


@dataclass
class Env:
    ctx: MiniAppCtx
    app: FastAPI
    client: httpx.AsyncClient
    notified: list[tuple[Any, str]]

    @property
    def settings(self) -> Settings:
        return self.ctx.settings

    async def get(self, path: str, user_id: int = USER_ID, **kw: Any) -> httpx.Response:
        return await self.client.get(BASE + path, headers=auth(user_id), **kw)

    async def post(self, path: str, json: Any = None, user_id: int = USER_ID, **kw: Any) -> httpx.Response:
        return await self.client.post(BASE + path, json=json if json is not None else {}, headers=auth(user_id), **kw)


@asynccontextmanager
async def miniapp_env(
    tmp_path: Path, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch, **overrides: Any
) -> AsyncIterator[Env]:
    """The web app with this module mounted, fakes for panels/payments/bot, and admin notifications captured."""
    notified: list[tuple[Any, str]] = []
    monkeypatch.setattr(miniapp_deps, "notify", lambda _ctx, event, text, **_: notified.append((event, text)))
    box = SettingsBox(**overrides)
    ctx = MiniAppCtx(
        box=box,
        session_factory=session_factory,
        i18n=build_i18n(tmp_path),
        modules=ModuleRegistry([miniapp_api.module]),
        gateways=FakeGateways(FakeGateway(), StarsGateway(box.value)),
    )
    app = create_web_app(cast(Any, ctx))
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield Env(ctx, app, client, notified)


def with_modules(env: Env, *modules: FeatureModule) -> None:
    """Install extra (stub) feature modules next to the Mini App API."""
    env.ctx.modules = ModuleRegistry([miniapp_api.module, *modules])


def stub_module(name: str, *, enabled: bool = True) -> FeatureModule:
    return FeatureModule(name=name, title_ru=name, title_en=name, enabled=lambda _s: enabled)
