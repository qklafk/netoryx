"""Cost sharing module (personal mode, optional): monthly reminders, "I paid", the owner's overview."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram.methods import EditMessageText

from app.db.session import SessionFactory
from app.keyboards import Nav
from app.modules.cost_sharing import module as cost_module
from app.modules.cost_sharing import service
from app.modules.cost_sharing.bot import NAV, CostCb
from app.modules.cost_sharing.jobs import monthly_job
from app.utils.time import utcnow
from tests.test_middlewares_support import callback_update
from tests.test_mod_personal_support import OWNER, Harness, harnesses
from tests.test_subs_support import add_sub, add_user

A, B = 7001, 7002
CONFIG = {
    "cost_sharing_enabled": True,
    "cost_sharing_total_rub": 900,
    "cost_sharing_day": 1,
    "cost_sharing_details": "карта 2200 0000 0000 0000",
}


@pytest.fixture
async def harness_factory(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Any]:
    async with harnesses(session_factory, tmp_path) as factory:
        yield factory


def _configure(h: Harness, **overrides: Any) -> None:
    h.ctx.current = h.ctx.current.model_copy(update={**CONFIG, **overrides})


async def _members(sf: SessionFactory, now: datetime | None = None) -> None:
    now = now or utcnow()
    async with sf.begin() as session:
        await add_user(session, OWNER)
        await add_sub(session, OWNER, expires_at=now + timedelta(days=30))
        for uid in (A, B):
            await add_user(session, uid, first_name=f"Член{uid}")
            await add_sub(session, uid, expires_at=now + timedelta(days=30))
        await add_user(session, 7003)
        await add_sub(session, 7003, expires_at=now - timedelta(days=1))  # no access: not asked


def test_share_and_config() -> None:
    assert service.share_kop(100_000, 3) == 33_400
    assert service.share_kop(90_000, 3) == 30_000
    assert service.share_kop(0, 3) == 0 and service.share_kop(100, 0) == 0
    assert service.Period.from_json("2026-11", "junk") is None


async def test_off_unless_enabled_in_personal_mode(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(cost_module, app_mode="personal")
    await _members(session_factory)
    await h.feed(callback_update(OWNER, Nav(to=NAV).pack()))
    assert not [c for c in h.api.calls if type(c).__name__ in ("SendMessage", "EditMessageText")]
    _configure(h, cost_sharing_total_rub=0)
    await h.feed(callback_update(OWNER, Nav(to=NAV).pack()))
    assert "укажите в настройках" in h.last_text()


async def test_owner_starts_members_pay(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(cost_module, app_mode="personal")
    _configure(h)
    await _members(session_factory)
    await h.feed(callback_update(OWNER, Nav(to=NAV).pack()))
    assert "Напоминания уйдут автоматически 1-го числа" in h.last_text()
    period = service.period_of(utcnow(), h.ctx.settings)

    await h.feed(callback_update(OWNER, CostCb(action="start", period=period).pack()))
    assert "Напоминаний отправлено: 2" in h.popups()
    reminder = h.sent_to(A)[-1]
    assert "300 ₽" in reminder and "карта 2200" in reminder
    assert not h.sent_to(7003)
    assert "Оплатили: 0 из 2" in h.last_text()

    await h.feed(callback_update(A, CostCb(action="paid", period=period).pack()))
    assert any("Спасибо" in c.text for c in h.api.of(EditMessageText))
    assert any("оплату" in t and "1 из 2" in t for t in h.sent_to(OWNER))
    await h.feed(callback_update(A, CostCb(action="paid", period=period).pack()))  # twice is fine
    record = await service.get_period(session_factory, period)
    assert record is not None and record.paid == [A]

    await h.feed(callback_update(OWNER, CostCb(action="remind", period=period).pack()))
    assert "Напоминаний отправлено: 1" in h.popups()
    assert len(h.sent_to(B)) == 2 and len(h.sent_to(A)) == 1
    await h.feed(callback_update(OWNER, Nav(to=NAV).pack()))
    assert f"✅ Ann · {A}" in h.last_text() and f"⏳ Член{B} @user{B} · {B}" in h.last_text()

    await h.feed(callback_update(7003, CostCb(action="paid", period=period).pack()))
    assert "уже закрыта" in (h.popups()[-1] or "")


async def test_monthly_job_runs_once_per_month(
    harness_factory: Any, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    h: Harness = harness_factory(cost_module, app_mode="personal")
    _configure(h, cost_sharing_day=10, cost_sharing_include_owner=False)
    await _members(session_factory, datetime(2026, 11, 12, 9, tzinfo=UTC))
    early = datetime(2026, 11, 5, 9, tzinfo=UTC)
    monkeypatch.setattr("app.modules.cost_sharing.jobs.utcnow", lambda: early)
    await monthly_job(h.ctx)  # type: ignore[arg-type]
    assert not h.sent_to(A)

    later = datetime(2026, 11, 12, 9, tzinfo=UTC)
    monkeypatch.setattr("app.modules.cost_sharing.jobs.utcnow", lambda: later)
    await monthly_job(h.ctx)  # type: ignore[arg-type]
    await monthly_job(h.ctx)  # type: ignore[arg-type]
    assert len(h.sent_to(A)) == 1 and "450 ₽" in h.sent_to(A)[0] and "ноябрь 2026" in h.sent_to(A)[0]
    assert any("Складчина за ноябрь 2026" in t for t in h.sent_to(OWNER))
