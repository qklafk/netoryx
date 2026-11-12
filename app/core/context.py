"""The application context: one object holding every shared component, created once at startup."""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.config import Settings
from app.services.payments.base import (
    GatewayError,
    Invoice,
    PaymentGateway,
    ProviderStatus,
    WebhookRejected,
    WebhookResult,
    kop_to_plain,
)
from app.services.payments.registry import build_gateways, close_gateways

if TYPE_CHECKING:
    import aiohttp
    from aiogram import Bot, Dispatcher
    from sqlalchemy.ext.asyncio import AsyncEngine

    from app.core.i18n import I18n
    from app.core.locks import LockManager
    from app.core.modules import ModuleRegistry
    from app.core.settings_provider import SettingsProvider
    from app.db.models import Payment, User
    from app.db.session import SessionFactory
    from app.scheduler import Scheduler
    from app.services.maintenance import MaintenanceService
    from app.services.panels.registry import PanelRegistry
    from app.services.payments.service import CreditResult
    from app.utils.crypto import SecretBox

log = logging.getLogger(__name__)

DEV_GATEWAY_KEY = "test"
# Old gateways stay open this long after a rebuild so requests already in flight can finish.
GATEWAY_CLOSE_DELAY_SEC = 30.0

# Settings that affect payment gateways; other changes do not rebuild them.
_GATEWAY_FIELD_PREFIXES = (
    "stars_",
    "cryptobot_",
    "platega_",
    "yookassa_",
    "yoomoney_",
    "freekassa_",
    "payment_",
    "ton_",
    "usdt_trc20_",
    "crypto_rate_",
)
_GATEWAY_FIELDS = frozenset({"trusted_proxies", "public_base_url", "dev_mode", "app_mode"})


def gateway_fingerprint(settings: Settings) -> dict[str, Any]:
    data = settings.model_dump()
    return {k: v for k, v in data.items() if k in _GATEWAY_FIELDS or k.startswith(_GATEWAY_FIELD_PREFIXES)}


class DevGateway:
    """Fake provider for local development: the payment page is served by this bot and credits instantly."""

    key = DEV_GATEWAY_KEY
    title = "💳 Оплатить (тест)"
    supports_polling = False
    min_amount_kop = 100

    def __init__(self, public_base_url: str | None) -> None:
        self._base = (public_base_url or "http://localhost:8080").rstrip("/")

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        return Invoice(
            provider_payment_id=f"test-{payment.id}",
            url=f"{self._base}/dev/pay/{payment.id}",
            provider_amount=kop_to_plain(payment.amount_kop),
            provider_currency="RUB",
        )

    async def fetch(self, payment: Payment) -> ProviderStatus:
        raise GatewayError("test payments are confirmed on the development payment page")

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        raise WebhookRejected("test payments have no webhooks", 404)

    async def close(self) -> None:
        return None


class GatewayRegistry:
    """The enabled payment gateways for the current settings; rebuilt when payment settings change."""

    def __init__(
        self,
        settings_provider: SettingsProvider,
        *,
        return_url: str | None = None,
        http_timeout: float = 15.0,
        close_delay: float = GATEWAY_CLOSE_DELAY_SEC,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._provider = settings_provider
        self._session_factory = session_factory
        self._return_url = return_url
        self._timeout = http_timeout
        self._close_delay = close_delay
        self._gateways: dict[str, PaymentGateway] = {}
        self._fingerprint: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._closing: set[asyncio.Task[None]] = set()

    def get(self, key: str) -> PaymentGateway | None:
        return self._gateways.get(key)

    def enabled(self) -> list[PaymentGateway]:
        """Gateways in the order they are offered to users."""
        return list(self._gateways.values())

    def as_dict(self) -> dict[str, PaymentGateway]:
        """Snapshot for the payment service (``reconcile`` / ``handle_webhook`` take a mapping)."""
        return dict(self._gateways)

    async def rebuild(self) -> None:
        settings = self._provider.current()
        async with self._lock:
            gateways = self._build(settings)
            old, self._gateways = self._gateways, gateways
            self._fingerprint = gateway_fingerprint(settings)
        log.info("payment gateways: %s", ", ".join(gateways) or "none")
        if old:
            self._close_later(old)

    async def on_settings_changed(self, settings: Settings) -> None:
        """Settings subscriber: rebuild only when payment-related values changed."""
        if gateway_fingerprint(settings) != self._fingerprint:
            await self.rebuild()

    async def close(self) -> None:
        async with self._lock:
            gateways, self._gateways = self._gateways, {}
        for task in list(self._closing):
            task.cancel()
        await asyncio.gather(*self._closing, return_exceptions=True)
        await _close_quietly(gateways)

    def _build(self, settings: Settings) -> dict[str, PaymentGateway]:
        if settings.app_mode == "personal":
            gateways: dict[str, PaymentGateway] = {}
        else:
            gateways = build_gateways(
                settings, self._timeout, return_url=self._return_url, session_factory=self._session_factory
            )
        if settings.dev_mode:
            gateways[DEV_GATEWAY_KEY] = DevGateway(settings.public_base_url)
        return gateways

    def _close_later(self, gateways: dict[str, PaymentGateway]) -> None:
        async def close() -> None:
            try:
                await asyncio.sleep(self._close_delay)
            finally:
                await _close_quietly(gateways)

        task = asyncio.create_task(close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)


async def _close_quietly(gateways: dict[str, PaymentGateway]) -> None:
    try:
        await close_gateways(gateways)
    except Exception:
        log.exception("failed to close payment gateways")


@dataclass(eq=False)
class AppContext:
    """Shared components (dependency container). Handlers get it as ``ctx``, web routes via ``app.state.ctx``."""

    settings_provider: SettingsProvider
    session_factory: SessionFactory
    engine: AsyncEngine
    secret_box: SecretBox
    panels: PanelRegistry
    gateways: GatewayRegistry
    bot: Bot
    dispatcher: Dispatcher
    i18n: I18n
    http: aiohttp.ClientSession
    redis: Any | None
    bot_username: str
    started_at: datetime
    modules: ModuleRegistry
    maintenance: MaintenanceService
    locks: LockManager
    role: str = "all"
    webhook_secret: str | None = None  # X-Telegram-Bot-Api-Secret-Token in webhook mode
    log_buffer: Any | None = None  # recent log lines for the admin "System" page
    plugin_core: Any | None = None  # independent plugin lifecycle registry; legacy FeatureModules remain authoritative
    scheduler: Scheduler = field(init=False)

    @property
    def settings(self) -> Settings:
        return self.settings_provider.current()

    async def on_payment_credited(self, result: CreditResult) -> None:
        """Called after every successful credit (webhook, reconcile, manual): purchases, rewards, notices."""
        try:
            hooks = importlib.import_module("app.services.hooks")
            await hooks.on_payment_credited(self, result)
        except Exception:
            log.exception("payment %s: post-credit processing failed", result.payment_id)
