"""Helpers for the web admin API tests: a context with real settings/i18n/maintenance services, fakes for Telegram,
panels, gateways and the scheduler, and an httpx client logged in as an admin of a given role."""

from __future__ import annotations

import io
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.core.i18n import LOCALES_DIR, I18n
from app.core.modules import ModuleRegistry
from app.core.settings_provider import SettingsProvider
from app.db.models import Admin, AdminRole, Base, Plan, Subscription, SubscriptionStatus, User
from app.db.session import SessionFactory, create_engine, create_session_factory
from app.modules import admin_api
from app.modules.admin_api.deps import drain_background
from app.runtime import LogBuffer
from app.scheduler import JobStatus
from app.services.maintenance import MaintenanceService
from app.utils.crypto import SecretBox
from app.web import auth
from app.web.app import create_web_app
from tests.conftest import make_settings
from tests.fakes import FakeCapablePanel

OWNER = 1000  # in ADMIN_IDS of make_settings()
MODULE_LOCALES = Path(admin_api.__file__).parent / "locales"


# ---------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------
@dataclass
class FakeBot:
    sent: list[tuple[int, str, dict[str, Any]]] = field(default_factory=list)
    refunds: list[tuple[int, str]] = field(default_factory=list)
    uploads: list[tuple[str, int]] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    stars_canceled: list[tuple[int, str]] = field(default_factory=list)
    fail_send: bool = False
    fail_stars: bool = False

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        if self.fail_send:
            from aiogram.exceptions import TelegramForbiddenError
            from aiogram.methods import SendMessage

            raise TelegramForbiddenError(SendMessage(chat_id=chat_id, text=text), "Forbidden: bot was blocked")
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=len(self.sent))

    async def refund_star_payment(self, user_id: int, telegram_payment_charge_id: str) -> bool:
        self.refunds.append((user_id, telegram_payment_charge_id))
        return True

    async def edit_user_star_subscription(
        self, user_id: int, telegram_payment_charge_id: str, is_canceled: bool
    ) -> bool:
        if self.fail_stars:
            raise OSError("network is down")
        if is_canceled:
            self.stars_canceled.append((user_id, telegram_payment_charge_id))
        return True

    async def _upload(self, kind: str, chat_id: int, *_: Any, **__: Any) -> Any:
        self.uploads.append((kind, chat_id))
        item = SimpleNamespace(file_id=f"{kind}-file-id")
        return SimpleNamespace(
            message_id=900 + len(self.uploads),
            photo=[SimpleNamespace(file_id="small"), item] if kind == "photo" else None,
            video=item if kind == "video" else None,
            animation=item if kind == "animation" else None,
            document=item if kind == "document" else None,
        )

    async def send_photo(self, chat_id: int, *a: Any, **kw: Any) -> Any:
        return await self._upload("photo", chat_id, *a, **kw)

    async def send_video(self, chat_id: int, *a: Any, **kw: Any) -> Any:
        return await self._upload("video", chat_id, *a, **kw)

    async def send_animation(self, chat_id: int, *a: Any, **kw: Any) -> Any:
        return await self._upload("animation", chat_id, *a, **kw)

    async def send_document(self, chat_id: int, *a: Any, **kw: Any) -> Any:
        return await self._upload("document", chat_id, *a, **kw)

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append(message_id)
        return True

    async def get_webhook_info(self) -> Any:
        return SimpleNamespace(
            url="",
            pending_update_count=0,
            last_error_date=None,
            last_error_message=None,
            max_connections=None,
            ip_address=None,
        )

    async def get_file(self, file_id: str) -> Any:
        return SimpleNamespace(file_id=file_id, file_size=4, file_path="photos/file_1.jpg")

    async def download(self, file: Any) -> io.BytesIO:
        return io.BytesIO(b"JPEG")

    def texts_to(self, chat_id: int) -> list[str]:
        return [text for cid, text, _ in self.sent if cid == chat_id]


class FakePanels:
    """Panel registry returning in-memory panels."""

    def __init__(self) -> None:
        self.backends: dict[int, FakeCapablePanel] = {}
        self.invalidated: list[int] = []

    def backend(self, panel_id: int) -> FakeCapablePanel:
        return self.backends.setdefault(panel_id, FakeCapablePanel())

    async def get(self, panel_id: int) -> FakeCapablePanel:
        return self.backend(panel_id)

    async def invalidate(self, panel_id: int) -> None:
        self.invalidated.append(panel_id)

    async def close(self) -> None:
        self.backends.clear()


@dataclass
class FakeGateway:
    key: str
    title: str = "Test"
    supports_polling: bool = True
    min_amount_kop: int = 100
    checked: int = 0

    async def check(self) -> str:
        self.checked += 1
        return "ok"


class FakeGateways:
    def __init__(self, *gateways: FakeGateway) -> None:
        self._gateways = {g.key: g for g in gateways}

    def get(self, key: str) -> Any:
        return self._gateways.get(key)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._gateways)


class FakeScheduler:
    def __init__(self, *job_ids: str) -> None:
        self.job_ids = list(job_ids)
        self.ran: list[str] = []

    def run_now(self, job_id: str) -> bool:
        if job_id not in self.job_ids:
            raise KeyError(job_id)
        self.ran.append(job_id)
        return True

    async def status(self) -> list[JobStatus]:
        return [JobStatus(id=j, module="core", enabled=True, next_run_at=None) for j in self.job_ids]


@dataclass
class ApiCtx:
    settings_provider: SettingsProvider
    session_factory: SessionFactory
    engine: AsyncEngine
    secret_box: SecretBox
    i18n: I18n
    maintenance: MaintenanceService
    modules: ModuleRegistry
    bot: FakeBot = field(default_factory=FakeBot)
    panels: FakePanels = field(default_factory=FakePanels)
    gateways: FakeGateways = field(default_factory=FakeGateways)
    scheduler: FakeScheduler | None = field(
        default_factory=lambda: FakeScheduler("provisioning.reconcile", "broadcasts.run", "payments.reconcile")
    )
    log_buffer: LogBuffer = field(default_factory=LogBuffer)
    redis: Any = None
    http: Any = None
    bot_username: str = "test_vpn_bot"
    role: str = "all"
    webhook_secret: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    credited: list[Any] = field(default_factory=list)

    @property
    def settings(self) -> Settings:
        return self.settings_provider.current()

    async def on_payment_credited(self, result: Any) -> None:
        self.credited.append(result)


async def build_ctx(tmp_path: Path, **overrides: Any) -> tuple[ApiCtx, AsyncEngine]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    settings = make_settings(backup_dir=str(tmp_path / "backups"), **overrides)
    box = SecretBox(settings.secret_key.get_secret_value() if settings.secret_key else "k")
    provider = SettingsProvider(settings, factory, box)
    await provider.load()
    modules_dir = tmp_path / "modules"
    shutil.copytree(MODULE_LOCALES, modules_dir / "admin_api" / "locales")
    i18n = I18n(factory, locales_dir=LOCALES_DIR, modules_dir=modules_dir)
    await i18n.load()
    ctx = ApiCtx(
        settings_provider=provider,
        session_factory=factory,
        engine=engine,
        secret_box=box,
        i18n=i18n,
        maintenance=MaintenanceService(factory),
        modules=ModuleRegistry([admin_api.module]),
    )
    return ctx, engine


class Api:
    """An httpx client for the app, logged in as one admin."""

    def __init__(self, ctx: ApiCtx, client: httpx.AsyncClient) -> None:
        self.ctx = ctx
        self.client = client
        self.csrf = ""
        self.cookie = ""

    async def login(self, tg_id: int = OWNER, role: AdminRole | None = None) -> Api:
        async with self.ctx.session_factory.begin() as session:
            if role is not None and tg_id not in self.ctx.settings.admin_ids:
                session.add(Admin(tg_id=tg_id, role=role))
            token, _ = await auth.create_session(session, self.ctx.settings, tg_id, ip="203.0.113.5", user_agent="t")
        self.cookie = f"__Host-{auth.SESSION_COOKIE}={token}"
        self.csrf = auth.csrf_token_for(token)
        return self

    def _headers(self, method: str, extra: dict[str, str] | None) -> dict[str, str]:
        headers = dict(extra or {})
        if self.cookie:
            headers.setdefault("cookie", self.cookie)
        if method not in ("GET", "HEAD") and self.csrf:
            headers.setdefault(auth.CSRF_HEADER, self.csrf)
        return headers

    async def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = self._headers(method, kw.pop("headers", None))
        return await self.client.request(method, "/api/admin" + path, headers=headers, **kw)

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> httpx.Response:
        return await self.request("POST", path, **kw)

    async def put(self, path: str, **kw: Any) -> httpx.Response:
        return await self.request("PUT", path, **kw)

    async def patch(self, path: str, **kw: Any) -> httpx.Response:
        return await self.request("PATCH", path, **kw)

    async def delete(self, path: str, **kw: Any) -> httpx.Response:
        return await self.request("DELETE", path, **kw)


@pytest.fixture
async def ctx(tmp_path: Path) -> AsyncIterator[ApiCtx]:
    context, engine = await build_ctx(tmp_path)
    try:
        yield context
    finally:
        await drain_background()
        await engine.dispose()


@pytest.fixture
async def api(ctx: ApiCtx) -> AsyncIterator[Api]:
    app = create_web_app(ctx)  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app, client=("203.0.113.5", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield await Api(ctx, client).login()


async def as_role(api: Api, role: AdminRole, tg_id: int = 2000) -> Api:
    other = Api(api.ctx, api.client)
    return await other.login(tg_id, role)


# ---------------------------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------------------------
async def add_user(
    ctx: ApiCtx,
    tg_id: int,
    *,
    username: str | None = None,
    first_name: str | None = None,
    balance: int = 0,
    referred_by: int | None = None,
    sub_days: int | None = None,
    plan_id: int | None = None,
    **fields: Any,
) -> User:
    from datetime import timedelta

    async with ctx.session_factory.begin() as session:
        user = User(
            tg_id=tg_id,
            username=username,
            first_name=first_name,
            referral_code=f"R{tg_id}",
            sub_token=f"token{tg_id:0>20}",
            balance_kop=balance,
            referred_by=referred_by,
            **fields,
        )
        session.add(user)
        await session.flush()
        if sub_days is not None:
            session.add(
                Subscription(
                    user_id=tg_id,
                    plan_id=plan_id,
                    client_uuid=f"00000000-0000-4000-8000-{tg_id:012d}",
                    status=SubscriptionStatus.ACTIVE,
                    expires_at=datetime.now(UTC) + timedelta(days=sub_days),
                    device_limit=3,
                )
            )
    return user


async def add_plan(ctx: ApiCtx, title: str = "1 месяц", days: int = 30, price_kop: int = 19900) -> int:
    async with ctx.session_factory.begin() as session:
        plan = Plan(title=title, duration_days=days, price_kop=price_kop, device_limit=3, sort_order=0)
        session.add(plan)
        await session.flush()
        return plan.id
