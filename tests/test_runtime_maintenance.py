from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import AdminAuditLog, User
from app.db.session import SessionFactory
from app.modules.system import jobs as system_jobs
from app.services import kv
from app.services.maintenance import STATE_KEY, MaintenanceService, MaintenanceState, MaintenanceWindow
from tests.conftest import make_settings

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def window(start_min: int, end_min: int, **kw: Any) -> MaintenanceWindow:
    return MaintenanceWindow(
        id=kw.pop("id", "w1"),
        starts_at=NOW + timedelta(minutes=start_min),
        ends_at=NOW + timedelta(minutes=end_min),
        **kw,
    )


# ---------------------------------------------------------------------------------------------
# State (pure)
# ---------------------------------------------------------------------------------------------
def test_state_defaults_and_blocking() -> None:
    state = MaintenanceState()
    assert not state.is_active(NOW)
    assert not state.blocks(1, is_admin=False, now=NOW)
    state.enabled = True
    state.allowlist = [7]
    assert state.blocks(1, is_admin=False, now=NOW)
    assert not state.blocks(1, is_admin=True, now=NOW)
    assert not state.blocks(7, is_admin=False, now=NOW)
    assert state.blocks(None, is_admin=False, now=NOW)


def test_messages_fall_back_between_languages() -> None:
    state = MaintenanceState(enabled=True, message_ru="Работы")
    assert state.message("ru", NOW) == "Работы"
    assert state.message("en", NOW) == "Работы"
    state.message_en = "Works"
    assert state.message("en", NOW) == "Works"
    state.enabled = False
    state.windows = [window(-5, 30, message_en="Window")]
    assert state.message("en", NOW) == "Window"
    assert state.message("ru", NOW) == "Window"
    assert state.effective_eta(NOW) == NOW + timedelta(minutes=30)


def test_windows_and_partial_maintenance() -> None:
    soon = window(30, 90, announce_minutes=60)
    state = MaintenanceState(windows=[soon], disabled_modules=["payments"], allowlist=[5])
    assert not state.is_active(NOW)
    assert state.upcoming(NOW) is soon
    assert state.upcoming(NOW - timedelta(minutes=31)) is None
    assert state.is_active(NOW + timedelta(minutes=30))
    assert state.epoch(NOW + timedelta(minutes=31)) == "window:w1"
    assert not state.is_active(NOW + timedelta(minutes=90))
    assert state.module_blocked("payments", 1, is_admin=False)
    assert not state.module_blocked("payments", 5, is_admin=False)
    assert not state.module_blocked("payments", 1, is_admin=True)
    assert not state.module_blocked("wallet", 1, is_admin=False)


def test_serialization_roundtrip_and_corruption() -> None:
    state = MaintenanceState(
        enabled=True, message_ru="a", eta=NOW, allowlist=[1], disabled_modules=["x"], windows=[window(1, 2)]
    )
    again = MaintenanceState.parse(json.dumps(state.to_json()))
    assert again == state
    assert MaintenanceState.parse("not json") == MaintenanceState()
    assert MaintenanceState.parse("[]") == MaintenanceState()
    assert MaintenanceState.parse(None) == MaintenanceState()
    broken = MaintenanceState.parse(json.dumps({"windows": [{"id": "x"}], "enabled": True}))
    assert broken.enabled and broken.windows == []


# ---------------------------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------------------------
async def test_enable_disable_audited_and_cached(session_factory: SessionFactory) -> None:
    clock = Clock()
    service = MaintenanceService(session_factory, clock=clock)
    other = MaintenanceService(session_factory, clock=clock)
    assert not (await other.state()).enabled
    eta = NOW + timedelta(hours=1)
    state = await service.enable(1000, message_ru=" Обновляемся ", message_en="", eta=eta, source="web", ip="1.1.1.1")
    assert state.enabled and state.message_ru == "Обновляемся" and state.message_en is None
    assert state.started_by == 1000 and state.started_at is not None
    assert (await other.state()).enabled is False  # cached for a few seconds
    clock.now += 11
    assert (await other.state()).enabled is True
    async with session_factory() as s:
        assert await kv.is_maintenance(s)
    state = await service.disable(1000)
    assert not state.enabled and state.started_at is None and state.eta is None
    async with session_factory() as s:
        assert not await kv.is_maintenance(s)
        actions = [a.action for a in await s.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id))]
    assert actions == ["maintenance.enable", "maintenance.disable"]
    with pytest.raises(ValueError):
        await service.enable(1, eta=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        await service.enable(1, message_ru="x" * 1001)


async def test_allowlist_modules_and_message(session_factory: SessionFactory) -> None:
    service = MaintenanceService(session_factory)
    await service.set_allowlist(1, [3, 2, 3])
    await service.set_disabled_modules(1, ["payments", " ", "gifts", "payments"])
    await service.set_message(1, message_ru="ru", message_en="en", eta=NOW)
    state = await service.state(fresh=True)
    assert state.allowlist == [2, 3]
    assert state.disabled_modules == ["gifts", "payments"]
    assert (state.message_ru, state.message_en, state.eta) == ("ru", "en", NOW)
    assert await service.module_blocked("gifts", 9, is_admin=False)
    assert not await service.module_blocked("gifts", 2, is_admin=False)
    service.invalidate()
    assert service.cached() is None


async def test_check_records_hits_once(session_factory: SessionFactory) -> None:
    service = MaintenanceService(session_factory)
    assert not await service.check(1, is_admin=False)
    await service.enable(1000)
    assert await service.check(1, is_admin=False)
    assert await service.check(1, is_admin=False)
    assert await service.check(2, is_admin=False)
    assert not await service.check(3, is_admin=True)
    assert await service.pending_hits() == 2
    assert sorted(await service.pop_hits()) == [1, 2]
    assert await service.pop_hits() == []
    assert await service.check(1, is_admin=False)  # remembered again after the pop
    assert await service.pop_hits(limit=10) == [1]


async def test_windows_lifecycle(session_factory: SessionFactory) -> None:
    service = MaintenanceService(session_factory)
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        await service.schedule(1, now + timedelta(hours=2), now + timedelta(hours=1))
    with pytest.raises(ValueError):
        await service.schedule(1, now - timedelta(hours=2), now - timedelta(hours=1))
    with pytest.raises(ValueError):
        await service.schedule(1, now, now + timedelta(hours=1), announce_minutes=-1)
    first = await service.schedule(1, now + timedelta(hours=2), now + timedelta(hours=3), message_ru="план")
    second = await service.schedule(1, now + timedelta(hours=1), now + timedelta(hours=2))
    state = await service.state(fresh=True)
    assert [w.id for w in state.windows] == [second.id, first.id]
    await service.mark_window(second.id, announced=True, started=True)
    state = await service.state(fresh=True)
    assert state.windows[0].announced and state.windows[0].started
    assert await service.cancel_window(1, first.id)
    assert not await service.cancel_window(1, "missing")
    assert await service.prune_windows(now) == []
    finished = await service.prune_windows(now + timedelta(hours=5))
    assert [w.id for w in finished] == [second.id]
    assert (await service.state(fresh=True)).windows == []


async def test_corrupted_stored_state_is_off(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        await kv.set(s, STATE_KEY, "{broken")
    service = MaintenanceService(session_factory)
    assert not (await service.state()).enabled
    state = await service.enable(1)
    assert state.enabled


# ---------------------------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------------------------
class FakeI18n:
    def t(self, key: str, lang: str, /, **kw: Any) -> str:
        return key  # unknown everywhere: bundled texts are used


def fake_ctx(session_factory: SessionFactory, **settings: Any) -> Any:
    return SimpleNamespace(
        maintenance=MaintenanceService(session_factory),
        settings=make_settings(**settings),
        session_factory=session_factory,
        bot=object(),
        i18n=FakeI18n(),
    )


async def test_tick_announces_windows_and_notifies_users(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_texts: list[str] = []
    sent: list[tuple[int, str]] = []

    async def notify_admins(bot: Any, settings: Any, text: str, **kw: Any) -> None:
        admin_texts.append(text)

    async def safe_send(bot: Any, chat_id: int, text: str, **kw: Any) -> bool:
        sent.append((chat_id, text))
        return True

    monkeypatch.setattr(system_jobs, "notify_admins", notify_admins)
    monkeypatch.setattr(system_jobs, "safe_send", safe_send)
    monkeypatch.setattr(system_jobs, "SEND_INTERVAL_SEC", 0)
    ctx = fake_ctx(session_factory, brand_name="Brand")
    async with session_factory.begin() as s:
        s.add(User(tg_id=1, referral_code="A", sub_token="a", language="en"))
        s.add(User(tg_id=2, referral_code="B", sub_token="b", language="ru"))
        s.add(User(tg_id=3, referral_code="C", sub_token="c", bot_blocked=True))

    service: MaintenanceService = ctx.maintenance
    now = datetime.now(UTC)
    upcoming = await service.schedule(1, now + timedelta(minutes=10), now + timedelta(minutes=20), announce_minutes=30)
    await system_jobs.maintenance_tick(ctx)
    assert len(admin_texts) == 1 and "🛠" in admin_texts[0]
    state = await service.state(fresh=True)
    assert state.windows[0].announced and not state.windows[0].started
    await system_jobs.maintenance_tick(ctx)
    assert len(admin_texts) == 1  # announced once

    # Window in progress: started notice, users blocked and remembered.
    await service.cancel_window(1, upcoming.id)
    await service.schedule(1, now - timedelta(minutes=1), now + timedelta(minutes=1), announce_minutes=0)
    await system_jobs.maintenance_tick(ctx)
    assert len(admin_texts) == 2
    for user_id in (1, 2, 3):
        assert await service.check(user_id, is_admin=False)
    assert sent == []

    # Window over: finished notice and "we're back" messages in each user's language (blocked users skipped).
    finished_at = datetime.now(UTC) + timedelta(minutes=5)
    monkeypatch.setattr(system_jobs, "datetime", SimpleNamespace(now=lambda tz=None: finished_at))
    await system_jobs.maintenance_tick(ctx)
    assert len(admin_texts) == 3
    assert sorted(chat for chat, _ in sent) == [1, 2]
    texts = dict(sent)
    assert "Brand" in texts[1] and "works again" in texts[1]
    assert "Brand" in texts[2] and "снова работает" in texts[2]
    assert await service.pending_hits() == 0


async def test_notify_disabled_still_clears_hits(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[int] = []

    async def safe_send(bot: Any, chat_id: int, text: str, **kw: Any) -> bool:
        sent.append(chat_id)
        return True

    monkeypatch.setattr(system_jobs, "safe_send", safe_send)
    ctx = fake_ctx(session_factory, maintenance_notify_on_finish=False)
    await ctx.maintenance.record_hit(1, "e")
    assert await system_jobs.notify_users_back(ctx) == 0
    assert sent == []
    assert await ctx.maintenance.pending_hits() == 0
