"""Telegram proxy module: the user screen (connect, QR, copy, new link, FAQ) and the scan / reconcile jobs."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import Location, Panel, PanelKind, TgProxyAccess
from app.db.session import SessionFactory
from app.keyboards import Nav
from app.modules.tg_proxy import module as proxy_module
from app.modules.tg_proxy.bot import wait_text
from app.modules.tg_proxy.callbacks import ProxyCb
from app.modules.tg_proxy.jobs import detector_for, reconcile_job, scan_job
from app.services import notifications
from tests.test_middlewares_support import callback_update, message_update
from tests.test_mod_personal_support import OWNER, Harness, add_user, harnesses
from tests.test_tgproxy_service import FakeProxyPanel

USER = 5001


@pytest.fixture
async def harness_factory(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Any]:
    async with harnesses(session_factory, tmp_path) as factory:
        yield factory


async def _proxy_world(sf: SessionFactory, h: Harness, *, panels: int = 1) -> list[FakeProxyPanel]:
    fakes: list[FakeProxyPanel] = []
    async with sf.begin() as session:
        location = Location(name="Германия", flag="🇩🇪", sort_order=0)
        session.add(location)
        await session.flush()
        rows = []
        for index in range(panels):
            options: dict[str, Any] = {"location_id": location.id} if index == 0 else {}
            if index == 0:
                options["web_host"] = "web.example.com"
            rows.append(
                Panel(
                    name=f"tg-{index}", kind=PanelKind.TELEMT, url=f"http://t{index}:9091", options=json.dumps(options)
                )
            )
        session.add_all(rows)
        await session.flush()
        for row in rows:
            fake = FakeProxyPanel(host=f"{row.name}.example.com")
            h.ctx.panels.panels[row.id] = fake
            fakes.append(fake)
    await add_user(sf, USER)
    await add_user(sf, OWNER)
    return fakes


async def test_screen_creates_and_shows_personal_links(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(proxy_module)
    (fake,) = await _proxy_world(session_factory, h)
    await h.feed(callback_update(USER, Nav(to="tg_proxy").pack()))

    assert [c[0] for c in fake.calls] == ["upsert"]
    text = h.last_text()
    assert "Прокси для Telegram" in text and "🟢 🇩🇪 Германия" in text
    connect = [b for b in h.buttons() if b.url and b.url.startswith("https://t.me/proxy?")]
    assert len(connect) == 1 and "Подключить" in connect[0].text
    copy = [b for b in h.buttons() if b.copy_text is not None]
    assert copy and copy[0].copy_text.text.startswith("tg://proxy?server=tg-0.example.com")
    assert any(b.url and b.url.startswith("tg://webproxy?") for b in h.buttons())
    assert h.find("tgp:qr:0") and h.find("tgp:rotate")

    # Opening again does not push anything new; /proxy shows the same screen.
    await h.feed(message_update(USER, "/proxy"))
    assert [c[0] for c in fake.calls] == ["upsert"]
    assert "Германия" in h.last_text()


async def test_backup_server_is_labelled(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(proxy_module)
    await _proxy_world(session_factory, h, panels=2)
    await h.feed(callback_update(USER, Nav(to="proxy").pack()))
    assert "запасной" in h.last_text()
    assert len([b for b in h.buttons() if b.url and b.url.startswith("https://t.me/proxy?")]) == 2


async def test_qr_faq_and_english(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(proxy_module)
    await _proxy_world(session_factory, h)
    async with session_factory.begin() as session:
        from app.db.models import User

        user = await session.get(User, USER)
        assert user is not None
        user.language = "en"
    await h.feed(callback_update(USER, Nav(to="tg_proxy").pack()))
    assert "Your servers" in h.last_text()
    await h.feed(callback_update(USER, ProxyCb(action="qr", arg=0).pack()))
    (photo,) = h.photos()
    assert "Scan this code" in photo.caption
    await h.feed(callback_update(USER, ProxyCb(action="faq").pack()))
    assert "How it works" in h.last_text()
    # A QR for a server the user does not have falls back to the screen.
    await h.feed(callback_update(USER, ProxyCb(action="qr", arg=7).pack()))
    assert "Your servers" in h.last_text()


async def test_rotation_with_confirmation_and_cooldown(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(proxy_module, tg_proxy_rotate_cooldown_min=60)
    (fake,) = await _proxy_world(session_factory, h)
    await h.feed(callback_update(USER, Nav(to="tg_proxy").pack()))
    old_key = fake.account(f"tg{USER}").key

    await h.feed(callback_update(USER, ProxyCb(action="rotate").pack()))
    assert "новую ссылку" in h.last_text()
    await h.feed(callback_update(USER, ProxyCb(action="rotate_ok").pack()))
    assert fake.account(f"tg{USER}").key != old_key
    assert "✅ Новая ссылка готова" in h.popups()

    await h.feed(callback_update(USER, ProxyCb(action="rotate_ok").pack()))
    assert any(p and p.startswith("⏳ Ссылку недавно меняли") for p in h.popups())


async def test_unavailable_and_no_servers(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(proxy_module, tg_proxy_access="off")
    await add_user(session_factory, USER)
    await h.feed(callback_update(USER, Nav(to="tg_proxy").pack()))
    assert "недоступен" in h.last_text()

    h.close()
    h2: Harness = harness_factory(proxy_module)
    await h2.feed(callback_update(USER, Nav(to="tg_proxy").pack()))
    assert "нет свободных прокси-серверов" in h2.last_text()


async def test_staff_get_a_proxy_even_when_it_is_off(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(proxy_module, tg_proxy_access="off")
    (fake,) = await _proxy_world(session_factory, h)
    await h.feed(callback_update(OWNER, Nav(to="tg_proxy").pack()))
    assert fake.accounts and "🟢" in h.last_text()


async def test_free_limits_are_explained(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(proxy_module, tg_proxy_free_rate_limit_mbps=3, tg_proxy_free_ad_tag="a" * 32)
    await _proxy_world(session_factory, h)
    await h.feed(callback_update(USER, Nav(to="tg_proxy").pack()))
    text = h.last_text()
    assert "до 3 Мбит/с" in text and "спонсора" in text


async def test_scan_rotates_shared_links_and_tells_everyone(
    harness_factory: Any, session_factory: SessionFactory
) -> None:
    h: Harness = harness_factory(proxy_module, tg_proxy_abuse_strikes=2, tg_proxy_max_unique_ips=3)
    (fake,) = await _proxy_world(session_factory, h)
    await h.feed(callback_update(USER, Nav(to="tg_proxy").pack()))
    name = f"tg{USER}"
    fake.accounts[(None, name)] = replace(fake.account(name), recent_unique_ips=9)

    await scan_job(h.ctx)  # type: ignore[arg-type]
    assert not h.sent_to(USER)  # one strike is not enough
    await scan_job(h.ctx)  # type: ignore[arg-type]
    (notice,) = h.sent_to(USER)
    assert "9 адресов" in notice
    await notifications.close(h.ctx, drain_seconds=2)  # type: ignore[arg-type]
    assert any("выпущена новая ссылка" in t for t in h.sent_to(OWNER))

    old_key = fake.account(name).key
    await reconcile_job(h.ctx)  # type: ignore[arg-type]
    assert fake.account(name).key != old_key  # the new secret reached the server


def test_detector_is_per_application_and_follows_settings(harness_factory: Any) -> None:
    h: Harness = harness_factory(proxy_module, tg_proxy_abuse_strikes=4)
    detector = detector_for(h.ctx)  # type: ignore[arg-type]
    assert detector is detector_for(h.ctx) and detector.strikes == 4  # type: ignore[arg-type]
    h.ctx.current = h.ctx.current.model_copy(update={"tg_proxy_abuse_strikes": 2})
    assert detector_for(h.ctx).strikes == 2  # type: ignore[arg-type]


@pytest.mark.parametrize(("minutes", "expected"), [(1, "1 минута"), (45, "45 минут"), (150, "3 часа")])
def test_wait_text(minutes: int, expected: str, tmp_path: Any) -> None:
    from datetime import timedelta

    from tests.test_mod_personal_support import module_i18n

    _ = module_i18n(tmp_path, []).translator("ru")
    assert wait_text(_, timedelta(minutes=minutes)) == expected


async def test_module_is_switchable(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(proxy_module, disabled_modules=["tg_proxy"])
    await _proxy_world(session_factory, h)
    await h.feed(callback_update(USER, Nav(to="tg_proxy").pack()))
    async with session_factory() as session:
        assert (await session.scalars(select(TgProxyAccess))).first() is None
