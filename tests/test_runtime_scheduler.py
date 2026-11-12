from __future__ import annotations

import asyncio
from typing import Any

import pytest
from apscheduler.triggers.interval import IntervalTrigger

import app.scheduler as scheduler_module
from app.core.locks import LockManager
from app.core.modules import FeatureModule, JobSpec, ModuleRegistry
from app.core.settings_provider import SettingsProvider
from app.db.session import SessionFactory
from app.scheduler import Scheduler
from app.utils.crypto import SecretBox
from tests.conftest import make_settings


class FakeI18n:
    def t(self, key: str, lang: str, /, **kw: Any) -> str:
        return key


class Ctx:
    def __init__(self, provider: SettingsProvider, modules: list[FeatureModule], session_factory: SessionFactory):
        self.settings_provider = provider
        self.modules = ModuleRegistry(modules)
        self.session_factory = session_factory
        self.locks = LockManager(session_factory, owner="me")
        self.redis = None
        self.bot = object()
        self.i18n = FakeI18n()

    @property
    def settings(self) -> Any:
        return self.settings_provider.current()


async def make(session_factory: SessionFactory, modules: list[FeatureModule], **kw: Any) -> tuple[Ctx, Scheduler]:
    provider = SettingsProvider(make_settings(), session_factory, SecretBox("k"))
    await provider.load()
    ctx = Ctx(provider, modules, session_factory)
    scheduler = Scheduler(ctx, **kw)  # type: ignore[arg-type]
    scheduler.register_modules(ctx.modules)
    return ctx, scheduler


def module(name: str, jobs: list[JobSpec], **kw: Any) -> FeatureModule:
    return FeatureModule(name=name, title_ru=name, title_en=name, jobs=jobs, **kw)


async def wait_idle(scheduler: Scheduler) -> None:
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not scheduler._running and not scheduler._background:
            return


async def test_jobs_follow_module_switches_and_settings(session_factory: SessionFactory) -> None:
    async def noop(ctx: Any) -> None:
        return None

    jobs = [
        JobSpec("a.fixed", noop, kwargs={"seconds": 30}),
        JobSpec("a.dynamic", noop, schedule=lambda s: {"minutes": s.reconcile_interval_min}),
        JobSpec("a.cond", noop, kwargs={"hours": 1}, enabled=lambda s: s.gifts_enabled),
    ]
    ctx, scheduler = await make(session_factory, [module("a", jobs)])
    assert not scheduler.is_scheduled("a.fixed")
    await scheduler.start()
    try:
        assert all(scheduler.is_scheduled(j) for j in ("a.fixed", "a.dynamic", "a.cond"))
        trigger = scheduler._aps.get_job("a.dynamic").trigger
        assert isinstance(trigger, IntervalTrigger) and trigger.interval.total_seconds() == 300

        await ctx.settings_provider.set("reconcile_interval_min", 2, actor_id=1)
        trigger = scheduler._aps.get_job("a.dynamic").trigger
        assert trigger.interval.total_seconds() == 120

        await ctx.settings_provider.set("gifts_enabled", False, actor_id=1)
        assert not scheduler.is_scheduled("a.cond")
        assert scheduler._aps.get_job("a.cond") is None

        await ctx.settings_provider.set("disabled_modules", "a", actor_id=1)
        assert not any(scheduler.is_scheduled(j) for j in scheduler.job_ids)
        await ctx.settings_provider.set("disabled_modules", "", actor_id=1)
        assert scheduler.is_scheduled("a.fixed") and not scheduler.is_scheduled("a.cond")
    finally:
        await scheduler.shutdown()
    assert not scheduler.is_scheduled("a.fixed")


async def test_registration_validation(session_factory: SessionFactory) -> None:
    async def noop(ctx: Any) -> None:
        return None

    _, scheduler = await make(session_factory, [])
    scheduler.register(JobSpec("x", noop))
    with pytest.raises(ValueError, match="duplicate"):
        scheduler.register(JobSpec("x", noop))
    with pytest.raises(ValueError):
        scheduler.register(JobSpec("y" * 60, noop))
    with pytest.raises(ValueError):
        scheduler.register(JobSpec("z", noop, trigger="weekly"))  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        scheduler.run_now("missing")


async def test_plugin_contribution_replaces_legacy_job_without_duplicates(session_factory: SessionFactory) -> None:
    async def legacy(_: Any) -> None:
        return None

    async def plugin(_: Any) -> None:
        return None

    legacy_spec = JobSpec("housekeeping.backup", legacy, kwargs={"hours": 24})
    plugin_spec = JobSpec("housekeeping.backup", plugin, kwargs={"hours": 24})
    _, scheduler = await make(session_factory, [module("housekeeping", [legacy_spec])], use_locks=False)
    await scheduler.start()
    try:
        assert scheduler.is_scheduled("housekeeping.backup")
        await scheduler.activate_contribution("infrastructure.backups", (plugin_spec,))
        await scheduler.activate_contribution("infrastructure.backups", (plugin_spec,))
        statuses = {item.id: item for item in await scheduler.status()}
        assert list(scheduler.job_ids).count("housekeeping.backup") == 1
        assert statuses["housekeeping.backup"].module == "infrastructure.backups"
        assert (await scheduler.contribution_status("infrastructure.backups"))["jobs"][0]["scheduled"] is True

        await scheduler.deactivate_contribution("infrastructure.backups")
        statuses = {item.id: item for item in await scheduler.status()}
        assert statuses["housekeeping.backup"].enabled is False
        status = await scheduler.contribution_status("infrastructure.backups")
        assert status["jobs"] == [] and status["suppressed_legacy_jobs"] == ["housekeeping.backup"]
    finally:
        await scheduler.shutdown()


async def test_run_records_status_and_alerts_once(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    alerts: list[str] = []

    async def notify_admins(bot: Any, settings: Any, text: str, **kw: Any) -> None:
        alerts.append(text)

    monkeypatch.setattr(scheduler_module, "notify_admins", notify_admins)
    calls: list[str] = []

    async def good(ctx: Any) -> None:
        calls.append("good")

    async def bad(ctx: Any) -> None:
        calls.append("bad")
        raise RuntimeError("panel <down>")

    _, scheduler = await make(
        session_factory, [module("m", [JobSpec("m.good", good), JobSpec("m.bad", bad)])], use_locks=False
    )
    await scheduler.start()
    try:
        assert scheduler.run_now("m.good")
        await wait_idle(scheduler)
        scheduler.run_now("m.bad")
        await wait_idle(scheduler)
        scheduler.run_now("m.bad")
        await wait_idle(scheduler)
        statuses = {s.id: s for s in await scheduler.status()}
    finally:
        await scheduler.shutdown()
    assert calls == ["good", "bad", "bad"]
    good_status, bad_status = statuses["m.good"], statuses["m.bad"]
    assert good_status.last_ok is True and good_status.last_error is None and good_status.enabled
    assert good_status.last_duration_ms is not None and good_status.next_run_at is not None
    assert bad_status.last_ok is False and bad_status.last_error == "RuntimeError: panel <down>"
    assert bad_status.module == "m" and not bad_status.running
    assert len(alerts) == 1  # rate-limited
    assert "m.bad" in alerts[0] and "&lt;down&gt;" in alerts[0]


async def test_error_kept_after_success(session_factory: SessionFactory) -> None:
    state = {"fail": True}

    async def flaky(ctx: Any) -> None:
        if state["fail"]:
            raise ValueError("once")

    _, scheduler = await make(session_factory, [module("m", [JobSpec("m.flaky", flaky)])], use_locks=False)
    scheduler._alert_interval = 0
    await scheduler._run("m.flaky")
    state["fail"] = False
    await scheduler._run("m.flaky")
    (status,) = await scheduler.status()
    assert status.last_ok is True and status.last_error == "ValueError: once" and status.last_error_at is not None
    assert status.enabled is False and status.next_run_at is None  # never started


async def test_locked_job_is_skipped(session_factory: SessionFactory) -> None:
    calls: list[int] = []

    async def job(ctx: Any) -> None:
        calls.append(1)

    _, scheduler = await make(session_factory, [module("m", [JobSpec("m.job", job)])], use_locks=True)
    other = LockManager(session_factory, owner="other")
    assert await other.acquire("job:m.job", 60)
    await scheduler._run("m.job")
    assert calls == []
    (status,) = await scheduler.status()
    assert status.last_started_at is None  # skipped runs are not recorded
    await other.release("job:m.job")
    await scheduler._run("m.job")
    assert calls == [1]


async def test_shutdown_cancels_long_jobs(session_factory: SessionFactory) -> None:
    started = asyncio.Event()

    async def slow(ctx: Any) -> None:
        started.set()
        await asyncio.sleep(30)

    _, scheduler = await make(session_factory, [module("m", [JobSpec("m.slow", slow)])], use_locks=False)
    await scheduler.start()
    assert scheduler.run_now("m.slow")
    await started.wait()
    assert not scheduler.run_now("m.slow")  # already running
    await scheduler.shutdown(wait_sec=0.05)
    assert not scheduler._running


async def test_run_on_start_and_cron(session_factory: SessionFactory) -> None:
    ran = asyncio.Event()

    async def job(ctx: Any) -> None:
        ran.set()

    jobs = [JobSpec("m.now", job, kwargs={"hours": 6}, run_on_start=True), JobSpec("m.cron", job, "cron", {"hour": 4})]
    _, scheduler = await make(session_factory, [module("m", jobs)], use_locks=False)
    await scheduler.start()
    try:
        await asyncio.wait_for(ran.wait(), 5)
        assert scheduler.is_scheduled("m.cron")
    finally:
        await scheduler.shutdown()


async def test_bad_schedule_is_logged_not_raised(session_factory: SessionFactory) -> None:
    async def job(ctx: Any) -> None:
        return None

    def broken(settings: Any) -> dict[str, Any]:
        raise RuntimeError("bad")

    jobs = [JobSpec("m.broken", job, schedule=broken), JobSpec("m.invalid", job, kwargs={"fortnights": 1})]
    _, scheduler = await make(session_factory, [module("m", jobs)], use_locks=False)
    await scheduler.start()
    try:
        assert not scheduler.is_scheduled("m.broken")
        assert not scheduler.is_scheduled("m.invalid")
    finally:
        await scheduler.shutdown()
