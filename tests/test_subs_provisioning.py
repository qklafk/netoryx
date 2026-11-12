"""Tests for app.services.provisioning: desired state, specs, provision rows, reconcile, sync, orphans."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import update

from app.config import Settings
from app.db.models import (
    Inbound,
    Panel,
    PanelKind,
    Provision,
    ProvisionState,
    Subscription,
    SubscriptionStatus,
    utcnow,
)
from app.db.session import SessionFactory
from app.services import provisioning as prov
from app.services import subscriptions
from app.services.panels.base import RemoteAccount
from tests.conftest import make_settings
from tests.fakes import FakePanel
from tests.test_subs_support import (
    NOW,
    FakeRegistry,
    TxTracker,
    add_rw_panel,
    add_sub,
    add_user,
    add_xui_panel,
    days,
    provisions_of,
)

GIB = 1024**3
TEN_DAYS = timedelta(days=10)


def _sub(status: SubscriptionStatus, expires_in: timedelta) -> Subscription:
    return Subscription(status=status, expires_at=NOW + expires_in, client_uuid="u", revision=1)


# ---------------------------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "expires_in", "retention", "expected"),
    [
        (SubscriptionStatus.ACTIVE, days(1), 30, ProvisionState.ENABLED),
        (SubscriptionStatus.ACTIVE, timedelta(0), 30, ProvisionState.DISABLED),  # expires exactly now
        (SubscriptionStatus.ACTIVE, -days(1), 30, ProvisionState.DISABLED),  # expire_due has not run yet
        (SubscriptionStatus.ACTIVE, -days(31), 30, ProvisionState.ABSENT),
        (SubscriptionStatus.EXPIRED, -days(1), 30, ProvisionState.DISABLED),
        (SubscriptionStatus.EXPIRED, -days(30), 30, ProvisionState.DISABLED),  # boundary: not beyond yet
        (SubscriptionStatus.EXPIRED, -days(30) - timedelta(seconds=1), 30, ProvisionState.ABSENT),
        (SubscriptionStatus.EXPIRED, -timedelta(seconds=1), 0, ProvisionState.ABSENT),  # no retention
        (SubscriptionStatus.EXPIRED, days(5), 30, ProvisionState.DISABLED),  # inconsistent but safe
        (SubscriptionStatus.DISABLED, days(10), 30, ProvisionState.DISABLED),
        (SubscriptionStatus.DISABLED, -days(30), 30, ProvisionState.DISABLED),  # blocked, within retention
        (SubscriptionStatus.DISABLED, -days(400), 30, ProvisionState.ABSENT),  # blocked and long expired
    ],
)
def test_desired_state(
    status: SubscriptionStatus, expires_in: timedelta, retention: int, expected: ProvisionState
) -> None:
    assert prov.desired_state(_sub(status, expires_in), NOW, retention_days=retention) == expected


def test_desired_state_default_retention_is_30_days() -> None:
    assert prov.desired_state(_sub(SubscriptionStatus.EXPIRED, -days(29)), NOW) == ProvisionState.DISABLED
    assert prov.desired_state(_sub(SubscriptionStatus.EXPIRED, -days(31)), NOW) == ProvisionState.ABSENT


@pytest.mark.parametrize(
    ("attempts", "seconds"), [(0, 30), (1, 30), (2, 60), (3, 120), (6, 960), (7, 1800), (50, 1800)]
)
def test_backoff_delay_is_exponential_and_capped(attempts: int, seconds: int) -> None:
    assert prov.backoff_delay(attempts) == timedelta(seconds=seconds)


def _panel(kind: PanelKind, options: str | None = None) -> Panel:
    return Panel(id=7, name="p", kind=kind, url="https://p", options=options)


def test_account_email_per_kind() -> None:
    assert prov.account_email(_panel(PanelKind.XUI), 42, 5) == "tg42-5"
    assert prov.account_email(_panel(PanelKind.REMNAWAVE), 42, None) == "tg_42"
    assert prov.account_email(_panel(PanelKind.PASARGUARD), 42, None) == "tg_42"
    custom = _panel(PanelKind.REMNAWAVE, json.dumps({"username_prefix": "vpn-"}))
    assert prov.account_email(custom, 42, None) == "vpn-42"
    with pytest.raises(ValueError):
        prov.account_email(_panel(PanelKind.XUI), 42, None)


@pytest.mark.parametrize("options", [None, "", "not json", "[1, 2]", json.dumps({"username_prefix": ""})])
def test_bad_or_empty_options_fall_back_to_default_prefix(options: str | None) -> None:
    assert prov.username_prefix(_panel(PanelKind.REMNAWAVE, options)) == "tg_"


@pytest.mark.parametrize(
    ("kind", "email", "managed"),
    [
        (PanelKind.XUI, "tg42-5", True),
        (PanelKind.XUI, "tg42", False),
        (PanelKind.XUI, "tg42-5x", False),
        (PanelKind.XUI, "xtg42-5", False),
        (PanelKind.XUI, "alice", False),
        (PanelKind.XUI, "tg٤٢-5", False),  # non-ASCII digits
        (PanelKind.REMNAWAVE, "tg_42", True),
        (PanelKind.REMNAWAVE, "tg_", False),
        (PanelKind.REMNAWAVE, "tg_42a", False),
        (PanelKind.REMNAWAVE, "tg_٤٢", False),
        (PanelKind.REMNAWAVE, "admin", False),
        (PanelKind.PASARGUARD, "tg_42", True),
        (PanelKind.PASARGUARD, "admin", False),
    ],
)
def test_is_managed_email(kind: PanelKind, email: str, managed: bool) -> None:
    assert prov.is_managed_email(_panel(kind), email) is managed


def test_derive_sub_id_is_deterministic_and_opaque() -> None:
    uuid = "0b7e3f0e-8c1d-4b8e-9d55-3c2f1f0a9e11"
    sub_id = prov.derive_sub_id(uuid, "tg1-1")
    assert sub_id == prov.derive_sub_id(uuid, "tg1-1")
    assert len(sub_id) == 16
    assert sub_id.isalnum() and sub_id == sub_id.lower()
    assert sub_id not in uuid.replace("-", "")
    assert prov.derive_sub_id("another", "tg1-1") != sub_id
    assert prov.derive_sub_id(uuid, "tg1-2") != sub_id


@pytest.mark.parametrize(
    ("params", "flow", "expected"),
    [
        ({"type": "tcp", "security": "reality"}, "xtls-rprx-vision", "xtls-rprx-vision"),
        ({"type": "tcp", "security": "tls"}, "xtls-rprx-vision", "xtls-rprx-vision"),
        ({"type": "xhttp", "security": "reality"}, "xtls-rprx-vision", ""),
        ({"type": "grpc", "security": "tls"}, "xtls-rprx-vision", ""),
        ({"type": "tcp", "security": "none"}, "xtls-rprx-vision", ""),
        ({"type": "tcp", "security": "reality"}, "", ""),
        ({}, "xtls-rprx-vision", ""),
    ],
)
def test_account_flow(params: dict[str, str], flow: str, expected: str) -> None:
    inbound = Inbound(id=1, panel_id=1, remote_id=1, name="i", params=json.dumps(params), flow=flow)
    assert prov.account_flow(inbound) == expected


def test_account_flow_with_malformed_params_or_no_inbound() -> None:
    assert prov.account_flow(Inbound(params="{broken", flow="xtls-rprx-vision")) == ""
    assert prov.account_flow(None) == ""


# ---------------------------------------------------------------------------------------------
# build_spec
# ---------------------------------------------------------------------------------------------
async def test_build_spec_xui(session_factory: SessionFactory) -> None:
    settings = make_settings(xui_device_limit="ip")
    async with session_factory.begin() as s:
        user = await add_user(s, 42, username="bob")
        panel, (inbound,) = await add_xui_panel(s)
        sub = await add_sub(s, 42, expires_at=NOW + days(10), device_limit=2, traffic_gb=50)
        provision = Provision(subscription_id=sub.id, panel_id=panel.id, inbound_id=inbound.id)
        spec = prov.build_spec(sub, user, provision, panel, inbound, settings, now=NOW)
    assert spec.email == f"tg42-{inbound.id}"
    assert spec.uuid == sub.client_uuid
    assert spec.enabled is True
    assert spec.expires_at == sub.expires_at
    assert spec.device_limit == 2
    assert spec.traffic_limit_bytes == 50 * GIB
    assert spec.tg_id == 42
    assert spec.sub_id == prov.derive_sub_id(sub.client_uuid, spec.email)
    assert spec.flow == "xtls-rprx-vision"
    assert "42" in spec.comment and "@bob" in spec.comment


async def test_build_spec_remnawave_and_stored_email(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        user = await add_user(s, 42, username=None)
        panel = await add_rw_panel(s, options={"username_prefix": "vpn_"})
        sub = await add_sub(s, 42, expires_at=NOW - days(1), status=SubscriptionStatus.EXPIRED)
        fresh = Provision(subscription_id=sub.id, panel_id=panel.id)
        stored = Provision(subscription_id=sub.id, panel_id=panel.id, remote_email="tg_42")
        spec = prov.build_spec(sub, user, fresh, panel, None, settings, now=NOW)
        kept = prov.build_spec(sub, user, stored, panel, None, settings, now=NOW)
    assert spec.email == "vpn_42"
    assert spec.flow == ""
    assert spec.enabled is False  # expired
    assert spec.comment == "tg:42"
    assert kept.email == "tg_42"  # prefix changes never orphan existing accounts


# ---------------------------------------------------------------------------------------------
# ensure_provisions / backfill
# ---------------------------------------------------------------------------------------------
async def test_ensure_provisions_creates_one_per_active_target(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        xui, inbounds = await add_xui_panel(s, inbounds=3)
        inbounds[2].is_active = False
        await add_xui_panel(s, is_active=False, name="off")
        rw = await add_rw_panel(s)
        # ensure_provisions judges the subscription against the real clock, not the fixed test time.
        sub = await add_sub(s, 1, expires_at=utcnow() + days(5))
        await prov.ensure_provisions(s, sub)
        await prov.ensure_provisions(s, sub)  # idempotent
        sub_id = sub.id
    rows = await provisions_of(session_factory, sub_id)
    assert {(p.panel_id, p.inbound_id, p.inbound_key) for p in rows} == {
        (xui.id, inbounds[0].id, inbounds[0].id),
        (xui.id, inbounds[1].id, inbounds[1].id),
        (rw.id, None, 0),
    }
    assert all(p.actual_state == ProvisionState.ABSENT and p.synced_revision == 0 for p in rows)
    assert all(p.desired_state == ProvisionState.ENABLED for p in rows)


async def test_ensure_provisions_removes_nothing(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        _, (inbound,) = await add_xui_panel(s)
        sub = await add_sub(s, 1, expires_at=NOW + days(5))
        await prov.ensure_provisions(s, sub)
        inbound.is_active = False
        await prov.ensure_provisions(s, sub)
        sub_id = sub.id
    assert len(await provisions_of(session_factory, sub_id)) == 1


async def test_ensure_provisions_flushes_a_new_subscription(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        await add_rw_panel(s)
        sub = Subscription(
            user_id=1,
            client_uuid="c",
            status=SubscriptionStatus.ACTIVE,
            expires_at=NOW + days(1),
            revision=1,
        )
        s.add(sub)
        await prov.ensure_provisions(s, sub)
        assert sub.id is not None
    assert len(await provisions_of(session_factory)) == 1


async def test_backfill_skips_subscriptions_beyond_retention(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        for uid in (1, 2, 3, 4):
            await add_user(s, uid)
        await add_xui_panel(s, inbounds=2)
        await add_sub(s, 1, expires_at=NOW + days(5))
        await add_sub(s, 2, expires_at=NOW - days(5), status=SubscriptionStatus.EXPIRED)
        await add_sub(s, 3, expires_at=NOW - days(60), status=SubscriptionStatus.EXPIRED)
        await add_sub(s, 4, expires_at=NOW - days(60), status=SubscriptionStatus.DISABLED)
    async with session_factory.begin() as s:
        assert await prov.backfill_provisions(s, settings, NOW) == 4  # users 3 and 4 are past retention
    async with session_factory.begin() as s:
        assert await prov.backfill_provisions(s, settings, NOW) == 0
    rows = await provisions_of(session_factory)
    states = sorted(p.desired_state for p in rows)
    assert states.count(ProvisionState.ENABLED) == 2
    assert states.count(ProvisionState.DISABLED) == 2


# ---------------------------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------------------------
async def _world(
    session_factory: SessionFactory, *, users: int = 1, expires_in: timedelta = TEN_DAYS
) -> tuple[FakeRegistry, Panel, Inbound, list[int]]:
    """One 3x-ui panel with one inbound and ``users`` active subscriptions (no provisions yet)."""
    async with session_factory.begin() as s:
        panel, (inbound,) = await add_xui_panel(s)
        sub_ids = []
        for uid in range(1, users + 1):
            await add_user(s, uid)
            sub_ids.append((await add_sub(s, uid, expires_at=NOW + expires_in)).id)
    return FakeRegistry({panel.id: FakePanel()}), panel, inbound, sub_ids


async def test_reconcile_converges_panel_to_db(session_factory: SessionFactory, settings: Settings) -> None:
    registry, panel, inbound, (sub_id,) = await _world(session_factory)
    fake = registry.panels[panel.id]

    report = await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert (report.provisions_created, report.synced, report.failed, report.skipped) == (1, 1, 0, 0)
    account = fake.accounts[(inbound.remote_id, f"tg1-{inbound.id}")]
    assert account.enabled is True
    assert account.expires_at == NOW + days(10)
    (row,) = await provisions_of(session_factory, sub_id)
    assert row.actual_state == ProvisionState.ENABLED
    assert row.synced_revision == 1
    assert row.remote_email == f"tg1-{inbound.id}"
    assert row.remote_id == account.remote_id and row.remote_short_id == account.short_id
    assert (row.last_error, row.attempts, row.next_attempt_at) == (None, 0, None)

    fake.calls.clear()
    again = await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert fake.calls == []
    assert (again.synced, again.failed, again.provisions_created) == (0, 0, 0)


async def test_revision_bump_triggers_repush(session_factory: SessionFactory) -> None:
    settings = make_settings(xui_device_limit="ip")
    registry, panel, inbound, (sub_id,) = await _world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    async with session_factory.begin() as s:
        sub = await s.get(Subscription, sub_id)
        assert sub is not None
        sub.expires_at = NOW + days(40)
        sub.device_limit = 5
        sub.revision += 1

    report = await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert report.synced == 1
    account = registry.panels[panel.id].accounts[(inbound.remote_id, f"tg1-{inbound.id}")]
    assert (account.expires_at, account.device_limit) == (NOW + days(40), 5)
    (row,) = await provisions_of(session_factory, sub_id)
    assert row.synced_revision == 2


async def test_panel_down_backs_off_without_crashing(session_factory: SessionFactory, settings: Settings) -> None:
    registry, panel, inbound, (sub_id,) = await _world(session_factory)
    fake = registry.panels[panel.id]
    fake.down = True

    report = await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert (report.synced, report.failed) == (0, 1)
    assert "panel down" in report.errors[0]
    (row,) = await provisions_of(session_factory, sub_id)
    assert row.actual_state == ProvisionState.ABSENT
    assert row.attempts == 1
    assert row.last_error and "panel down" in row.last_error
    assert row.next_attempt_at == NOW + timedelta(seconds=30)

    # Still inside the backoff window: not retried.
    fake.down = False
    fake.calls.clear()
    soon = await prov.reconcile(session_factory, registry, settings, now=NOW + timedelta(seconds=10))
    assert fake.calls == [] and soon.synced == 0

    # Second failure doubles the delay.
    fake.down = True
    later = NOW + timedelta(seconds=31)
    await prov.reconcile(session_factory, registry, settings, now=later)
    (row,) = await provisions_of(session_factory, sub_id)
    assert row.attempts == 2
    assert row.next_attempt_at == later + timedelta(seconds=60)

    fake.down = False
    recovered = await prov.reconcile(session_factory, registry, settings, now=later + timedelta(minutes=2))
    assert recovered.synced == 1
    (row,) = await provisions_of(session_factory, sub_id)
    assert (row.actual_state, row.attempts, row.last_error, row.next_attempt_at) == (
        ProvisionState.ENABLED,
        0,
        None,
        None,
    )
    assert (inbound.remote_id, f"tg1-{inbound.id}") in fake.accounts


async def test_failure_streak_postpones_rest_of_panel_batch(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, panel, _, _ = await _world(session_factory, users=5)
    registry.panels[panel.id].down = True

    report = await prov.reconcile(session_factory, registry, settings, now=NOW, scan_orphans=False)

    assert (report.failed, report.skipped) == (prov.PANEL_ABORT_AFTER_FAILURES, 5 - prov.PANEL_ABORT_AFTER_FAILURES)
    rows = await provisions_of(session_factory)
    assert sum(1 for r in rows if r.attempts == 1) == prov.PANEL_ABORT_AFTER_FAILURES
    assert sum(1 for r in rows if r.attempts == 0 and r.next_attempt_at is None) == 2


async def test_unhealthy_panel_is_skipped(session_factory: SessionFactory, settings: Settings) -> None:
    registry, panel, _, _ = await _world(session_factory)
    async with session_factory.begin() as s:
        await s.execute(update(Panel).values(consecutive_failures=settings.health_failures_before_alert))

    report = await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert report.panels_down == [panel.id]
    assert registry.panels[panel.id].calls == []
    assert report.synced == report.failed == 0


async def test_backend_construction_failure_is_recorded(session_factory: SessionFactory, settings: Settings) -> None:
    registry, panel, _, (sub_id,) = await _world(session_factory)
    registry.broken.add(panel.id)

    report = await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert report.failed == 1
    (row,) = await provisions_of(session_factory, sub_id)
    assert row.last_error and row.last_error.startswith("panel unavailable")
    assert row.next_attempt_at == NOW + timedelta(seconds=30)


async def test_unexpected_backend_exception_is_contained(session_factory: SessionFactory, settings: Settings) -> None:
    registry, panel, _, (sub_id,) = await _world(session_factory)

    async def boom(*_: object) -> RemoteAccount:
        raise RuntimeError("bug")

    registry.panels[panel.id].upsert_account = boom  # type: ignore[method-assign,assignment]
    report = await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert report.failed == 1
    (row,) = await provisions_of(session_factory, sub_id)
    assert row.last_error == "bug"


async def test_expired_within_retention_is_disabled_then_deleted_after(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, panel, inbound, (sub_id,) = await _world(session_factory, expires_in=days(1))
    fake = registry.panels[panel.id]
    key = (inbound.remote_id, f"tg1-{inbound.id}")
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert fake.accounts[key].enabled is True

    # Expired (expire_due bumps the revision): account disabled, kept.
    async with session_factory.begin() as s:
        await s.execute(
            update(Subscription).values(status=SubscriptionStatus.EXPIRED, revision=Subscription.revision + 1)
        )
    await prov.reconcile(session_factory, registry, settings, now=NOW + days(2))
    assert fake.accounts[key].enabled is False
    (row,) = await provisions_of(session_factory, sub_id)
    assert row.actual_state == ProvisionState.DISABLED

    # Nothing changes in the DB, but the retention period elapses: the account is deleted.
    fake.calls.clear()
    report = await prov.reconcile(session_factory, registry, settings, now=NOW + days(1 + 31))
    assert report.synced == 1
    assert key not in fake.accounts
    assert ("delete", inbound.remote_id, key[1]) in fake.calls
    (row,) = await provisions_of(session_factory, sub_id)
    assert (row.actual_state, row.desired_state, row.remote_id, row.remote_short_id) == (
        ProvisionState.ABSENT,
        ProvisionState.ABSENT,
        None,
        None,
    )

    # Stays deleted, no more calls.
    fake.calls.clear()
    await prov.reconcile(session_factory, registry, settings, now=NOW + days(40))
    assert fake.calls == []


async def test_blocked_subscription_within_retention_is_kept_disabled(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, panel, inbound, _ = await _world(session_factory, expires_in=-days(10))
    async with session_factory.begin() as s:
        await s.execute(update(Subscription).values(status=SubscriptionStatus.DISABLED))
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    account = registry.panels[panel.id].accounts[(inbound.remote_id, f"tg1-{inbound.id}")]
    assert account.enabled is False


async def test_blocked_subscription_is_deleted_after_retention(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, panel, inbound, (sub_id,) = await _world(session_factory, expires_in=days(1))
    fake = registry.panels[panel.id]
    key = (inbound.remote_id, f"tg1-{inbound.id}")
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    async with session_factory.begin() as s:
        await s.execute(
            update(Subscription).values(status=SubscriptionStatus.DISABLED, revision=Subscription.revision + 1)
        )
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert fake.accounts[key].enabled is False

    # The period ended long ago: the blocked account goes away like any expired one.
    await prov.reconcile(session_factory, registry, settings, now=NOW + days(1 + 31))
    assert key not in fake.accounts
    (row,) = await provisions_of(session_factory, sub_id)
    assert (row.desired_state, row.actual_state) == (ProvisionState.ABSENT, ProvisionState.ABSENT)


async def test_blocking_long_expired_user_does_not_recreate_accounts(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, panel, inbound, (sub_id,) = await _world(session_factory, expires_in=days(1))
    fake = registry.panels[panel.id]
    key = (inbound.remote_id, f"tg1-{inbound.id}")
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    async with session_factory.begin() as s:
        await s.execute(
            update(Subscription).values(status=SubscriptionStatus.EXPIRED, revision=Subscription.revision + 1)
        )
    later = NOW + days(90)
    await prov.reconcile(session_factory, registry, settings, now=later)
    assert key not in fake.accounts

    async with session_factory.begin() as s:
        sub = await s.get(Subscription, sub_id)
        assert sub is not None
        await subscriptions.set_status(s, sub, SubscriptionStatus.DISABLED, actor_id=1, now=later)
    fake.calls.clear()
    await prov.reconcile(session_factory, registry, settings, now=later)
    assert key not in fake.accounts
    assert not [c for c in fake.calls if c[0] == "upsert"]

    # A panel added later gets no provision for the blocked, long-expired subscription either.
    async with session_factory.begin() as s:
        await add_xui_panel(s, inbounds=1)
        assert await prov.backfill_provisions(s, settings, later) == 0


async def test_absent_and_should_stay_absent_needs_no_call(session_factory: SessionFactory, settings: Settings) -> None:
    registry, panel, _, (sub_id,) = await _world(session_factory, expires_in=-days(100))
    async with session_factory.begin() as s:
        await s.execute(update(Subscription).values(status=SubscriptionStatus.EXPIRED))
        sub = await s.get(Subscription, sub_id)
        assert sub is not None
        await prov.ensure_provisions(s, sub)  # created with desired ABSENT, actual ABSENT, synced 0

    report = await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert registry.panels[panel.id].calls == []
    assert report.synced == 1  # just marks the revision as synced
    (row,) = await provisions_of(session_factory, sub_id)
    assert row.synced_revision == 1


async def test_inactive_inbound_provisions_are_left_alone(session_factory: SessionFactory, settings: Settings) -> None:
    registry, panel, _, _ = await _world(session_factory)
    async with session_factory.begin() as s:
        sub = await s.get(Subscription, 1)
        assert sub is not None
        await prov.ensure_provisions(s, sub)
        await s.execute(update(Inbound).values(is_active=False))
    report = await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert registry.panels[panel.id].calls == []
    assert report.synced == 0


async def test_reconcile_pushes_new_inbound_to_existing_subscriptions(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, panel, _, (sub_id,) = await _world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    async with session_factory.begin() as s:
        new = Inbound(panel_id=panel.id, remote_id=9, name="new", params=json.dumps({"type": "xhttp"}), flow="")
        s.add(new)
        await s.flush()
        new_id = new.id

    report = await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert (report.provisions_created, report.synced) == (1, 1)
    assert (9, f"tg1-{new_id}") in registry.panels[panel.id].accounts
    assert len(await provisions_of(session_factory, sub_id)) == 2


async def test_remnawave_provisioning(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 77)
        panel = await add_rw_panel(s)
        sub_id = (await add_sub(s, 77, expires_at=NOW + days(3))).id
    fake = FakePanel(kind=PanelKind.REMNAWAVE)
    report = await prov.reconcile(session_factory, FakeRegistry({panel.id: fake}), settings, now=NOW)
    assert report.synced == 1
    assert fake.accounts[(None, "tg_77")].enabled is True
    (row,) = await provisions_of(session_factory, sub_id)
    assert row.inbound_id is None and row.remote_short_id is not None


async def test_reconcile_holds_no_transaction_during_panel_calls(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, panel, _, _ = await _world(session_factory, users=3)
    tracker = TxTracker(session_factory)
    tracker.guard(registry.panels[panel.id])
    registry.panels[panel.id].accounts[(1, "tg999-1")] = RemoteAccount("tg999-1", "x", True, None, 0, 0)

    await prov.reconcile(tracker, registry, settings, now=NOW)  # type: ignore[arg-type]
    await prov.sync_subscription(tracker, registry, settings, 1, now=NOW)  # type: ignore[arg-type]

    assert registry.panels[panel.id].calls  # the panel was actually used
    assert tracker.violations == []


async def test_reconcile_batch_size_limits_work(session_factory: SessionFactory, settings: Settings) -> None:
    registry, _, _, _ = await _world(session_factory, users=4)
    first = await prov.reconcile(session_factory, registry, settings, now=NOW, batch_size=3)
    second = await prov.reconcile(session_factory, registry, settings, now=NOW, batch_size=3)
    assert (first.synced, second.synced) == (3, 1)


# ---------------------------------------------------------------------------------------------
# Orphans
# ---------------------------------------------------------------------------------------------
def _remote(email: str, uuid: str = "orphan-uuid") -> RemoteAccount:
    return RemoteAccount(email=email, uuid=uuid, enabled=True, expires_at=None, device_limit=0, traffic_limit_bytes=0)


async def test_orphans_are_reported_but_kept_by_default(session_factory: SessionFactory, settings: Settings) -> None:
    registry, panel, inbound, _ = await _world(session_factory)
    fake = registry.panels[panel.id]
    fake.accounts[(inbound.remote_id, "tg999-1")] = _remote("tg999-1")
    fake.accounts[(inbound.remote_id, "alice")] = _remote("alice")  # not ours: ignored

    report = await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert [(o.email, o.target, o.deleted) for o in report.orphans] == [("tg999-1", inbound.remote_id, False)]
    assert (inbound.remote_id, "tg999-1") in fake.accounts
    assert not any(email == f"tg1-{inbound.id}" for email in (o.email for o in report.orphans))


async def test_orphans_are_deleted_when_enabled(session_factory: SessionFactory) -> None:
    settings = make_settings(delete_orphan_clients=True)
    registry, panel, inbound, _ = await _world(session_factory)
    fake = registry.panels[panel.id]
    fake.accounts[(inbound.remote_id, "tg999-1")] = _remote("tg999-1")

    report = await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert [(o.email, o.deleted) for o in report.orphans] == [("tg999-1", True)]
    assert (inbound.remote_id, "tg999-1") not in fake.accounts
    assert (inbound.remote_id, f"tg1-{inbound.id}") in fake.accounts  # our own account survives


async def test_orphan_deletion_refused_on_panel_without_provisions(session_factory: SessionFactory) -> None:
    settings = make_settings(delete_orphan_clients=True)
    async with session_factory.begin() as s:
        panel, (inbound,) = await add_xui_panel(s)
    fake = FakePanel()
    fake.accounts[(inbound.remote_id, "tg5-1")] = _remote("tg5-1")

    orphans, errors = await prov.find_orphans(session_factory, FakeRegistry({panel.id: fake}), settings, delete=True)

    assert [(o.email, o.deleted) for o in orphans] == [("tg5-1", False)]
    assert errors == []
    assert (inbound.remote_id, "tg5-1") in fake.accounts


async def test_deleted_on_purpose_but_still_present_is_an_orphan(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 77)
        panel = await add_rw_panel(s)
        sub = await add_sub(s, 77, expires_at=NOW - days(100), status=SubscriptionStatus.EXPIRED)
        await prov.ensure_provisions(s, sub)
    fake = FakePanel(kind=PanelKind.REMNAWAVE)
    registry = FakeRegistry({panel.id: fake})
    await prov.reconcile(session_factory, registry, settings, now=NOW, scan_orphans=False)
    fake.accounts[(None, "tg_77")] = _remote("tg_77")

    orphans, _ = await prov.find_orphans(session_factory, registry, settings)

    assert [o.email for o in orphans] == ["tg_77"]


async def test_orphan_scan_errors_are_reported_not_raised(session_factory: SessionFactory, settings: Settings) -> None:
    registry, panel, _, _ = await _world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    registry.panels[panel.id].fail_next = 1  # list_accounts fails

    orphans, errors = await prov.find_orphans(session_factory, registry, settings)

    assert orphans == []
    assert len(errors) == 1 and f"panel {panel.id}" in errors[0]


async def test_orphan_delete_failure_is_reported(session_factory: SessionFactory) -> None:
    settings = make_settings(delete_orphan_clients=True)
    registry, panel, inbound, _ = await _world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    fake = registry.panels[panel.id]
    fake.accounts[(inbound.remote_id, "tg999-1")] = _remote("tg999-1")

    async def failing_delete(*_: object) -> None:
        raise RuntimeError("nope")

    fake.delete_account = failing_delete  # type: ignore[method-assign,assignment]
    orphans, _ = await prov.find_orphans(session_factory, registry, settings, delete=True)
    assert [(o.email, o.deleted, o.error) for o in orphans] == [("tg999-1", False, "nope")]


async def test_orphan_scan_skips_down_and_excluded_panels(session_factory: SessionFactory, settings: Settings) -> None:
    registry, panel, inbound, _ = await _world(session_factory)
    registry.panels[panel.id].accounts[(inbound.remote_id, "tg999-1")] = _remote("tg999-1")
    orphans, _ = await prov.find_orphans(session_factory, registry, settings, exclude={panel.id})
    assert orphans == []
    async with session_factory.begin() as s:
        await s.execute(update(Panel).values(consecutive_failures=99))
    orphans, _ = await prov.find_orphans(session_factory, registry, settings)
    assert orphans == []


# ---------------------------------------------------------------------------------------------
# sync_subscription
# ---------------------------------------------------------------------------------------------
async def test_sync_subscription_ignores_backoff(session_factory: SessionFactory, settings: Settings) -> None:
    registry, panel, inbound, (sub_id,) = await _world(session_factory)
    fake = registry.panels[panel.id]
    fake.down = True
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    fake.down = False

    result = await prov.sync_subscription(session_factory, registry, settings, sub_id, now=NOW)

    assert result.ok and (result.synced, result.failed, result.skipped) == (1, 0, 0)
    assert (inbound.remote_id, f"tg1-{inbound.id}") in fake.accounts


async def test_sync_subscription_creates_missing_provisions(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, panel, _, (sub_id,) = await _world(session_factory)
    result = await prov.sync_subscription(session_factory, registry, settings, sub_id, now=NOW)
    assert result.synced == 1
    assert len(registry.panels[panel.id].accounts) == 1


async def test_sync_subscription_reports_failures_and_down_panels(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, panel, _, (sub_id,) = await _world(session_factory)
    async with session_factory.begin() as s:
        rw = await add_rw_panel(s)
        rw.consecutive_failures = 50
    registry.panels[panel.id].fail_next = 1

    result = await prov.sync_subscription(session_factory, registry, settings, sub_id, now=NOW)

    assert not result.ok
    assert (result.synced, result.failed, result.skipped) == (0, 1, 1)
    assert len(result.errors) == 1


async def test_sync_subscription_unknown_id(session_factory: SessionFactory, settings: Settings) -> None:
    result = await prov.sync_subscription(session_factory, FakeRegistry(), settings, 12345, now=NOW)
    assert result.found is False and not result.ok


async def test_sync_result_ok_property() -> None:
    assert prov.SyncResult(1).ok
    assert not replace(prov.SyncResult(1), skipped=1).ok
