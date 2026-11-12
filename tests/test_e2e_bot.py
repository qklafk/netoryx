"""End-to-end flows through the real application: every module, the real middlewares, texts and web app.

Only the edges are replaced: Telegram answers locally (recorded calls) and panels are in-memory fakes. Updates are fed
into the dispatcher the way polling or the webhook would, and the web routes are called in-process.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetUpdates, SendMessage, SendPhoto, TelegramMethod
from aiogram.types import InlineKeyboardButton, Update
from sqlalchemy import func, select

from app.config import Settings
from app.core.context import AppContext
from app.db.models import (
    Invite,
    Panel,
    PanelKind,
    Payment,
    PaymentStatus,
    Plan,
    Provision,
    Purchase,
    PurchaseKind,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.keyboards import Nav
from app.middlewares import Middlewares, setup_middlewares
from app.modules.invites.callbacks import NAV as INVITES_NAV
from app.modules.invites.callbacks import UNLIMITED, InvNew
from app.modules.subscription.callbacks import PayCb, PlanCb
from app.runtime import Factories, Runtime
from app.services.panels import inventory
from app.services.panels import registry as panel_registry
from app.web.app import create_web_app
from app.web.auth import create_login_link
from app.web.tma import sign_init_data
from tests.fakes import FakePanel
from tests.test_middlewares_support import FakeSession, callback_update, make_test_settings, message_update

OWNER = 1000  # ADMIN_IDS
REFERRER = 5100
USER = 5001
FRIEND = 5002


class Telegram(FakeSession):
    """The recording Bot API of the middleware tests, plus an idle ``getUpdates`` for polling."""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, GetUpdates):
            await asyncio.sleep(0.05)
            return []
        return await super().make_request(bot, method, timeout)


@dataclass
class App:
    runtime: Runtime
    ctx: AppContext
    api: Telegram
    mw: Middlewares
    panel: FakePanel
    web: httpx.AsyncClient
    pressed: list[str] = field(default_factory=list)

    @property
    def settings(self) -> Settings:
        return self.ctx.settings

    async def feed(self, update: Update) -> None:
        await self.ctx.dispatcher.feed_update(self.ctx.bot, update)
        await self.mw.replayer.wait()

    async def send(self, uid: int, text: str, *, first_name: str = "Ann") -> None:
        await self.feed(message_update(uid, text, first_name=first_name))

    async def press(self, uid: int, data: Any) -> None:
        update = callback_update(uid, data if isinstance(data, str) else data.pack())
        assert update.callback_query is not None
        self.pressed.append(update.callback_query.id)
        await self.feed(update)

    def screens(self, uid: int) -> list[tuple[str, Any]]:
        """Texts (with keyboards) the bot sent to or edited for ``uid``, in order."""
        result: list[tuple[str, Any]] = []
        for call in self.api.calls:
            if isinstance(call, SendMessage | EditMessageText | SendPhoto) and int(call.chat_id or 0) == uid:
                text = call.caption if isinstance(call, SendPhoto) else call.text
                result.append((text or "", call.reply_markup))
        return result

    def last_text(self, uid: int) -> str:
        screens = self.screens(uid)
        return screens[-1][0] if screens else ""

    def buttons(self, uid: int) -> list[InlineKeyboardButton]:
        screens = self.screens(uid)
        markup = screens[-1][1] if screens else None
        if markup is None or not hasattr(markup, "inline_keyboard"):
            return []
        return [button for row in markup.inline_keyboard for button in row]

    def button_data(self, uid: int, prefix: str) -> str:
        for button in self.buttons(uid):
            if button.callback_data and button.callback_data.startswith(prefix):
                return button.callback_data
        raise AssertionError(f"no {prefix!r} button in {[b.callback_data for b in self.buttons(uid)]}")

    def unanswered_callbacks(self) -> list[str]:
        answered = {call.callback_query_id for call in self.api.of(AnswerCallbackQuery)}
        return [cid for cid in self.pressed if cid not in answered]


def e2e_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'e2e.db').as_posix()}",
        "dev_mode": True,
        "throttle_rate_limit": 10_000,
        "sub_happ_crypto_links": False,  # no calls to the Happ API from offline tests
        "shutdown_timeout_sec": 5,
        "trial_days": 3,
    }
    values.update(overrides)
    return make_test_settings(**values)


@pytest.fixture
async def start_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Any]:
    started: list[App] = []
    panel = FakePanel()
    monkeypatch.setattr(panel_registry, "build_backend", lambda *_args, **_kw: panel)

    async def start(**overrides: Any) -> App:
        api = Telegram()
        captured: list[Middlewares] = []

        async def middlewares(dispatcher: Dispatcher, ctx: AppContext) -> None:
            captured.append(setup_middlewares(dispatcher, ctx))

        runtime = Runtime(
            e2e_settings(tmp_path, **overrides),
            role="bot",
            factories=Factories(bot=lambda _settings: Bot("42:TEST", session=api), middlewares=middlewares),
        )
        ctx = await runtime.start()
        web = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_web_app(ctx)), base_url="https://bot.example.com"
        )
        app = App(runtime, ctx, api, captured[0], panel, web)
        started.append(app)
        await add_server(app)
        return app

    yield start
    for app in started:
        await app.web.aclose()
        await app.runtime.stop()


async def add_server(app: App) -> int:
    """A 3x-ui panel with one VLESS-Reality inbound and a direct endpoint, imported like the admin wizard does."""
    async with app.ctx.session_factory.begin() as session:
        panel = Panel(name="DE", kind=PanelKind.XUI, url="https://xui.example.com")
        session.add(panel)
        await session.flush()
        panel_id = panel.id
    await inventory.refresh_inbounds(
        app.ctx.session_factory, app.ctx.panels, panel_id, activate_new=True, endpoint_address="de.example.com"
    )
    return panel_id


def init_data(settings: Settings, user_id: int) -> str:
    """``initData`` as Telegram signs it for the bot's token."""
    fields = {
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": "Ann", "language_code": "ru"}),
        "auth_date": str(int(datetime.now(UTC).timestamp())),
    }
    fields["hash"] = sign_init_data(fields, settings.bot_token.get_secret_value())
    return urlencode(fields)


async def one(app: App, model: Any, *where: Any) -> Any:
    async with app.ctx.session_factory() as session:
        return await session.scalar(select(model).where(*where))


async def count(app: App, model: Any, *where: Any) -> int:
    async with app.ctx.session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def test_shop_flow_from_referral_link_to_admin_and_web(start_app: Any) -> None:
    app: App = await start_app()
    sf = app.ctx.session_factory
    async with sf.begin() as session:
        plan = Plan(title="Месяц", duration_days=30, price_kop=15_000, device_limit=3, traffic_limit_gb=0)
        session.add(plan)
        await session.flush()
        plan_id = plan.id

    # A referrer joins, then a friend comes through the referral link.
    await app.send(REFERRER, "/start", first_name="Ref")
    referrer: User = await one(app, User, User.tg_id == REFERRER)
    await app.send(USER, f"/start ref_{referrer.referral_code}")
    user: User = await one(app, User, User.tg_id == USER)
    assert user.referred_by == REFERRER
    assert app.screens(USER), "no welcome screen"

    # Main menu, then the free trial: the account is pushed to the panel right away.
    await app.press(USER, Nav(to="menu"))
    assert app.buttons(USER)
    await app.press(USER, Nav(to="trial"))
    sub: Subscription = await one(app, Subscription, Subscription.user_id == USER)
    assert sub is not None and sub.status == SubscriptionStatus.ACTIVE
    trial_expiry = sub.expires_at
    assert await count(app, Purchase, Purchase.kind == PurchaseKind.TRIAL) == 1
    assert app.panel.accounts, "the trial account was not created on the panel"

    # Connection screen shows the personal subscription URL.
    await app.press(USER, Nav(to="connect"))
    assert f"/sub/{user.sub_token}" in app.last_text(USER) or any(
        f"/sub/{user.sub_token}" in (b.url or "") or f"/sub/{user.sub_token}" in str(b.copy_text or "")
        for b in app.buttons(USER)
    )

    # Plans -> plan -> pay with the test provider (DEV_MODE) for what the balance (a welcome bonus) lacks.
    balance = (await one(app, User, User.tg_id == USER)).balance_kop
    assert 0 <= balance < 15_000
    await app.press(USER, Nav(to="buy"))
    assert "Месяц" in app.last_text(USER)
    await app.press(USER, PlanCb(id=plan_id))
    pay = PayCb.unpack(app.button_data(USER, f"sbpay:{plan_id}:test:"))
    await app.press(USER, pay)
    payment: Payment = await one(app, Payment, Payment.user_id == USER)
    assert payment is not None and payment.amount_kop == 15_000 - balance and payment.plan_id == plan_id
    assert any((b.url or "").endswith(f"/dev/pay/{payment.id}") for b in app.buttons(USER))

    # The test payment page credits it; the plan is bought and the subscription extended.
    resp = await app.web.post(f"/dev/pay/{payment.id}", follow_redirects=False)
    assert resp.status_code == 303
    payment = await one(app, Payment, Payment.id == payment.id)
    assert payment.status == PaymentStatus.SUCCEEDED
    sub = await one(app, Subscription, Subscription.user_id == USER)
    assert sub.plan_id == plan_id
    assert sub.expires_at - trial_expiry >= timedelta(days=29)
    assert await count(app, Purchase, Purchase.kind == PurchaseKind.PURCHASE) == 1
    user = await one(app, User, User.tg_id == USER)
    assert user.balance_kop == 0

    # Subscription URL for apps: links + subscription headers; a browser gets the landing page.
    resp = await app.web.get(f"/sub/{user.sub_token}", headers={"user-agent": "v2rayNG/1.9.0"})
    assert resp.status_code == 200
    assert "subscription-userinfo" in resp.headers
    body = resp.text
    if "vless://" not in body:
        body = base64.b64decode(body + "=" * (-len(body) % 4)).decode()
    assert "vless://" in body and sub.client_uuid in body
    landing = await app.web.get(f"/sub/{user.sub_token}", headers={"user-agent": "Mozilla/5.0", "accept": "text/html"})
    assert landing.status_code == 200 and "text/html" in landing.headers["content-type"]
    assert (await app.web.get("/sub/unknown-token-000")).status_code == 404

    # One-tap import page for Happ.
    opened = await app.web.get(f"/open/happ/{user.sub_token}")
    assert opened.status_code == 200 and "happ://" in opened.text

    # Mini App with a valid initData.
    me = await app.web.get("/api/app/me", headers={"authorization": f"tma {init_data(app.settings, USER)}"})
    assert me.status_code == 200, me.text
    assert str(USER) in me.text
    forged = await app.web.get("/api/app/me", headers={"authorization": "tma user=1&hash=00"})
    assert forged.status_code == 401

    # The admin finds the user and grants 7 days.
    await app.send(OWNER, "/admin")
    assert app.buttons(OWNER), "no admin menu"
    await app.press(OWNER, "nav:adm_users:")
    await app.send(OWNER, str(USER))
    assert f"<code>{USER}</code>" in app.last_text(OWNER)
    before = sub.expires_at
    await app.press(OWNER, f"admu:days:{USER}:")
    await app.send(OWNER, "7")
    confirm = next(b.callback_data for b in app.buttons(OWNER) if b.text == "✅ Да")
    await app.press(OWNER, confirm)
    sub = await one(app, Subscription, Subscription.user_id == USER)
    assert sub.expires_at - before == timedelta(days=7)

    # Web admin: one-time login link from the bot, then the users list.
    link = await create_login_link(sf, app.settings, OWNER)
    token = link.split("#token=", 1)[1]
    login = await app.web.post("/api/admin/auth/magic", json={"token": token})
    assert login.status_code == 200, login.text
    users = await app.web.get("/api/admin/users")
    assert users.status_code == 200, users.text
    assert str(USER) in users.text
    assert (await app.web.post("/api/admin/auth/magic", json={"token": token})).status_code == 401  # single use

    # Every button press got an answer (no spinning buttons).
    assert app.unanswered_callbacks() == []


async def test_personal_mode_invite_to_subscription(start_app: Any) -> None:
    app: App = await start_app(app_mode="personal")

    # Strangers cannot use a private bot.
    await app.send(FRIEND, "/start")
    assert await count(app, Subscription, Subscription.user_id == FRIEND) == 0

    # The owner creates an invitation for one person, a month, any number of devices.
    await app.send(OWNER, "/start")
    await app.press(OWNER, Nav(to=INVITES_NAV))
    await app.press(OWNER, InvNew(step="create", days=30, devices=UNLIMITED, uses=1))
    invite: Invite = await one(app, Invite)
    assert invite is not None and invite.created_by == OWNER
    assert f"start=inv_{invite.code}" in app.last_text(OWNER)

    # A friend opens the link: free subscription, pushed to the panel.
    await app.send(FRIEND, f"/start inv_{invite.code}", first_name="Дима")
    sub: Subscription = await one(app, Subscription, Subscription.user_id == FRIEND)
    assert sub is not None and sub.status == SubscriptionStatus.ACTIVE and sub.plan_id is None
    assert sub.expires_at - datetime.now(UTC) > timedelta(days=29)
    assert await count(app, Provision, Provision.subscription_id == sub.id) >= 1
    assert app.panel.accounts, "the member's account was not created on the panel"
    assert Nav(to="connect").pack() in [b.callback_data for b in app.buttons(FRIEND)]
    assert any("Дима" in text for text, _ in app.screens(OWNER)), "the inviter was not told"

    # ...and sees it in the bot, without anything to buy.
    await app.press(FRIEND, Nav(to="sub"))
    card = app.last_text(FRIEND)
    assert card
    labels = " ".join(b.text for b in app.buttons(FRIEND))
    assert "₽" not in labels

    # The invitation was for one person only.
    await app.send(REFERRER, f"/start inv_{invite.code}")
    assert await count(app, Subscription, Subscription.user_id == REFERRER) == 0
    assert app.unanswered_callbacks() == []
