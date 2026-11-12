"""Jobs of the subscription module: expiry, auto-renewal, reminders with quiet hours, reconcile, usage, inbound
refresh and panel health alerts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from aiogram.methods import SendMessage
from sqlalchemy import select

from app.core.modules import ModuleRegistry
from app.db.models import Inbound, Panel, PanelKind, SubscriptionStatus
from app.db.session import SessionFactory
from app.modules.subscription import jobs
from app.services import notifications
from app.services.notifications import AdminEvent
from app.services.panels.inventory import HealthChange
from tests.fakes import FakePanel, default_inbound
from tests.test_mod_user_support import USER, Harness, add_plan, add_subscription, get_sub, make_harness, top_up


@pytest.fixture
async def harness(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Harness]:
    h = make_harness(session_factory, tmp_path, tz="UTC")
    yield h
    await h.close()


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[AdminEvent, str]]:
    sent: list[tuple[AdminEvent, str]] = []

    def notify(ctx: Any, event: AdminEvent, text: str, **kwargs: Any) -> bool:
        sent.append((event, text))
        return True

    monkeypatch.setattr(notifications, "notify", notify)
    return sent


def at_hour(hour: int) -> datetime:
    return datetime(2026, 11, 4, hour, 30, tzinfo=UTC)


async def register(h: Harness, uid: int = USER) -> None:
    await h.send("/start", uid=uid)
    h.api.calls.clear()


def sent_to(h: Harness, uid: int) -> list[str]:
    return [call.text for call in h.api.of(SendMessage) if int(call.chat_id) == uid]


# --- quiet hours ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("start", "end", "hour", "quiet"),
    [(22, 9, 23, True), (22, 9, 3, True), (22, 9, 9, False), (22, 9, 21, False), (1, 6, 3, True), (8, 8, 3, False)],
)
def test_quiet_hours(harness: Harness, start: int, end: int, hour: int, quiet: bool) -> None:
    settings = harness.ctx.settings.model_copy(
        update={"reminder_quiet_start_hour": start, "reminder_quiet_end_hour": end}
    )
    assert jobs.in_quiet_hours(settings, at_hour(hour)) is quiet


# --- reminders --------------------------------------------------------------------------------------------------------
async def test_reminder_is_sent_outside_quiet_hours_only(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness
    await register(h)
    plan_id = await add_plan(h.sf, title="Месяц", price_kop=15_000)
    await add_subscription(h.sf, days=0.5, plan_id=plan_id, auto_renew=False)

    monkeypatch.setattr(jobs, "utcnow", lambda: datetime.now(UTC).replace(hour=3))
    await jobs.reminders_job(h.ctx)  # type: ignore[arg-type]
    assert not sent_to(h, USER)

    monkeypatch.setattr(jobs, "utcnow", lambda: datetime.now(UTC).replace(hour=12))
    await jobs.reminders_job(h.ctx)  # type: ignore[arg-type]
    texts = sent_to(h, USER)
    assert len(texts) == 1 and "Подписка закончится через 1 день" in texts[0]
    assert h.t("subscription.remind.renew") in texts[0]
    await jobs.reminders_job(h.ctx)  # type: ignore[arg-type]
    assert len(sent_to(h, USER)) == 1  # recorded: not repeated


async def test_reminder_mentions_auto_renewal_and_low_balance(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    await register(h)
    plan_id = await add_plan(h.sf, title="Месяц", price_kop=15_000)
    await add_subscription(h.sf, days=2.5, plan_id=plan_id, auto_renew=True)
    monkeypatch.setattr(jobs, "utcnow", lambda: datetime.now(UTC).replace(hour=12))
    await jobs.reminders_job(h.ctx)  # type: ignore[arg-type]
    texts = sent_to(h, USER)
    assert texts and "на балансе 0 ₽, а нужно 150 ₽" in texts[0]


async def test_expired_notice_and_complimentary_silence(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness
    await register(h)
    await register(h, 7003)
    now = at_hour(12)
    await add_subscription(h.sf, expires_at=now - timedelta(hours=2))
    await add_subscription(h.sf, 7003, expires_at=now + timedelta(hours=12), is_complimentary=True)
    monkeypatch.setattr(jobs, "utcnow", lambda: now)
    await jobs.reminders_job(h.ctx)  # type: ignore[arg-type]
    assert h.t("subscription.remind.expired").split("\n")[0] in sent_to(h, USER)[0]
    assert not sent_to(h, 7003)
    sub = await get_sub(h.sf, 7003)
    assert sub is not None and sub.last_reminder_days is not None  # recorded without a message


# --- expiry & auto-renewal --------------------------------------------------------------------------------------------
async def test_expire_job_marks_and_triggers_reconcile(harness: Harness) -> None:
    h = harness
    await register(h)
    await add_subscription(h.sf, days=-0.01)
    await jobs.expire_job(h.ctx)  # type: ignore[arg-type]
    sub = await get_sub(h.sf)
    assert sub is not None and sub.status == SubscriptionStatus.EXPIRED
    assert h.ctx.scheduler.started == [jobs.RECONCILE_JOB]


async def test_auto_renew_job_renews_and_tells_the_user(harness: Harness, alerts: list[Any]) -> None:
    h = harness
    await register(h)
    plan_id = await add_plan(h.sf, title="Месяц", price_kop=15_000)
    await add_subscription(h.sf, days=0.5, plan_id=plan_id, auto_renew=True)
    await top_up(h.sf, USER, 20_000)
    before = await get_sub(h.sf)
    assert before is not None
    await jobs.auto_renew_job(h.ctx)  # type: ignore[arg-type]
    after = await get_sub(h.sf)
    assert after is not None and after.expires_at - before.expires_at == timedelta(days=30)
    texts = sent_to(h, USER)
    assert texts and "Подписка продлена автоматически" in texts[0] and "осталось 50 ₽" in texts[0]
    assert alerts and alerts[-1][0] == AdminEvent.PAYMENTS and "Ann" in alerts[-1][1]


# --- panels -----------------------------------------------------------------------------------------------------------
async def _panel(sf: SessionFactory, kind: PanelKind = PanelKind.XUI, **values: Any) -> int:
    async with sf.begin() as session:
        panel = Panel(name="DE-1", kind=kind, url="https://p.example.com", **values)
        session.add(panel)
        await session.flush()
        return panel.id


async def test_health_job_alerts_on_transitions(harness: Harness, alerts: list[Any]) -> None:
    h = harness
    h.ctx.current = h.ctx.current.model_copy(update={"health_failures_before_alert": 1})
    panel_id = await _panel(h.sf)
    h.ctx.panels.backends[panel_id] = FakePanel(down=True)
    await jobs.health_job(h.ctx)  # type: ignore[arg-type]
    assert alerts and alerts[-1][0] == AdminEvent.ALERTS and "DE-1" in alerts[-1][1] and "не отвечает" in alerts[-1][1]
    h.ctx.panels.backends[panel_id] = FakePanel()
    await jobs.health_job(h.ctx)  # type: ignore[arg-type]
    assert "снова на связи" in alerts[-1][1]


def test_health_alert_texts(harness: Harness) -> None:
    from app.modules.subscription.notices import health_alert

    _ = harness.ctx.i18n.translator("ru")
    kinds: tuple[Literal["panel", "endpoint", "probes"], ...] = ("panel", "endpoint", "probes")
    for kind in kinds:
        for ok in (True, False):
            text = health_alert(_, HealthChange(kind, 1, "X<1>", ok=ok, error="boom"))
            assert not text.startswith("subscription.") and "X<1>" not in text


async def test_refresh_inbounds_job_reports_new_inbounds(harness: Harness, alerts: list[Any]) -> None:
    h = harness
    panel_id = await _panel(h.sf)
    await _panel(h.sf, kind=PanelKind.REMNAWAVE)  # not a 3x-ui panel: skipped
    h.ctx.panels.backends[panel_id] = FakePanel(inbounds=[default_inbound(1), default_inbound(2, 444)])
    await jobs.refresh_inbounds_job(h.ctx)  # type: ignore[arg-type]
    async with h.sf() as session:
        assert len(list(await session.scalars(select(Inbound)))) == 2
    assert alerts and "2" in alerts[0][1] and "DE-1" in alerts[0][1]


async def test_refresh_inbounds_job_survives_a_dead_panel(harness: Harness, alerts: list[Any]) -> None:
    h = harness
    panel_id = await _panel(h.sf)
    h.ctx.panels.backends[panel_id] = FakePanel(down=True)
    await jobs.refresh_inbounds_job(h.ctx)  # type: ignore[arg-type]
    assert not alerts


async def test_reconcile_and_usage_jobs_run(harness: Harness) -> None:
    h = harness
    await register(h)
    panel_id = await _panel(h.sf, kind=PanelKind.REMNAWAVE)
    backend = FakePanel(kind=PanelKind.REMNAWAVE)
    h.ctx.panels.backends[panel_id] = backend
    await add_subscription(h.sf, days=10)
    await jobs.reconcile_job(h.ctx)  # type: ignore[arg-type]
    assert any(op == "upsert" for op, _target, _email in backend.calls)
    await jobs.usage_job(h.ctx)  # type: ignore[arg-type]


def test_job_specs(harness: Harness) -> None:
    ids = [spec.id for spec in jobs.JOBS]
    assert len(ids) == len(set(ids)) and all(len(i) <= 48 for i in ids)
    settings = harness.ctx.settings.model_copy(update={"reconcile_interval_min": 7, "health_check_interval_min": 0})
    by_id = {spec.id: spec for spec in jobs.JOBS}
    assert by_id[jobs.RECONCILE_JOB].trigger_kwargs(settings) == {"minutes": 7}
    assert by_id[jobs.HEALTH_JOB].trigger_kwargs(settings) == {"minutes": 1}


def test_modules_register_with_the_registry(harness: Harness) -> None:
    from app.modules.core_user import module as core_user
    from app.modules.subscription import module as subscription

    registry = ModuleRegistry([subscription, core_user])
    assert [m.name for m in registry] == ["core_user", "subscription"]
    assert all(m.required for m in registry)
    commands = [c.command for c in registry.commands(harness.ctx.settings, admin=False)]
    assert commands[0] == "start" and "connect" in commands
