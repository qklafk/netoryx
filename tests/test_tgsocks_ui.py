"""SOCKS5 proxy in the bot screen and the Mini App API, next to the MTProto links."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.db.models import Inbound, Location, Panel, PanelKind, TgSocksAccess, User
from app.db.session import SessionFactory
from app.keyboards import Nav
from app.modules.tg_proxy import module as proxy_module
from app.modules.tg_proxy.callbacks import ProxyCb
from tests.test_middlewares_support import callback_update
from tests.test_mod_miniapp_support import USER_ID, Env, miniapp_env
from tests.test_mod_miniapp_support import add_user as add_miniapp_user
from tests.test_mod_personal_support import Harness, add_user, harnesses
from tests.test_tgproxy_service import FakeProxyPanel
from tests.test_tgsocks_service import FakeSocksPanel

USER = 5001
MIXED = {"protocol": "mixed", "port": 1080, "local": True, "panel_enabled": True, "tg_socks": True}


async def _socks_world(sf: SessionFactory, panels: Any, *, with_mtproto: bool = False) -> FakeSocksPanel:
    """One 3x-ui panel with a selected mixed inbound (and optionally a telemt MTProto panel)."""
    async with sf.begin() as session:
        location = Location(name="Германия", flag="🇩🇪", sort_order=0)
        session.add(location)
        await session.flush()
        xui = Panel(
            name="xui-de",
            kind=PanelKind.XUI,
            url="https://de.example.com:2053/p",
            options=json.dumps({"tg_socks_host": "socks.example.com"}),
        )
        session.add(xui)
        if with_mtproto:
            telemt = Panel(
                name="tg-0",
                kind=PanelKind.TELEMT,
                url="http://t0:9091",
                options=json.dumps({"location_id": location.id}),
            )
            session.add(telemt)
        await session.flush()
        session.add(Inbound(panel_id=xui.id, remote_id=5, name="s", params=json.dumps(MIXED), location_id=location.id))
        fake = FakeSocksPanel()
        panels[xui.id] = fake
        if with_mtproto:
            panels[telemt.id] = FakeProxyPanel(host="tg-0.example.com")
    return fake


# ---------------------------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------------------------
@pytest.fixture
async def harness_factory(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Any]:
    async with harnesses(session_factory, tmp_path) as factory:
        yield factory


async def test_screen_shows_socks_credentials_link_and_qr(
    harness_factory: Any, session_factory: SessionFactory
) -> None:
    h: Harness = harness_factory(proxy_module)
    await add_user(session_factory, USER)
    fake = await _socks_world(session_factory, h.ctx.panels.panels)
    await h.feed(callback_update(USER, Nav(to="tg_proxy").pack()))

    password = fake.accounts[5][f"tg{USER}"]
    text = h.last_text()
    assert "SOCKS5-прокси" in text
    assert f"<code>tg{USER}</code>" in text and f"<code>{password}</code>" in text
    assert "<code>socks.example.com</code>" in text and "<code>1080</code>" in text
    (connect,) = [b for b in h.buttons() if b.url and b.url.startswith("https://t.me/socks?")]
    query = parse_qs(urlsplit(connect.url).query)
    assert query["user"] == [f"tg{USER}"] and query["pass"] == [password] and query["server"] == ["socks.example.com"]
    assert "Здесь нет серверов" not in text and "Сейчас нет свободных" not in text
    assert h.find("tgp:qrs:0") and h.find("tgp:rotate")

    await h.feed(callback_update(USER, ProxyCb(action="qrs", arg=0).pack()))
    (photo,) = h.photos()
    assert "SOCKS5" in photo.caption
    # A QR for a server the user does not have falls back to the screen.
    await h.feed(callback_update(USER, ProxyCb(action="qrs", arg=4).pack()))
    assert "SOCKS5-прокси" in h.last_text()


async def test_screen_shows_both_kinds_and_english(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(proxy_module)
    await add_user(session_factory, USER, language="en")
    await _socks_world(session_factory, h.ctx.panels.panels, with_mtproto=True)
    await h.feed(callback_update(USER, Nav(to="tg_proxy").pack()))
    text = h.last_text()
    assert "Your servers" in text and "SOCKS5 proxy" in text and "Login:" in text
    urls = [b.url for b in h.buttons() if b.url]
    assert any(u.startswith("https://t.me/proxy?") for u in urls) and any(
        u.startswith("https://t.me/socks?") for u in urls
    )


async def test_screen_without_socks_inbound_is_unchanged(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(proxy_module)
    await add_user(session_factory, USER)
    async with session_factory.begin() as session:
        panel = Panel(name="tg-0", kind=PanelKind.TELEMT, url="http://t0:9091")
        session.add(panel)
        await session.flush()
        h.ctx.panels.panels[panel.id] = FakeProxyPanel()
    await h.feed(callback_update(USER, Nav(to="tg_proxy").pack()))
    assert "SOCKS5-прокси" not in h.last_text()
    assert not [b for b in h.buttons() if b.url and "t.me/socks" in b.url]


async def test_new_link_changes_the_socks_password_too(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(proxy_module, tg_proxy_rotate_cooldown_min=60)
    await add_user(session_factory, USER)
    fake = await _socks_world(session_factory, h.ctx.panels.panels)
    await h.feed(callback_update(USER, Nav(to="tg_proxy").pack()))
    old = fake.accounts[5][f"tg{USER}"]

    await h.feed(callback_update(USER, ProxyCb(action="rotate_ok").pack()))
    new = fake.accounts[5][f"tg{USER}"]
    assert new != old and f"<code>{new}</code>" in h.last_text()
    assert "✅ Новая ссылка готова" in h.popups()
    await h.feed(callback_update(USER, ProxyCb(action="rotate_ok").pack()))
    assert any(p and p.startswith("⏳ Ссылку недавно меняли") for p in h.popups())
    assert fake.accounts[5][f"tg{USER}"] == new


# ---------------------------------------------------------------------------------------------
# Mini App
# ---------------------------------------------------------------------------------------------
@pytest.fixture
async def env(tmp_path: Path, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    async with miniapp_env(tmp_path, session_factory, monkeypatch) as environment:
        await add_miniapp_user(environment.ctx.session_factory, environment.settings)
        yield environment


async def test_miniapp_returns_socks_accounts_and_rotates_them(env: Env, session_factory: SessionFactory) -> None:
    fake = await _socks_world(session_factory, env.ctx.panels.panels)
    body = (await env.get("/tg-proxy")).json()
    assert body["allowed"] is True and body["servers"] == []
    (socks,) = body["socks"]
    password = fake.accounts[5][f"tg{USER_ID}"]
    assert socks["ready"] is True and socks["title"] == "🇩🇪 Германия"
    link = socks["link"]
    assert (link["server"], link["port"], link["user"], link["password"]) == (
        "socks.example.com",
        1080,
        f"tg{USER_ID}",
        password,
    )
    assert link["tme_url"].startswith("https://t.me/socks?server=socks.example.com&port=1080")
    assert link["tg_url"].startswith("tg://socks?") and socks["qr"].startswith("data:image/png")

    again = (await env.get("/tg-proxy")).json()  # served from the database
    assert again["socks"][0]["link"]["password"] == password
    assert sum(1 for c in fake.calls if c[0] == "apply") == 1

    rotated = await env.post("/tg-proxy/rotate")
    assert rotated.status_code == 200
    new = rotated.json()["socks"][0]["link"]["password"]
    assert new != password and fake.accounts[5][f"tg{USER_ID}"] == new
    assert (await env.post("/tg-proxy/rotate")).status_code == 429


async def test_miniapp_without_targets_has_an_empty_socks_list(env: Env) -> None:
    body = (await env.get("/tg-proxy")).json()
    assert body["socks"] == []
    async with env.ctx.session_factory() as session:
        assert (await session.scalars(select(TgSocksAccess))).first() is None
        assert await session.get(User, USER_ID) is not None


async def test_faq_mentions_socks_only_for_users_who_have_it(
    harness_factory: Any, session_factory: SessionFactory
) -> None:
    h: Harness = harness_factory(proxy_module)
    await add_user(session_factory, USER)
    await h.feed(callback_update(USER, ProxyCb(action="faq").pack()))
    assert "Что такое SOCKS5" not in h.last_text()
    await _socks_world(session_factory, h.ctx.panels.panels)
    await h.feed(callback_update(USER, Nav(to="tg_proxy").pack()))
    await h.feed(callback_update(USER, ProxyCb(action="faq").pack()))
    assert "Что такое SOCKS5" in h.last_text()
