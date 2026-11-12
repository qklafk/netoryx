from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter
from sqlalchemy import select

from app.config import Settings
from app.core.modules import FeatureModule
from app.db.models import Panel, PanelKind, Payment, PaymentStatus, User
from app.db.session import SessionFactory
from app.services import users
from app.web.routes import telegram
from tests.conftest import make_settings
from tests.test_web_support import AsgiClient, FakeCtx, make_ctx, web_app

WEBHOOK_SETTINGS = {"bot_mode": "webhook", "telegram_webhook_secret": "s3cret-token"}


# ---------------------------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------------------------
async def test_healthz_ok(session_factory: SessionFactory, settings: Settings) -> None:
    client = AsgiClient(web_app(make_ctx(settings, session_factory)))
    resp = await client.get("/healthz")
    assert resp.status == 200
    body = resp.json()
    assert body["status"] == "ok" and body["db"] and body["bot"]
    assert body["panels"] == {"total": 0, "healthy": 0}
    assert body["uptime_sec"] >= 0
    assert resp.headers["cache-control"] == "no-store"


async def test_healthz_degraded(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        s.add(Panel(name="a", kind=PanelKind.XUI, url="https://a", consecutive_failures=0))
        s.add(Panel(name="b", kind=PanelKind.XUI, url="https://b", consecutive_failures=4))
        s.add(Panel(name="c", kind=PanelKind.XUI, url="https://c", consecutive_failures=9, is_active=False))
    ctx = make_ctx(settings, session_factory)
    client = AsgiClient(web_app(ctx))
    body = (await client.get("/healthz")).json()
    assert body["status"] == "degraded" and body["panels"] == {"total": 2, "healthy": 1}
    async with session_factory.begin() as s:
        for panel in (await s.scalars(select(Panel))).all():
            panel.consecutive_failures = 0
    ctx.bot_username = ""
    body = (await client.get("/healthz")).json()
    assert body["status"] == "degraded" and body["bot"] is False


class _BrokenFactory:
    def __init__(self) -> None:
        self.kw: dict[str, Any] = {}

    def __call__(self, **_: Any) -> Any:
        raise ConnectionError("db down")


async def test_healthz_db_down(session_factory: SessionFactory, settings: Settings) -> None:
    ctx = make_ctx(settings, session_factory)
    client = AsgiClient(web_app(ctx))
    ctx.session_factory = _BrokenFactory()  # type: ignore[assignment]
    resp = await client.get("/healthz")
    assert resp.status == 503
    assert resp.json()["status"] == "error" and resp.json()["db"] is False


# ---------------------------------------------------------------------------------------------
# Telegram webhook
# ---------------------------------------------------------------------------------------------
def _hook_headers(secret: str = "s3cret-token") -> dict[str, str]:
    return {telegram.SECRET_HEADER: secret}


async def _drain(ctx: FakeCtx, client: AsgiClient) -> None:
    feeder: telegram.UpdateFeeder = client.app.state.update_feeder  # type: ignore[attr-defined]
    for _ in range(50):
        if feeder.pending == 0:
            return
        await asyncio.sleep(0.01)


async def test_webhook_disabled_in_polling_mode(session_factory: SessionFactory, settings: Settings) -> None:
    client = AsgiClient(web_app(make_ctx(settings, session_factory)))
    resp = await client.post(telegram.WEBHOOK_PATH, json={"update_id": 1}, headers=_hook_headers())
    assert resp.status == 404


async def test_webhook_feeds_dispatcher(session_factory: SessionFactory) -> None:
    ctx = make_ctx(make_settings(**WEBHOOK_SETTINGS), session_factory)
    client = AsgiClient(web_app(ctx))
    assert (await client.post(telegram.WEBHOOK_PATH, json={"update_id": 1})).status == 401
    assert (await client.post(telegram.WEBHOOK_PATH, json={"update_id": 1}, headers=_hook_headers("x"))).status == 401

    resp = await client.post(telegram.WEBHOOK_PATH, json={"update_id": 42}, headers=_hook_headers())
    assert resp.status == 200
    await _drain(ctx, client)
    assert [u.update_id for _, u in ctx.dispatcher.fed] == [42]

    bad_json = await client.post(telegram.WEBHOOK_PATH, body=b"{nope", headers=_hook_headers())
    assert bad_json.status == 400
    not_object = await client.post(telegram.WEBHOOK_PATH, json=[1], headers=_hook_headers())
    assert not_object.status == 400
    unparseable = await client.post(telegram.WEBHOOK_PATH, json={"update_id": "x"}, headers=_hook_headers())
    assert unparseable.status == 200
    await _drain(ctx, client)
    assert len(ctx.dispatcher.fed) == 1


async def test_webhook_handler_errors_are_contained(session_factory: SessionFactory) -> None:
    ctx = make_ctx(make_settings(**WEBHOOK_SETTINGS), session_factory)
    ctx.dispatcher.fail = True
    client = AsgiClient(web_app(ctx))
    resp = await client.post(telegram.WEBHOOK_PATH, json={"update_id": 1}, headers=_hook_headers())
    assert resp.status == 200
    await _drain(ctx, client)


async def test_webhook_queue_full(session_factory: SessionFactory) -> None:
    ctx = make_ctx(make_settings(**WEBHOOK_SETTINGS), session_factory)
    app = web_app(ctx)
    app.state.update_feeder = telegram.UpdateFeeder(max_concurrency=1, max_pending=1)
    release = asyncio.Event()

    async def slow(bot: Any, update: Any) -> None:
        await release.wait()

    ctx.dispatcher.feed_update = slow  # type: ignore[method-assign]
    client = AsgiClient(app)
    assert (await client.post(telegram.WEBHOOK_PATH, json={"update_id": 1}, headers=_hook_headers())).status == 200
    full = await client.post(telegram.WEBHOOK_PATH, json={"update_id": 2}, headers=_hook_headers())
    assert full.status == 503 and full.headers["retry-after"] == "5"
    release.set()
    await app.state.update_feeder.close(1.0)


async def test_feeder_close_cancels_stragglers() -> None:
    feeder = telegram.UpdateFeeder()
    started = asyncio.Event()
    finished: list[str] = []

    async def quick() -> None:
        finished.append("quick")

    async def stuck() -> None:
        started.set()
        await asyncio.sleep(3600)

    assert feeder.submit(quick) and feeder.submit(stuck)
    await started.wait()
    await feeder.close(0.05)
    assert finished == ["quick"] and feeder.pending == 0
    assert not feeder.submit(quick)  # closed


async def test_lifespan_closes_feeder(session_factory: SessionFactory, settings: Settings) -> None:
    app = web_app(make_ctx(settings, session_factory))
    async with app.router.lifespan_context(app):
        pass
    assert not app.state.update_feeder.submit(lambda: asyncio.sleep(0))


def test_webhook_secret_token() -> None:
    assert telegram.webhook_secret_token(make_settings(telegram_webhook_secret="abc")) == "abc"
    derived = telegram.webhook_secret_token(make_settings())
    assert derived == telegram.webhook_secret_token(make_settings()) and len(derived) == 64
    assert derived != telegram.webhook_secret_token(make_settings(bot_token="999:OTHER"))
    assert telegram.webhook_url(make_settings()) == "https://bot.example.com/telegram/webhook"
    assert telegram.webhook_url(make_settings(public_base_url=None)) is None


# ---------------------------------------------------------------------------------------------
# /dev/pay
# ---------------------------------------------------------------------------------------------
async def _payment(session_factory: SessionFactory, settings: Settings, provider: str = "test") -> str:
    payment_id = str(uuid.uuid4())
    async with session_factory.begin() as s:
        await users.get_or_create(s, settings, 5, "u", "U", "ru")
        s.add(Payment(id=payment_id, provider=provider, user_id=5, amount_kop=15_000, status=PaymentStatus.PENDING))
    return payment_id


async def test_dev_pay_only_in_dev_mode(session_factory: SessionFactory, settings: Settings) -> None:
    payment_id = await _payment(session_factory, settings)
    client = AsgiClient(web_app(make_ctx(settings, session_factory)))
    assert (await client.get(f"/dev/pay/{payment_id}")).status == 404
    assert (await client.post(f"/dev/pay/{payment_id}")).status == 404


async def test_dev_pay_flow(session_factory: SessionFactory) -> None:
    settings = make_settings(dev_mode=True)
    payment_id = await _payment(session_factory, settings)
    ctx = make_ctx(settings, session_factory)
    client = AsgiClient(web_app(ctx))

    page = await client.get(f"/dev/pay/{payment_id}")
    assert page.status == 200 and "150 ₽" in page.text and "<form" in page.text

    resp = await client.post(f"/dev/pay/{payment_id}")
    assert resp.status == 303 and resp.headers["location"] == f"/dev/pay/{payment_id}"
    assert len(ctx.credited) == 1 and ctx.credited[0].amount_kop == 15_000
    async with session_factory() as s:
        user = await s.get(User, 5)
        payment = await s.get(Payment, payment_id)
    assert user is not None and user.balance_kop == 15_000
    assert payment is not None and payment.status == PaymentStatus.SUCCEEDED

    assert (await client.post(f"/dev/pay/{payment_id}")).status == 303
    assert len(ctx.credited) == 1  # idempotent
    done = await client.get(f"/dev/pay/{payment_id}")
    assert "succeeded" in done.text and "<form" not in done.text and "t.me/test_vpn_bot" in done.text


async def test_dev_pay_hook_failure_is_logged(session_factory: SessionFactory) -> None:
    settings = make_settings(dev_mode=True)
    payment_id = await _payment(session_factory, settings)

    async def boom(_: Any) -> None:
        raise RuntimeError("hook")

    ctx = make_ctx(settings, session_factory, on_credit=boom)
    resp = await AsgiClient(web_app(ctx)).post(f"/dev/pay/{payment_id}")
    assert resp.status == 303


@pytest.mark.parametrize("payment_ref", ["real", "missing", "bad"])
async def test_dev_pay_refuses_other_payments(session_factory: SessionFactory, payment_ref: str) -> None:
    settings = make_settings(dev_mode=True)
    real = await _payment(session_factory, settings, provider="yookassa")
    payment_id = {"real": real, "missing": str(uuid.uuid4()), "bad": "not a uuid!"}[payment_ref]
    client = AsgiClient(web_app(make_ctx(settings, session_factory)))
    assert (await client.get(f"/dev/pay/{payment_id}")).status == 404
    assert (await client.post(f"/dev/pay/{payment_id}")).status == 404


# ---------------------------------------------------------------------------------------------
# SPAs
# ---------------------------------------------------------------------------------------------
async def test_spa_placeholder(session_factory: SessionFactory, settings: Settings, tmp_path: Path) -> None:
    client = AsgiClient(web_app(make_ctx(settings, session_factory), static_dir=tmp_path))
    resp = await client.get("/admin/")
    assert resp.status == 503 and "not built" in resp.text
    redirect = await client.get("/admin")
    assert redirect.status in (307, 308) and redirect.headers["location"].endswith("/admin/")
    assert (await client.get("/app/")).status == 503


async def test_spa_serves_build(session_factory: SessionFactory, tmp_path: Path) -> None:
    for name in ("admin", "app"):
        (tmp_path / name / "assets").mkdir(parents=True)
        (tmp_path / name / "index.html").write_text(f"<html>{name} index</html>", encoding="utf-8")
        (tmp_path / name / "assets" / "main.abc123.js").write_text("console.log(1)", encoding="utf-8")
    settings = make_settings()
    ctx = make_ctx(settings, session_factory)
    client = AsgiClient(web_app(ctx, static_dir=tmp_path))

    index = await client.get("/admin/")
    assert index.status == 200 and "admin index" in index.text and index.headers["cache-control"] == "no-cache"
    deep = await client.get("/admin/users/42")
    assert deep.status == 200 and "admin index" in deep.text
    asset = await client.get("/admin/assets/main.abc123.js")
    assert asset.status == 200 and "immutable" in asset.headers["cache-control"]
    missing = await client.get("/admin/assets/missing.js")
    assert missing.status == 404
    traversal = await client.get("/admin/../../etc/passwd.txt")
    assert traversal.status == 404
    assert "app index" in (await client.get("/app/")).text

    ctx.settings = make_settings(web_admin_enabled=False, miniapp_enabled=False)
    assert (await client.get("/admin/")).status == 404
    assert (await client.get("/app/")).status == 404


# ---------------------------------------------------------------------------------------------
# Module routers
# ---------------------------------------------------------------------------------------------
async def test_public_module_routes_follow_module_switch(session_factory: SessionFactory) -> None:
    router = APIRouter()

    @router.get("/gift/{code}")
    async def gift(code: str) -> dict[str, str]:
        return {"code": code}

    module = FeatureModule(name="gifts", title_ru="g", title_en="g", web_routers=[router])
    ctx = make_ctx(make_settings(), session_factory, module)
    client = AsgiClient(web_app(ctx))
    assert (await client.get("/gift/ABC")).json() == {"code": "ABC"}
    ctx.settings = make_settings(app_mode="personal")  # gifts are a shop-only feature
    assert (await client.get("/gift/ABC")).status == 404
    ctx.settings = make_settings(disabled_modules="gifts")
    assert (await client.get("/gift/ABC")).status == 404


async def test_modules_argument_overrides_context(session_factory: SessionFactory, settings: Settings) -> None:
    router = APIRouter()

    @router.get("/ping-module")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    module = FeatureModule(name="probe", title_ru="p", title_en="p", web_routers=[router])
    ctx = make_ctx(settings, session_factory, module)
    client = AsgiClient(web_app(ctx, modules=[]))
    assert (await client.get("/ping-module")).status == 404


async def test_webhook_prefers_context_secret(session_factory: SessionFactory) -> None:
    ctx = make_ctx(make_settings(bot_mode="webhook"), session_factory, webhook_secret="generated-at-startup")
    client = AsgiClient(web_app(ctx))
    fallback = telegram.webhook_secret_token(ctx.settings)
    assert (
        await client.post(telegram.WEBHOOK_PATH, json={"update_id": 1}, headers=_hook_headers(fallback))
    ).status == 401
    ok = await client.post(telegram.WEBHOOK_PATH, json={"update_id": 1}, headers=_hook_headers("generated-at-startup"))
    assert ok.status == 200
    ctx.webhook_secret = None
    assert (
        await client.post(telegram.WEBHOOK_PATH, json={"update_id": 2}, headers=_hook_headers(fallback))
    ).status == 200
    await _drain(ctx, client)
