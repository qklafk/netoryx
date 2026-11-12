"""Members module (personal mode): list, card, extend, pause/resume, limits, devices, new link, removal."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import AdminAuditLog, Subscription, SubscriptionStatus, User
from app.db.session import SessionFactory
from app.keyboards import Nav
from app.modules.members import module as members_module
from app.modules.members import service
from app.modules.members.callbacks import NAV, UNLIMITED, MemCb
from app.modules.members.jobs import finalize_job
from app.services import invites, provisioning
from app.utils.time import utcnow
from tests.conftest import make_settings
from tests.fakes import FakeCapablePanel
from tests.test_middlewares_support import callback_update
from tests.test_mod_personal_support import OWNER, Harness, harnesses
from tests.test_subs_support import add_sub, add_user, add_xui_panel
from tests.test_tgproxy_service import NOW as PROXY_NOW
from tests.test_tgproxy_service import World as ProxyWorld
from tests.test_tgproxy_service import add_sub as add_proxy_sub
from tests.test_tgproxy_service import world

MEMBER = 6001
__all__ = ["world"]


@pytest.fixture
async def harness_factory(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Any]:
    async with harnesses(session_factory, tmp_path) as factory:
        yield factory


async def _world(sf: SessionFactory, h: Harness, *, members: int = 1) -> FakeCapablePanel:
    fake = FakeCapablePanel()
    async with sf.begin() as session:
        panel, _ = await add_xui_panel(session)
        await add_user(session, OWNER)
        for i in range(members):
            await add_user(session, MEMBER + i, first_name=f"Член{i}")
            sub = await add_sub(session, MEMBER + i, expires_at=(utcnow() + timedelta(days=10)).replace(microsecond=0))
            await provisioning.ensure_provisions(session, sub)
    h.ctx.panels.panels[panel.id] = fake
    async with sf() as session:
        sub_ids = list(await session.scalars(select(Subscription.id)))
    for sub_id in sub_ids:
        await provisioning.sync_subscription(sf, h.ctx.panels, h.ctx.settings, sub_id)
    return fake


async def _sub(sf: SessionFactory, uid: int = MEMBER) -> Subscription | None:
    async with sf() as session:
        return await session.scalar(select(Subscription).where(Subscription.user_id == uid))


async def test_list_and_card(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(members_module, app_mode="personal")
    await _world(session_factory, h, members=10)
    await h.feed(callback_update(OWNER, Nav(to=NAV).pack()))
    assert "Участники</b> · 10" in h.last_text()
    assert MemCb(action="list", page=1).pack() in h.button_data()
    await h.feed(callback_update(OWNER, MemCb(action="card", uid=MEMBER).pack()))
    text = h.last_text()
    assert "Член0" in text and "активен" in text and "осталось" in text


async def test_hidden_in_shop_mode(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(members_module)
    await _world(session_factory, h)
    await h.feed(callback_update(OWNER, Nav(to=NAV).pack()))
    assert not [c for c in h.api.calls if type(c).__name__ in ("SendMessage", "EditMessageText")]


async def test_extend_and_unlimited(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(members_module, app_mode="personal")
    await _world(session_factory, h)
    before = await _sub(session_factory)
    assert before is not None

    await h.feed(callback_update(OWNER, MemCb(action="ext", uid=MEMBER).pack()))
    assert "Продлить доступ" in h.last_text()
    await h.feed(callback_update(OWNER, MemCb(action="ext_ok", uid=MEMBER, value=30).pack()))
    after = await _sub(session_factory)
    assert after is not None and after.expires_at - before.expires_at == timedelta(days=30)
    assert "✅ Доступ продлён" in h.popups()
    assert any("продлён" in t for t in h.sent_to(MEMBER))

    await h.feed(callback_update(OWNER, MemCb(action="ext_ok", uid=MEMBER, value=UNLIMITED).pack()))
    sub = await _sub(session_factory)
    assert sub is not None and invites.is_unlimited(sub) and sub.is_complimentary
    assert "бессрочно" in h.sent_to(MEMBER)[-1]
    assert "бессрочно" in h.last_text()


async def test_pause_resume_and_limits(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(members_module, app_mode="personal", xui_device_limit="ip")
    fake = await _world(session_factory, h)
    await h.feed(callback_update(OWNER, MemCb(action="pause", uid=MEMBER).pack()))
    sub = await _sub(session_factory)
    assert sub is not None and sub.status == SubscriptionStatus.DISABLED
    assert all(not acc.enabled for acc in fake.accounts.values())
    assert "на паузе" in h.last_text() and "приостановлен" in h.sent_to(MEMBER)[-1]

    await h.feed(callback_update(OWNER, MemCb(action="resume", uid=MEMBER).pack()))
    assert all(acc.enabled for acc in fake.accounts.values())

    await h.feed(callback_update(OWNER, MemCb(action="lim", uid=MEMBER).pack()))
    assert "✓ 3" in [b.text for b in h.buttons()]
    await h.feed(callback_update(OWNER, MemCb(action="dev_ok", uid=MEMBER, value=5).pack()))
    await h.feed(callback_update(OWNER, MemCb(action="gb_ok", uid=MEMBER, value=100).pack()))
    sub = await _sub(session_factory)
    assert sub is not None and (sub.device_limit, sub.traffic_limit_gb) == (5, 100)
    assert all(acc.device_limit == 5 for acc in fake.accounts.values())
    assert "✓ 5" in [b.text for b in h.buttons()]
    async with session_factory() as session:
        actions = list(await session.scalars(select(AdminAuditLog.action)))
    assert actions.count("member.limits") == 2 and "subscription.set_status" in actions


async def test_reset_devices_and_new_link(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(members_module, app_mode="personal")
    fake = await _world(session_factory, h)
    await h.feed(callback_update(OWNER, MemCb(action="reset", uid=MEMBER).pack()))
    assert any(call[0] == "reset_devices" for call in fake.calls)
    assert "✅ Устройства сброшены" in h.popups()

    await h.feed(callback_update(OWNER, MemCb(action="rot", uid=MEMBER).pack()))
    assert "новую ссылку" in h.last_text()
    await h.feed(callback_update(OWNER, MemCb(action="rot_ok", uid=MEMBER).pack()))
    async with session_factory() as session:
        user = await session.get(User, MEMBER)
    assert user is not None and user.sub_token != f"token{MEMBER}"
    assert "ссылка подписки обновилась" in h.sent_to(MEMBER)[-1]


async def test_remove_member(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(members_module, app_mode="personal")
    fake = await _world(session_factory, h)
    assert fake.accounts
    await h.feed(callback_update(OWNER, MemCb(action="del", uid=MEMBER).pack()))
    assert "Удалить" in h.last_text()
    await h.feed(callback_update(OWNER, MemCb(action="del_ok", uid=MEMBER).pack()))
    assert "🗑 Участник удалён" in h.popups()
    assert not fake.accounts
    assert await _sub(session_factory) is None
    assert "закрыт" in h.sent_to(MEMBER)[-1]
    assert "Пока здесь никого нет" in h.last_text()


async def test_removal_waits_for_unreachable_servers(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(members_module, app_mode="personal")
    fake = await _world(session_factory, h)
    (panel_id,) = h.ctx.panels.panels
    h.ctx.panels.broken.add(panel_id)
    complete = await service.remove(session_factory, h.ctx.panels, h.ctx.settings, MEMBER, OWNER)
    assert complete is False and fake.accounts
    assert await service.get_member(session_factory, MEMBER) is None  # hidden while being removed
    members, total = await service.list_members(session_factory, limit=10, offset=0)
    assert (members, total) == ([], 0)

    h.ctx.panels.broken.clear()
    sub = await _sub(session_factory)
    assert sub is not None
    await provisioning.sync_subscription(session_factory, h.ctx.panels, h.ctx.settings, sub.id)
    await finalize_job(h.ctx)  # type: ignore[arg-type]
    assert not fake.accounts and await _sub(session_factory) is None


async def test_multi_subscription_member_is_listed_once_and_fully_removed(
    harness_factory: Any, session_factory: SessionFactory
) -> None:
    h: Harness = harness_factory(members_module, app_mode="personal")
    fake = await _world(session_factory, h)
    async with session_factory.begin() as session:
        extra = await add_sub(session, MEMBER, expires_at=(utcnow() + timedelta(days=20)).replace(microsecond=0))
        await provisioning.ensure_provisions(session, extra)
        extra_id = extra.id
    await provisioning.sync_subscription(session_factory, h.ctx.panels, h.ctx.settings, extra_id)

    members, total = await service.list_members(session_factory, limit=10, offset=0)
    assert total == len(members) == 1
    assert await service.remove(session_factory, h.ctx.panels, h.ctx.settings, MEMBER, OWNER)
    assert not fake.accounts
    async with session_factory() as session:
        assert not list(await session.scalars(select(Subscription).where(Subscription.user_id == MEMBER)))


async def test_gone_member_returns_to_the_list(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(members_module, app_mode="personal")
    await _world(session_factory, h)
    await h.feed(callback_update(OWNER, MemCb(action="ext_ok", uid=424242, value=30).pack()))
    assert "Этого участника уже нет в списке" in h.popups()


async def test_proxy_of_a_removed_member_goes_away_once_its_server_is_back(world: ProxyWorld) -> None:
    world.settings = make_settings(app_mode="personal", tg_proxy_access="everyone", admin_ids=[1000])
    await add_proxy_sub(world, 1)
    await world.open(1)
    assert await world.rows(1) and world.telemt.accounts
    world.telemt.fail_next = 100  # the proxy server is down during the removal
    await service.remove(world.session_factory, world.registry, world.settings, 1, 1000, now=PROXY_NOW)
    assert await world.rows(1)

    world.telemt.fail_next = 0
    await world.reconcile(now=PROXY_NOW + timedelta(minutes=10))
    assert await world.rows(1) == []
    assert not world.telemt.accounts
