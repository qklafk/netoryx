"""Regression tests for subscriptions/provisioning hardening: bans reach the panels, first-subscription creation
takes no gap locks, the trial cannot land on a paid subscription, panel drift is repaired, retired panels are
drained even while marked down, scans and staleness checks are pure reads, one DB error does not discard a whole
run, and a lapsed Stars subscription hands renewal back to the wallet."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState

from app.config import Settings
from app.db.models import (
    Panel,
    Provision,
    ProvisionState,
    PurchaseKind,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.db.session import SessionFactory
from app.services import promo, referrals
from app.services import provisioning as prov
from app.services import subscriptions as subs
from app.services.panels.base import RemoteAccount
from app.services.panels.xui import build_client
from app.services.subscriptions import SubErrorCode, SubscriptionError
from app.utils.time import utcnow
from tests.conftest import make_settings
from tests.fakes import FakePanel
from tests.test_subs_support import (
    NOW,
    FakeRegistry,
    add_plan,
    add_rw_panel,
    add_sub,
    add_user,
    add_xui_panel,
    days,
    provisions_of,
)
from tests.test_xui_backend import BOTH, Generation, Started, spec, start  # noqa: F401  (fixture)

TEN_DAYS = timedelta(days=10)


async def _world(session_factory: SessionFactory, *, inbounds: int = 1) -> tuple[FakeRegistry, Panel, list[Any], int]:
    async with session_factory.begin() as s:
        panel, created = await add_xui_panel(s, inbounds=inbounds)
        await add_user(s, 1)
        sub_id = (await add_sub(s, 1, expires_at=NOW + TEN_DAYS)).id
    return FakeRegistry({panel.id: FakePanel()}), panel, created, sub_id


# ---------------------------------------------------------------------------------------------
# Bans
# ---------------------------------------------------------------------------------------------
def test_desired_state_of_banned_user_is_disabled() -> None:
    sub = Subscription(status=SubscriptionStatus.ACTIVE, expires_at=NOW + TEN_DAYS, client_uuid="u", revision=1)
    assert prov.desired_state(sub, NOW) == ProvisionState.ENABLED
    assert prov.desired_state(sub, NOW, banned=True) == ProvisionState.DISABLED
    sub.expires_at = NOW - days(40)
    assert prov.desired_state(sub, NOW, banned=True) == ProvisionState.ABSENT  # retention still applies


async def _set_banned(session_factory: SessionFactory, banned: bool) -> None:
    async with session_factory.begin() as s:
        await s.execute(update(User).where(User.tg_id == 1).values(is_banned=banned))


async def test_ban_disables_panel_accounts_without_revision_bump(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, panel, (inbound,), sub_id = await _world(session_factory)
    fake = registry.panels[panel.id]
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    key = (inbound.remote_id, f"tg1-{inbound.id}")
    assert fake.accounts[key].enabled is True

    await _set_banned(session_factory, True)
    report = await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert report.synced == 1
    assert fake.accounts[key].enabled is False
    (row,) = await provisions_of(session_factory, sub_id)
    assert row.actual_state == ProvisionState.DISABLED

    await _set_banned(session_factory, False)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert fake.accounts[key].enabled is True
    (row,) = await provisions_of(session_factory, sub_id)
    assert row.actual_state == ProvisionState.ENABLED


async def test_sync_and_new_provisions_of_banned_user_are_disabled(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        panel, (inbound,) = await add_xui_panel(s)
        await add_user(s, 1, is_banned=True)
        now = utcnow()  # ensure_provisions evaluates the expiry against the real clock
        sub = await add_sub(s, 1, expires_at=now + TEN_DAYS)
        await prov.ensure_provisions(s, sub)
        sub_id = sub.id
    (row,) = await provisions_of(session_factory, sub_id)
    assert row.desired_state == ProvisionState.DISABLED
    registry = FakeRegistry({panel.id: FakePanel()})
    result = await prov.sync_subscription(session_factory, registry, settings, sub_id, now=now)
    assert result.ok
    assert registry.panels[panel.id].accounts[(inbound.remote_id, f"tg1-{inbound.id}")].enabled is False


# ---------------------------------------------------------------------------------------------
# First subscription: no locking read of a row that does not exist (InnoDB gap-lock deadlock)
# ---------------------------------------------------------------------------------------------
@contextmanager
def _subscription_locks(session: AsyncSession) -> Iterator[list[Any]]:
    """Collect every ``SELECT ... FOR UPDATE`` on ``subscriptions`` issued through ``session``."""
    locks: list[Any] = []

    def listener(state: ORMExecuteState) -> None:
        stmt: Any = state.statement
        locking = state.is_select and getattr(stmt, "_for_update_arg", None) is not None
        if locking and any(getattr(t, "name", None) == "subscriptions" for t in stmt.get_final_froms()):
            locks.append(stmt)

    event.listen(session.sync_session, "do_orm_execute", listener)
    try:
        yield locks
    finally:
        event.remove(session.sync_session, "do_orm_execute", listener)


async def test_first_subscription_paths_take_no_lock_on_a_missing_row(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        for uid in (1, 2, 3, 4):
            await add_user(s, uid, balance_kop=50_000)
        plan = await add_plan(s)
        code = (await promo.create_promo(s, code="DAYS5", bonus_days=5)).code
    async with session_factory.begin() as s:
        with _subscription_locks(s) as locks:
            user = await s.get(User, 1)
            assert user is not None
            await subs.start_trial(s, settings, user, now=NOW)
            user2 = await s.get(User, 2)
            plan_row = await s.get(type(plan), plan.id)
            assert user2 is not None and plan_row is not None
            await subs.purchase_plan(s, settings, user2, plan_row, now=NOW)
            user3 = await s.get(User, 3)
            assert user3 is not None
            await subs.grant_days(s, settings, user3, 7, kind=PurchaseKind.ADMIN_GRANT, now=NOW)
            user4 = await s.get(User, 4)
            assert user4 is not None
            outcome, _ = await promo.redeem(s, settings, user4, code)
            assert outcome is promo.PromoOutcome.OK
            await referrals._lock_subscription(s, 5)  # nobody: must not lock a gap either
        assert locks == []
    async with session_factory() as s:
        assert len((await s.scalars(select(Subscription))).all()) == 3


async def test_existing_subscription_is_still_row_locked(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        await add_sub(s, 1, expires_at=NOW + TEN_DAYS)
    async with session_factory.begin() as s:
        with _subscription_locks(s) as locks:
            user = await s.get(User, 1)
            assert user is not None
            await subs.grant_days(s, settings, user, 3, kind=PurchaseKind.ADMIN_GRANT, now=NOW)
            await promo._lock_subscription(s, 1)
            await referrals._lock_subscription(s, 1)
        assert len(locks) == 3


async def test_create_falls_back_to_locking_the_row_a_concurrent_transaction_inserted(
    session_factory: SessionFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On MySQL REPEATABLE READ the plain existence check may miss a row committed after the snapshot; the
    duplicate insert must then still find (and lock) that row."""
    async with session_factory.begin() as s:
        await add_user(s, 1)
        existing = await add_sub(s, 1, expires_at=NOW + TEN_DAYS)

    async def snapshot_misses_it(session: AsyncSession, user_id: int) -> None:
        return None

    monkeypatch.setattr(subs, "_lock_for_user", snapshot_misses_it)
    async with session_factory.begin() as s:
        user = await s.get(User, 1)
        assert user is not None
        purchase = await subs.grant_days(s, settings, user, 3, kind=PurchaseKind.ADMIN_GRANT, now=NOW)
        assert purchase.expires_before == existing.expires_at
    async with session_factory() as s:
        (sub,) = (await s.scalars(select(Subscription))).all()
    assert sub.expires_at == NOW + TEN_DAYS + days(3)


# ---------------------------------------------------------------------------------------------
# Trial racing a first purchase
# ---------------------------------------------------------------------------------------------
async def test_trial_racing_first_purchase_does_not_touch_the_paid_subscription(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(trial_days=3, trial_device_limit=1, trial_traffic_gb=10)
    async with session_factory.begin() as s:
        user = await add_user(s, 1, balance_kop=50_000)
        plan = await add_plan(s, device_limit=3, traffic_gb=0)
        await subs.purchase_plan(s, settings, user, plan, now=NOW)

    real = subs._lock_for_user
    calls = {"n": 0}

    async def check_ran_before_the_purchase_committed(session: AsyncSession, user_id: int) -> Subscription | None:
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(session, user_id)

    monkeypatch.setattr(subs, "_lock_for_user", check_ran_before_the_purchase_committed)
    async with session_factory.begin() as s:
        racer = await s.get(User, 1)
        assert racer is not None
        with pytest.raises(SubscriptionError) as err:
            await subs.start_trial(s, settings, racer, now=NOW)
        assert err.value.code == SubErrorCode.TRIAL_USED
        # The caller catches the refusal and commits: nothing of the trial may have been written.

    async with session_factory() as s:
        (sub,) = (await s.scalars(select(Subscription))).all()
        db_user = await s.get(User, 1)
    assert (sub.device_limit, sub.traffic_limit_gb, sub.is_trial) == (3, 0, False)
    assert sub.expires_at == NOW + days(30)
    assert db_user is not None and db_user.trial_used is False


# ---------------------------------------------------------------------------------------------
# Drift: accounts deleted/changed on the panel are repaired
# ---------------------------------------------------------------------------------------------
async def test_account_deleted_on_panel_is_recreated(session_factory: SessionFactory, settings: Settings) -> None:
    registry, panel, (inbound,), _ = await _world(session_factory)
    fake = registry.panels[panel.id]
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    fake.accounts.clear()

    first = await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert first.drifted == 1
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    account = fake.accounts[(inbound.remote_id, f"tg1-{inbound.id}")]
    assert account.enabled is True and account.expires_at == NOW + TEN_DAYS


async def test_account_changed_on_panel_is_repushed(session_factory: SessionFactory) -> None:
    settings = make_settings(xui_device_limit="ip")
    registry, panel, (inbound,), _ = await _world(session_factory)
    fake = registry.panels[panel.id]
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    key = (inbound.remote_id, f"tg1-{inbound.id}")
    fake.accounts[key] = replace(fake.accounts[key], enabled=False, device_limit=99)

    await prov.reconcile(session_factory, registry, settings, now=NOW)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert fake.accounts[key].enabled is True and fake.accounts[key].device_limit == 3


async def test_in_sync_and_panel_limited_accounts_are_not_drift(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        panel, (inbound,) = await add_xui_panel(s)
        await add_user(s, 1)
        await add_sub(s, 1, expires_at=NOW + TEN_DAYS, traffic_gb=1)
    fake = FakePanel()
    registry = FakeRegistry({panel.id: fake})
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    key = (inbound.remote_id, f"tg1-{inbound.id}")
    # 3x-ui disables a client that used up its quota itself: that is not drift to undo.
    fake.accounts[key] = replace(fake.accounts[key], enabled=False, used_bytes=1024**3)
    fake.calls.clear()
    for _ in range(3):
        report = await prov.reconcile(session_factory, registry, settings, now=NOW)
        assert report.drifted == 0
    # The used-up quota is also the subscription's: it is disabled once on purpose, never re-enabled as drift.
    assert [c for c in fake.calls if c[0] == "upsert"] == [("upsert", key[0], key[1])]
    assert fake.accounts[key].enabled is False


async def test_drift_is_not_checked_on_a_panel_whose_listing_failed(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, panel, _, sub_id = await _world(session_factory)
    fake = registry.panels[panel.id]
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    fake.accounts.clear()
    original = fake.list_accounts

    async def broken(target: int | None) -> list[RemoteAccount]:
        raise prov.PanelError("listing failed")

    fake.list_accounts = broken  # type: ignore[method-assign]
    report = await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert report.drifted == 0 and report.errors
    fake.list_accounts = original  # type: ignore[method-assign]
    (row,) = await provisions_of(session_factory, sub_id)
    assert row.synced_revision == 1


# ---------------------------------------------------------------------------------------------
# Retired panel that was marked down is still drained
# ---------------------------------------------------------------------------------------------
async def test_panel_deactivated_while_down_is_drained(session_factory: SessionFactory, settings: Settings) -> None:
    registry, panel, _, sub_id = await _world(session_factory)
    fake = registry.panels[panel.id]
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    async with session_factory.begin() as s:
        await s.execute(
            update(Panel)
            .where(Panel.id == panel.id)
            .values(is_active=False, consecutive_failures=settings.health_failures_before_alert + 2)
        )
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert fake.accounts == {}
    (row,) = await provisions_of(session_factory, sub_id)
    assert row.actual_state == ProvisionState.ABSENT


async def test_active_panel_marked_down_is_still_left_alone(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, panel, _, _ = await _world(session_factory)
    fake = registry.panels[panel.id]
    async with session_factory.begin() as s:
        await s.execute(
            update(Panel).where(Panel.id == panel.id).values(consecutive_failures=settings.health_failures_before_alert)
        )
    report = await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert report.panels_down == [panel.id]
    assert fake.calls == []


# ---------------------------------------------------------------------------------------------
# Scans and staleness checks are pure reads; the panel is listed once per scan
# ---------------------------------------------------------------------------------------------
@contextmanager
def _write_locks(session_factory: SessionFactory) -> Iterator[list[str]]:
    engine = session_factory.kw["bind"].sync_engine
    begins: list[str] = []

    def listener(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            begins.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield begins
    finally:
        event.remove(engine, "before_cursor_execute", listener)


async def test_orphan_scan_takes_no_write_lock(session_factory: SessionFactory, settings: Settings) -> None:
    registry, _, _, _ = await _world(session_factory, inbounds=2)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    with _write_locks(session_factory) as begins:
        orphans, errors = await prov.find_orphans(session_factory, registry, settings, now=NOW)
    assert (orphans, errors) == ([], [])
    assert begins == []


async def test_staleness_check_takes_no_write_lock(session_factory: SessionFactory, settings: Settings) -> None:
    _, _, _, sub_id = await _world(session_factory)
    async with session_factory.begin() as s:
        await prov.ensure_provisions(s, await s.get(Subscription, sub_id))  # type: ignore[arg-type]
    async with session_factory() as s:
        (row,) = (await s.execute(prov._work_query().where(Provision.subscription_id == sub_id))).all()
    job = prov._make_job(row, settings, NOW)
    assert job is not None
    with _write_locks(session_factory) as begins:
        assert await prov._is_stale(session_factory, job) is False
    assert begins == []


class _BulkPanel(FakePanel):
    """A backend that can list every target of the panel in one call."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.list_calls: list[Any] = []

    async def list_accounts(self, target: int | None) -> list[RemoteAccount]:
        self.list_calls.append(target)
        return await super().list_accounts(target)

    async def list_accounts_by_target(self, targets: list[int]) -> dict[int, list[RemoteAccount]]:
        self.list_calls.append(tuple(targets))
        return {t: [replace(a) for (tt, _), a in self.accounts.items() if tt == t] for t in targets}


async def test_orphan_scan_lists_the_panel_once(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        panel, created = await add_xui_panel(s, inbounds=3)
        await add_user(s, 1)
        await add_sub(s, 1, expires_at=NOW + TEN_DAYS)
    fake = _BulkPanel()
    registry = FakeRegistry({panel.id: fake})
    await prov.reconcile(session_factory, registry, settings, now=NOW, scan_orphans=False)
    fake.accounts[(created[0].remote_id, "tg999-1")] = RemoteAccount(
        email="tg999-1", uuid="x", enabled=True, expires_at=None, device_limit=0, traffic_limit_bytes=0
    )
    fake.list_calls.clear()
    orphans, _ = await prov.find_orphans(session_factory, registry, settings, now=NOW)
    assert [o.email for o in orphans] == ["tg999-1"]
    assert fake.list_calls == [tuple(i.remote_id for i in created)]


# ---------------------------------------------------------------------------------------------
# One DB error does not discard every panel's outcomes
# ---------------------------------------------------------------------------------------------
async def test_db_error_in_staleness_check_skips_only_that_job(
    session_factory: SessionFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with session_factory.begin() as s:
        xui, (inbound,) = await add_xui_panel(s)
        rw = await add_rw_panel(s)
        await add_user(s, 1)
        sub_id = (await add_sub(s, 1, expires_at=NOW + TEN_DAYS)).id
    registry = FakeRegistry({xui.id: FakePanel(), rw.id: FakePanel()})
    real = prov._is_stale

    async def flaky(session_factory: SessionFactory, job: Any) -> bool:
        if job.panel_id == rw.id:
            raise OperationalError("SELECT", {}, Exception("database is locked"))
        return await real(session_factory, job)

    monkeypatch.setattr(prov, "_is_stale", flaky)
    result = await prov.sync_subscription(session_factory, registry, settings, sub_id, now=NOW)
    assert (result.synced, result.skipped) == (1, 1)
    rows = {row.panel_id: row for row in await provisions_of(session_factory, sub_id)}
    assert rows[xui.id].actual_state == ProvisionState.ENABLED and rows[xui.id].synced_revision == 1
    assert rows[rw.id].actual_state == ProvisionState.ABSENT
    assert (inbound.remote_id, f"tg1-{inbound.id}") in registry.panels[xui.id].accounts


async def test_unexpected_error_in_one_panel_run_keeps_other_outcomes(
    session_factory: SessionFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with session_factory.begin() as s:
        xui, _ = await add_xui_panel(s)
        rw = await add_rw_panel(s)
        await add_user(s, 1)
        await add_sub(s, 1, expires_at=NOW + TEN_DAYS)
    registry = FakeRegistry({xui.id: FakePanel(), rw.id: FakePanel()})
    real = prov._execute

    async def exploding(backend: Any, job: Any) -> Any:
        if job.panel_id == rw.id:
            raise RuntimeError("bug outside the backend guard")
        return await real(backend, job)

    monkeypatch.setattr(prov, "_execute", exploding)
    report = await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert report.synced == 1
    rows = {row.panel_id: row for row in await provisions_of(session_factory)}
    assert rows[xui.id].actual_state == ProvisionState.ENABLED


# ---------------------------------------------------------------------------------------------
# Lapsed Stars subscription
# ---------------------------------------------------------------------------------------------
async def _stars_sub(session_factory: SessionFactory, expires_in: timedelta) -> int:
    async with session_factory.begin() as s:
        await add_user(s, 1, balance_kop=20_000)
        plan = await add_plan(s, days=30, price_kop=15_000)
        sub = await add_sub(
            s, 1, expires_at=NOW + expires_in, plan=plan, status=SubscriptionStatus.EXPIRED, stars_charge_id="ch-1"
        )
        return sub.id


async def test_lapsed_stars_subscription_falls_back_to_wallet_auto_renew(
    session_factory: SessionFactory, settings: Settings
) -> None:
    sub_id = await _stars_sub(session_factory, -subs.STARS_LAPSE_GRACE - timedelta(minutes=1))
    async with session_factory() as s:
        assert [sub.id for sub in await subs.due_for_auto_renew(s, settings, NOW)] == [sub_id]
    results = await subs.run_auto_renew(session_factory, settings, NOW)
    assert [outcome for _, _, outcome in results] == [subs.RenewOutcome.RENEWED]
    async with session_factory() as s:
        sub = await s.get(Subscription, sub_id)
    assert sub is not None
    assert sub.stars_charge_id is None and sub.status == SubscriptionStatus.ACTIVE


async def test_stars_renewal_still_has_time_to_arrive(session_factory: SessionFactory, settings: Settings) -> None:
    await _stars_sub(session_factory, -subs.STARS_LAPSE_GRACE + timedelta(minutes=1))
    async with session_factory() as s:
        assert await subs.due_for_auto_renew(s, settings, NOW) == []
    assert await subs.run_auto_renew(session_factory, settings, NOW) == []


# ---------------------------------------------------------------------------------------------
# 3x-ui: several inbounds listed with one request
# ---------------------------------------------------------------------------------------------


@BOTH
async def test_xui_lists_several_inbounds_with_one_request(start: Started, generation: Generation) -> None:  # noqa: F811
    fake, backend = await start(generation)
    fake.add_inbound(2, 8443)
    fake.seed_client(1, build_client(spec()), used=(1, 2))
    fake.seed_client(2, build_client(spec(email="tg43-2", uuid="22222222-2222-4333-8444-555555555555")))
    await backend.ping()
    fake.calls.clear()
    listed = await backend.list_accounts_by_target([1, 2])
    assert {t: [(a.email, a.used_bytes) for a in accs] for t, accs in listed.items()} == {
        1: [("tg42-1", 3)],
        2: [("tg43-2", 0)],
    }
    assert len([c for c in fake.calls if c[0] == "GET"]) == 1
    for target in (1, 2):
        assert [a.email for a in await backend.list_accounts(target)] == [a.email for a in listed[target]]
