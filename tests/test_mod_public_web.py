"""Public web module: /sub (apps and browsers), /open import pages, /gift landing, the Remnawave webhook."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.db.models import (
    Endpoint,
    EndpointKind,
    EndpointProfile,
    Inbound,
    Location,
    Panel,
    PanelKind,
    Plan,
    PromoCode,
    PromoKind,
    Provision,
    ProvisionState,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.db.session import SessionFactory
from app.modules.public_web import module as public_module
from app.services import kv, notifications, subscription_page, tg_proxy
from app.services.deeplinks import incy_decrypt_link
from app.utils.time import utcnow
from app.web.app import create_web_app
from tests.fakes import FakePanel
from tests.test_mod_personal_support import BOX, OWNER, ModCtx, make_ctx
from tests.test_tgproxy_service import FakeProxyPanel
from tests.test_web_support import AsgiClient

USER = 42
TOKEN = "tok-123"
SECRET = "hook-secret"
BROWSER = {
    "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Safari",
    "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
}
REALITY = {
    "protocol": "vless",
    "port": 443,
    "type": "tcp",
    "security": "reality",
    "pbk": "PUBKEY",
    "sid": "6ba85179",
    "sni": "www.example.ru",
    "fp": "chrome",
    "spx": "/",
    "flow_allowed": True,
}


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    subscription_page.clear_caches()


async def _add(sf: SessionFactory, *rows: Any) -> None:
    async with sf.begin() as session:
        for row in rows:
            session.add(row)
            await session.flush()


@pytest.fixture
async def web(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[tuple[ModCtx, AsgiClient, Any]]:
    ctx, api = make_ctx(session_factory, tmp_path, [public_module], remnawave_webhook_secret=SECRET)
    await _add(
        session_factory,
        User(tg_id=USER, referral_code="R42", sub_token=TOKEN, language="ru", first_name="Ann"),
        User(tg_id=OWNER, referral_code="R1000", sub_token="owner-token", language="ru"),
        Subscription(
            id=1,
            user_id=USER,
            client_uuid="6f1c1c2e-8d1f-4b6c-9a51-3e0e2c4d5f60",
            status=SubscriptionStatus.ACTIVE,
            expires_at=utcnow() + timedelta(days=10),
            device_limit=3,
        ),
        Location(id=1, name="Германия", flag="🇩🇪"),
        Panel(id=1, name="xui", kind=PanelKind.XUI, url="https://xui.example:2053/p"),
        Inbound(id=1, panel_id=1, remote_id=7, location_id=1, name="de", params=json.dumps(REALITY)),
        Endpoint(id=1, inbound_id=1, kind=EndpointKind.DIRECT, profile=EndpointProfile.STANDARD, address="203.0.113.1"),
    )
    ctx.panels.panels[1] = FakePanel()
    app = create_web_app(ctx, [public_module])  # type: ignore[arg-type]
    yield ctx, AsgiClient(app, host="bot.example.com"), api
    await notifications.close(ctx, drain_seconds=1)  # type: ignore[arg-type]


async def test_apps_get_the_raw_subscription(web: tuple[ModCtx, AsgiClient, Any]) -> None:
    _ctx, client, _api = web
    resp = await client.get(f"/sub/{TOKEN}", headers={"user-agent": "Happ/3.2.1", "accept": "*/*"})
    assert resp.status == 200
    assert resp.headers["profile-title"].startswith("base64:") and resp.headers["cache-control"] == "no-store"
    assert resp.headers["content-type"].startswith("application/json")
    configs = json.loads(resp.body)
    assert len(configs) == 1 and configs[0]["outbounds"][0]["settings"]["vnext"][0]["address"] == "203.0.113.1"
    # Apps without JSON support get the base64 link list; a browser can ask for it explicitly.
    plain = await client.get(f"/sub/{TOKEN}", headers={"user-agent": "Hiddify/2.5", "accept": "*/*"})
    links = base64.b64decode(plain.body).decode().splitlines()
    assert len(links) == 1 and links[0].startswith("vless://") and "203.0.113.1" in links[0]
    raw = await client.get(f"/sub/{TOKEN}?raw=1", headers=BROWSER)
    assert raw.body == plain.body
    head = await client.request("HEAD", f"/sub/{TOKEN}", headers={"user-agent": "v2rayNG/1.9"})
    assert head.status == 200


async def test_whitelist_url_and_unknown_tokens(web: tuple[ModCtx, AsgiClient, Any]) -> None:
    _ctx, client, _api = web
    wl = await client.get(f"/sub/{TOKEN}/wl", headers={"user-agent": "Happ/3.2.1"})
    assert wl.status == 200 and base64.b64decode(wl.body) == b""
    assert (await client.get("/sub/nope")).status == 404
    assert (await client.get("/sub/bad%20token!")).status == 404


async def test_unavailable_servers_answer_503(
    web: tuple[ModCtx, AsgiClient, Any], session_factory: SessionFactory
) -> None:
    ctx, client, _api = web
    async with session_factory.begin() as session:
        (await session.get(Endpoint, 1)).is_active = False  # type: ignore[union-attr]
    await _add(
        session_factory,
        Panel(id=2, name="rw", kind=PanelKind.REMNAWAVE, url="http://rw:3000"),
        Provision(
            subscription_id=1,
            panel_id=2,
            inbound_key=0,
            remote_email="tg_42",
            remote_id="5",
            desired_state=ProvisionState.ENABLED,
            actual_state=ProvisionState.ENABLED,
        ),
    )
    ctx.panels.panels[2] = FakePanel(kind=PanelKind.REMNAWAVE, down=True)
    resp = await client.get(f"/sub/{TOKEN}", headers={"user-agent": "Happ/3.2.1"})
    assert resp.status == 503 and resp.headers["retry-after"] == "300"


async def test_browsers_get_a_page(web: tuple[ModCtx, AsgiClient, Any], session_factory: SessionFactory) -> None:
    _ctx, client, _api = web
    resp = await client.get(f"/sub/{TOKEN}", headers=BROWSER)
    assert resp.status == 200 and resp.headers["content-type"].startswith("text/html")
    html = resp.text
    assert "Подписка активна до" in html and f"/open/happ/{TOKEN}" in html and "data:image/png;base64," in html
    assert f"https://bot.example.com/sub/{TOKEN}" in html and "t.me/test_bot" in html
    csp = resp.headers["content-security-policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in html and "default-src 'none'" in csp
    assert "Прокси для Telegram" not in html

    unknown = await client.get("/sub/unknown-token", headers=BROWSER)
    assert unknown.status == 404 and "Ссылка не найдена" in unknown.text


async def test_page_shows_the_telegram_proxy(
    web: tuple[ModCtx, AsgiClient, Any], session_factory: SessionFactory
) -> None:
    ctx, client, _api = web
    await _add(session_factory, Panel(id=3, name="tg", kind=PanelKind.TELEMT, url="http://telemt:9091"))
    ctx.panels.panels[3] = FakeProxyPanel()
    await tg_proxy.open_proxy(session_factory, ctx.panels, ctx.settings, BOX, USER)
    html = (await client.get(f"/sub/{TOKEN}", headers=BROWSER)).text
    assert "Прокси для Telegram" in html and "tg://proxy?server=proxy.example.com" in html


async def test_expired_subscription_page_in_english(
    web: tuple[ModCtx, AsgiClient, Any], session_factory: SessionFactory
) -> None:
    _ctx, client, _api = web
    async with session_factory.begin() as session:
        sub = await session.get(Subscription, 1)
        user = await session.get(User, USER)
        assert sub is not None and user is not None
        sub.expires_at = utcnow() - timedelta(days=1)
        user.language = "en"
    html = (await client.get(f"/sub/{TOKEN}", headers=BROWSER)).text
    assert "Your subscription has ended" in html


async def test_open_pages(web: tuple[ModCtx, AsgiClient, Any]) -> None:
    _ctx, client, _api = web
    happ = await client.get(f"/open/happ/{TOKEN}", headers=BROWSER)
    assert happ.status == 200
    assert f"happ://add/https://bot.example.com/sub/{TOKEN}" in happ.text and "Открываем Happ" in happ.text
    assert "apps.apple.com" in happ.text and "window.location.href" in happ.text

    incy = await client.get(f"/open/incy/{TOKEN}")
    link = incy.text.split('id="open" href="')[1].split('"')[0]
    assert link.startswith("incy://crypt1/")
    assert incy_decrypt_link(link)[0] == f"https://bot.example.com/sub/{TOKEN}"

    v2 = await client.get(f"/open/v2rayng/{TOKEN}")
    assert "v2rayng://install-config?url=" in v2.text
    assert (await client.get(f"/open/unknown/{TOKEN}")).status == 404
    assert (await client.get("/open/happ/nope")).status == 404


async def test_gift_landing(web: tuple[ModCtx, AsgiClient, Any], session_factory: SessionFactory) -> None:
    _ctx, client, _api = web
    await _add(
        session_factory,
        Plan(id=5, title="3 месяца", duration_days=90, price_kop=40000),
        PromoCode(code="GIFTCODE1234", kind=PromoKind.GIFT, plan_id=5, bonus_days=90, max_uses=1),
        PromoCode(code="USEDGIFT0000", kind=PromoKind.GIFT, plan_id=5, bonus_days=90, max_uses=1, uses=1),
    )
    resp = await client.get("/gift/giftcode1234", headers=BROWSER)
    assert resp.status == 200 and "Вам подарили VPN «TestVPN» — 3 месяца" in resp.text
    assert "https://t.me/test_bot?start=gift_GIFTCODE1234" in resp.text
    used = await client.get("/gift/USEDGIFT0000", headers={"accept-language": "en"})
    assert used.status == 404 and "already been claimed" in used.text
    assert (await client.get("/gift/x")).status == 404


def _signed(body: dict[str, Any], secret: str = SECRET) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"x-remnawave-signature": signature, "content-type": "application/json"}


def _user_event(event: str, **extra: Any) -> dict[str, Any]:
    user = {"id": 5, "username": "tg_42", "telegramId": USER, "status": "LIMITED"}
    if event.startswith("user_hwid_devices"):
        return {"scope": "user_hwid_devices", "event": event, "data": {"user": user, **extra}}
    return {"scope": "user", "event": event, "data": {**user, **extra}}


async def test_webhook_auth_and_dedup(web: tuple[ModCtx, AsgiClient, Any], session_factory: SessionFactory) -> None:
    ctx, client, api = web
    raw, headers = _signed(_user_event("user.limited"))
    forged = {**headers, "x-remnawave-signature": "0" * 64}
    assert (await client.post("/webhooks/remnawave", body=raw, headers=forged)).status == 401
    other_raw, other_headers = _signed(_user_event("user.limited"), "another-secret")
    assert (await client.post("/webhooks/remnawave", body=other_raw, headers=other_headers)).status == 401
    assert (
        await client.post("/webhooks/remnawave", body=raw, headers={"content-type": "application/json"})
    ).status == 401
    bad_raw, bad_headers = _signed({"no": "event"})
    assert (await client.post("/webhooks/remnawave", body=bad_raw, headers=bad_headers)).status == 400

    first = await client.post("/webhooks/remnawave", body=raw, headers=headers)
    assert first.status == 200 and first.json() == {"ok": True}
    (notice,) = [c.text for c in api.calls if type(c).__name__ == "SendMessage" and int(c.chat_id) == USER]
    assert "Трафик на этот период закончился" in notice
    again = await client.post("/webhooks/remnawave", body=raw, headers=headers)
    assert again.json() == {"ok": True, "duplicate": True}
    assert len([c for c in api.calls if type(c).__name__ == "SendMessage"]) == 1

    ctx.current = ctx.current.model_copy(update={"remnawave_webhook_secret": None})
    assert (await client.post("/webhooks/remnawave", body=raw, headers=headers)).status == 404


async def test_webhook_user_events(web: tuple[ModCtx, AsgiClient, Any], session_factory: SessionFactory) -> None:
    _ctx, client, api = web
    device = {"hwidUserDevice": {"hwid": "x", "platform": "iOS", "deviceModel": "iPhone 15"}}
    for body in (
        _user_event("user.first_connected"),
        _user_event("user.bandwidth_usage_threshold_reached", lastTriggeredThreshold=80),
        _user_event("user_hwid_devices.added", **device),
        {"scope": "user", "event": "user.limited", "data": {"id": 9, "username": "someone_else"}},
        {"scope": "user", "event": "user.created", "data": {"id": 5, "username": "tg_42"}},
    ):
        raw, headers = _signed(body)
        assert (await client.post("/webhooks/remnawave", body=raw, headers=headers)).status == 200
    texts = [c.text for c in api.calls if type(c).__name__ == "SendMessage" and int(c.chat_id) == USER]
    assert len(texts) == 3
    assert "VPN подключён" in texts[0] and "80%" in texts[1] and "iOS · iPhone 15" in texts[2]


async def test_webhook_admin_alerts(web: tuple[ModCtx, AsgiClient, Any], session_factory: SessionFactory) -> None:
    ctx, client, api = web
    for body in (
        {"scope": "node", "event": "node.connection_lost", "data": {"name": "DE-1", "lastStatusMessage": "timeout"}},
        _user_event("user.expired"),  # still active in the bot: a drift worth a look
    ):
        raw, headers = _signed(body)
        assert (await client.post("/webhooks/remnawave", body=raw, headers=headers)).status == 200
    await notifications.close(ctx, drain_seconds=2)  # type: ignore[arg-type]
    texts = [c.text for c in api.calls if type(c).__name__ == "SendMessage" and int(c.chat_id) == OWNER]
    assert any("нода DE-1 потеряла связь: timeout" in t for t in texts)
    assert any("<code>42</code>" in t and "истёкшую" in t for t in texts)
    assert not [c for c in api.calls if type(c).__name__ == "SendMessage" and int(c.chat_id) == USER]
    async with session_factory() as session:
        assert await kv.get(session, "rwhook:" + hashlib.sha256(raw).hexdigest()[:48])


async def test_webhook_expired_in_both_is_left_to_reminders(
    web: tuple[ModCtx, AsgiClient, Any], session_factory: SessionFactory
) -> None:
    ctx, client, api = web
    async with session_factory.begin() as session:
        sub = await session.get(Subscription, 1)
        assert sub is not None
        sub.expires_at = utcnow() - timedelta(hours=1)
    raw, headers = _signed(_user_event("user.expired"))
    assert (await client.post("/webhooks/remnawave", body=raw, headers=headers)).status == 200
    await notifications.close(ctx, drain_seconds=1)  # type: ignore[arg-type]
    assert not [c for c in api.calls if type(c).__name__ == "SendMessage"]
