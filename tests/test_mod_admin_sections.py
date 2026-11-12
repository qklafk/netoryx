"""Bot admin sections: subscriptions (bulk actions), plans and ad campaigns."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import select

from app.core.locks import LockManager
from app.db.models import (
    AdminAuditLog,
    AdminRole,
    Panel,
    PanelKind,
    Plan,
    Provision,
    ProvisionState,
    Subscription,
    SubscriptionStatus,
)
from app.db.session import SessionFactory
from app.modules.admin_subscriptions import admin_bot as subs_bot
from app.services import campaigns
from tests.fakes import FakePanel
from tests.test_mod_admin_support import USER, Builder, add_admin, add_subscription, add_user


@pytest.fixture
def make(session_factory: SessionFactory, tmp_path: Path) -> Iterator[Builder]:
    builder = Builder(session_factory, tmp_path)
    yield builder
    builder.close()


async def _audit(sf: SessionFactory) -> list[str]:
    async with sf() as s:
        return list(await s.scalars(select(AdminAuditLog.action).order_by(AdminAuditLog.id)))


async def _wait_jobs() -> None:
    while subs_bot.TASKS:
        await asyncio.gather(*list(subs_bot.TASKS))


# --- subscriptions ---------------------------------------------------------------------------------------------------


async def test_overview_and_expiring_list(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    now = datetime.now(UTC).replace(microsecond=0)
    for uid, days in ((USER, 2), (5002, 5), (5003, 40)):
        await add_user(session_factory, uid)
        await add_subscription(session_factory, uid, expires_at=now + timedelta(days=days))
    await add_user(session_factory, 5004)
    await add_subscription(session_factory, 5004, expires_at=now - timedelta(days=2), status=SubscriptionStatus.EXPIRED)
    await h.press("nav:adm_subs:")
    text = h.last_text()
    assert "Активных: <b>3</b>" in text and "за 3 дня: 1" in text and "за 7 дней: 2" in text
    assert "за последние 30 дней: 1" in text
    await h.press("adms:exp:0")
    labels = list(h.buttons())
    assert any(str(USER) in label for label in labels) and any("5002" in label for label in labels)
    assert not any("5003" in label for label in labels)
    assert h.buttons()[next(label for label in labels if str(USER) in label)] == f"nav:adm_user:{USER}"


async def test_bulk_compensation(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    h.ctx.locks = LockManager(session_factory)
    now = datetime.now(UTC).replace(microsecond=0)
    for uid in (USER, 5002):
        await add_user(session_factory, uid)
        await add_subscription(session_factory, uid, expires_at=now + timedelta(days=3))
    await add_user(session_factory, 5003)
    await add_subscription(session_factory, 5003, expires_at=now - timedelta(days=1), status=SubscriptionStatus.EXPIRED)

    await h.press("adms:comp:")
    await h.message("0")
    assert "целое число" in h.last_text()
    await h.message("7")
    assert "<b>7 дней</b>" in h.last_text() and "(2 шт.)" in h.last_text()
    yes = h.buttons()["✅ Да"]
    await h.press(yes)
    await h.press(yes)  # double tap does not start a second run
    await _wait_jobs()
    assert "Готово" in h.last_text()
    async with session_factory() as s:
        subs = {sub.user_id: sub for sub in await s.scalars(select(Subscription))}
    assert subs[USER].expires_at - now >= timedelta(days=10) - timedelta(seconds=5)
    assert subs[USER].expires_at - now < timedelta(days=11)
    assert subs[5003].status == SubscriptionStatus.EXPIRED
    assert (await _audit(session_factory)).count("subscriptions.compensate") == 1


async def test_bulk_resync_marks_everything(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    sub = await add_subscription(session_factory)
    async with session_factory.begin() as s:
        s.add(Panel(id=1, name="P", kind=PanelKind.REMNAWAVE, url="https://p"))
    async with session_factory.begin() as s:
        s.add(
            Provision(
                subscription_id=sub.id,
                panel_id=1,
                inbound_key=0,
                remote_email="x",
                desired_state=ProvisionState.ENABLED,
                actual_state=ProvisionState.ENABLED,
                synced_revision=1,
            )
        )
    h.ctx.panels.backends[1] = FakePanel(kind=PanelKind.REMNAWAVE, inbounds=[])
    await h.press("adms:sync:")
    await h.press(h.buttons()["✅ Да"])
    await _wait_jobs()
    assert "Отмечено к синхронизации: 1" in h.last_text()
    assert "subscriptions.resync_all" in await _audit(session_factory)


async def test_subscriptions_section_needs_permission(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_admin(session_factory, 2003, AdminRole.SUPPORT)
    h.reset()
    await h.press("nav:adm_subs:", uid=2003)
    assert not h.calls(EditMessageText) and not h.calls(SendMessage)


# --- plans -----------------------------------------------------------------------------------------------------------


async def test_create_plan_step_by_step(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await h.press("nav:adm_plans:")
    assert "Тарифов пока нет" in h.last_text()
    await h.press("admp:new:0:")
    for answer in ("1 месяц", "30", "149", "x", "3", "0"):
        await h.message(answer)
    assert "Тариф создан" in h.last_text()
    async with session_factory() as s:
        plan = await s.scalar(select(Plan))
    assert plan is not None
    assert (plan.title, plan.duration_days, plan.price_kop, plan.device_limit, plan.traffic_limit_gb) == (
        "1 месяц",
        30,
        14900,
        3,
        0,
    )
    assert "plan.create" in await _audit(session_factory)


async def test_edit_toggle_and_reorder_plans(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    async with session_factory.begin() as s:
        first = Plan(title="A", duration_days=30, price_kop=10000, sort_order=0)
        second = Plan(title="B", duration_days=90, price_kop=25000, sort_order=1)
        s.add_all([first, second])
    await h.press(f"admp:edit:{second.id}:price_kop")
    await h.message("199,90")
    await h.press(f"admp:edit:{second.id}:price_stars")
    await h.message("120")
    await h.press(f"admp:toggle:{second.id}:is_hidden")
    assert "start=plan_" in h.last_text()
    await h.press(f"admp:toggle:{second.id}:includes_tg_proxy")
    await h.press(f"admp:toggle:{second.id}:is_active")
    await h.press(f"admp:up:{second.id}:")
    async with session_factory() as s:
        b = await s.get(Plan, second.id)
        a = await s.get(Plan, first.id)
    assert b is not None and a is not None
    assert b.price_kop == 19990 and b.price_stars == 120
    assert b.is_hidden and not b.includes_tg_proxy and not b.is_active
    assert b.sort_order < a.sort_order
    await h.press(f"admp:edit:{second.id}:duration_days")
    await h.message("0")
    assert "не подходит" in h.last_text()
    assert "plan.reorder" in await _audit(session_factory)


async def test_plan_card_shows_stats(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    async with session_factory.begin() as s:
        plan = Plan(title="Год", duration_days=365, price_kop=99000)
        s.add(plan)
    await add_user(session_factory)
    await add_subscription(session_factory, plan_id=plan.id)
    await h.press(f"admp:card:{plan.id}:")
    text = h.last_text()
    assert "Год" in text and "Активных подписок: 1" in text and "1 год" in text


# --- campaigns -------------------------------------------------------------------------------------------------------


async def test_campaign_create_stats_and_pause(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await h.press("nav:adm_camp:")
    assert "Создайте кампанию" in h.last_text()
    await h.press("admk:new:")
    await h.message("!")
    assert "Код не подходит" in h.last_text()
    await h.message("vk_nov Реклама ВК")
    text = h.last_text()
    assert "Кампания создана" in text and "https://t.me/test_bot?start=c_VK_NOV" in text
    await h.message("/admin")
    await h.press("admk:new:")
    await h.message("VK_NOV again")
    assert "уже есть" in h.last_text()

    await add_user(session_factory)
    async with session_factory.begin() as s:
        assert await campaigns.attribute(s, USER, "vk_nov", is_new=True)
    await h.press("admk:card:VK_NOV")
    assert "Регистраций: <b>1</b>" in h.last_text()
    await h.press("admk:pause:VK_NOV")
    assert "на паузе" in h.last_text()
    await h.press("admk:resume:VK_NOV")
    assert "идёт" in h.last_text()
    await h.press("nav:adm_camp:")
    assert any("VK_NOV" in label for label in h.buttons())


async def _expiry(sf: SessionFactory) -> dict[int, datetime]:
    async with sf() as s:
        return {sub.user_id: sub.expires_at for sub in await s.scalars(select(Subscription))}


async def test_compensation_failure_midway_resumes_without_double_grant(
    make: Builder, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.admin_subscriptions import ops
    from app.services import subscriptions as subs_service

    h = make()
    now = datetime.now(UTC).replace(microsecond=0)
    uids = (USER, 5002, 5003, 5004)
    for uid in uids:
        await add_user(session_factory, uid)
        await add_subscription(session_factory, uid, expires_at=now + timedelta(days=3))
    before = await _expiry(session_factory)

    real_grant = subs_service.grant_days
    calls = 0

    async def flaky_grant(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("database is locked")
        return await real_grant(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subs_service, "grant_days", flaky_grant)
    with pytest.raises(RuntimeError):
        await ops.compensate(session_factory, h.ctx.settings, 5, 1, batch_size=2)

    run = await ops.pending_compensation(session_factory)
    assert run is not None and run.days == 5 and run.done == 2 and run.total == 4
    granted = await _expiry(session_factory)
    assert sum(granted[uid] - before[uid] == timedelta(days=5) for uid in uids) == 2
    assert sum(granted[uid] == before[uid] for uid in uids) == 2

    # Starting another compensation while one is unfinished is refused instead of granting twice.
    with pytest.raises(ops.CompensationPending):
        await ops.compensate(session_factory, h.ctx.settings, 5, 1, batch_size=2)

    done = await ops.resume_compensation(session_factory, h.ctx.settings, batch_size=2)
    assert done == 4
    after = await _expiry(session_factory)
    assert all(after[uid] - before[uid] == timedelta(days=5) for uid in uids)
    assert await ops.pending_compensation(session_factory) is None


async def test_bot_compensation_failure_reports_progress_and_offers_resume(
    make: Builder, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import subscriptions as subs_service

    h = make()
    h.ctx.locks = LockManager(session_factory)
    monkeypatch.setattr("app.modules.admin_subscriptions.ops.BATCH_SIZE", 1)
    now = datetime.now(UTC).replace(microsecond=0)
    uids = (USER, 5002, 5003)
    for uid in uids:
        await add_user(session_factory, uid)
        await add_subscription(session_factory, uid, expires_at=now + timedelta(days=3))
    before = await _expiry(session_factory)

    real_grant = subs_service.grant_days
    calls = 0

    async def flaky_grant(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("database is locked")
        return await real_grant(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subs_service, "grant_days", flaky_grant)
    await h.press("adms:comp:")
    await h.message("2")
    await h.press(h.buttons()["✅ Да"])
    await _wait_jobs()
    text = h.last_text()
    assert "1 из 3" in text, text

    # A fresh compensation is not started while this one is unfinished.
    await h.press("adms:comp:")
    assert "1 из 3" in h.last_text()
    resume = next(data for label, data in h.buttons().items() if "Продолжить" in label)
    await h.press(resume)
    await _wait_jobs()
    assert "Компенсация завершена: дни получили 3" in h.last_text()
    after = await _expiry(session_factory)
    assert all(after[uid] - before[uid] == timedelta(days=2) for uid in uids)


async def test_compensation_is_resumed_after_restart(make: Builder, session_factory: SessionFactory) -> None:
    from app.modules.admin_subscriptions import module as subs_module
    from app.modules.admin_subscriptions import ops

    h = make()
    h.ctx.locks = LockManager(session_factory)
    now = datetime.now(UTC).replace(microsecond=0)
    for uid in (USER, 5002):
        await add_user(session_factory, uid)
        await add_subscription(session_factory, uid, expires_at=now + timedelta(days=3))
    before = await _expiry(session_factory)
    run = await ops.start_compensation(session_factory, 4, 1)
    assert run.total == 2 and run.done == 0
    for hook in subs_module.on_startup:
        await hook(h.ctx)  # type: ignore[arg-type]
    await _wait_jobs()
    after = await _expiry(session_factory)
    assert all(after[uid] - before[uid] == timedelta(days=4) for uid in (USER, 5002))
    assert await ops.pending_compensation(session_factory) is None
    for hook in subs_module.on_shutdown:
        await hook(h.ctx)  # type: ignore[arg-type]
