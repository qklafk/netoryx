"""Regression tests for provisioning convergence: orphan races, traffic periods, config drift, flow agreement,
deactivated targets, stale snapshots and per-panel fairness."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select, update

from app.config import Settings
from app.db.models import (
    Inbound,
    Panel,
    PanelKind,
    Plan,
    Provision,
    ProvisionState,
    PurchaseKind,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.db.session import SessionFactory
from app.services import links, subscriptions
from app.services import provisioning as prov
from app.services.panels import inventory
from app.services.panels.base import AccountSpec, InboundInfo, PanelError, RemoteAccount
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

GIB = 1024**3
VISION = "xtls-rprx-vision"


class GatedPanel(FakePanel):
    """FakePanel whose first upsert matching ``gate`` blocks until ``release`` is set."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.gate: Callable[[AccountSpec], bool] | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.enabled_pushed = asyncio.Event()

    async def upsert_account(self, target: int | None, spec: AccountSpec) -> RemoteAccount:
        if self.gate is not None and self.gate(spec):
            self.gate = None
            self.entered.set()
            await self.release.wait()
        account = await super().upsert_account(target, spec)
        if spec.enabled:
            self.enabled_pushed.set()
        return account


@dataclass
class SquadPanel(FakePanel):
    """Remnawave-like fake with ``list_squads`` and the squad-cache hook."""

    kind: PanelKind = PanelKind.REMNAWAVE
    squads: list[str] = field(default_factory=lambda: ["sq-a"])
    squad_cache_resets: int = 0

    async def list_squads(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(uuid=uuid) for uuid in self.squads]

    def invalidate_squads(self) -> None:
        self.squad_cache_resets += 1


@dataclass
class PickyPanel(FakePanel):
    """Fails every upsert of the emails in ``broken``."""

    broken: set[str] = field(default_factory=set)

    async def upsert_account(self, target: int | None, spec: AccountSpec) -> RemoteAccount:
        if spec.email in self.broken:
            self.calls.append(("upsert", target, spec.email))
            raise PanelError(f"broken {spec.email}")
        return await super().upsert_account(target, spec)


async def _bump(session_factory: SessionFactory, sub_id: int, **values: Any) -> None:
    async with session_factory.begin() as s:
        await s.execute(
            update(Subscription).where(Subscription.id == sub_id).values(revision=Subscription.revision + 1, **values)
        )


# ---------------------------------------------------------------------------------------------
# 1. Orphan scan vs. a revived subscription
# ---------------------------------------------------------------------------------------------
async def test_orphan_scan_keeps_account_of_renewed_subscription(session_factory: SessionFactory) -> None:
    settings = make_settings(delete_orphan_clients=True)
    async with session_factory.begin() as s:
        await add_user(s, 42)
        panel, (inbound,) = await add_xui_panel(s)
        sub = await add_sub(s, 42, expires_at=NOW - days(40), status=SubscriptionStatus.EXPIRED)
        await prov.ensure_provisions(s, sub)
        sub_id = sub.id
    fake = FakePanel()
    registry = FakeRegistry({panel.id: fake})
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    (row,) = await provisions_of(session_factory, sub_id)
    assert (row.desired_state, row.actual_state) == (ProvisionState.ABSENT, ProvisionState.ABSENT)

    # Renewal (stored provision states untouched) and a sync that already created the account but has not
    # stored its outcome yet (another panel is slow).
    await _bump(session_factory, sub_id, status=SubscriptionStatus.ACTIVE, expires_at=NOW + days(30))
    key = (inbound.remote_id, f"tg42-{inbound.id}")
    fake.accounts[key] = RemoteAccount(key[1], "u", True, NOW + days(30), 0, 0)

    orphans, errors = await prov.find_orphans(session_factory, registry, settings, delete=True, now=NOW)

    assert (orphans, errors) == ([], [])
    assert key in fake.accounts


async def test_orphan_scan_still_reports_accounts_of_dead_subscriptions(session_factory: SessionFactory) -> None:
    settings = make_settings(delete_orphan_clients=True)
    async with session_factory.begin() as s:
        await add_user(s, 42)
        panel, (inbound,) = await add_xui_panel(s)
        sub = await add_sub(s, 42, expires_at=NOW - days(40), status=SubscriptionStatus.EXPIRED)
        await prov.ensure_provisions(s, sub)
    fake = FakePanel()
    registry = FakeRegistry({panel.id: fake})
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    key = (inbound.remote_id, f"tg42-{inbound.id}")
    fake.accounts[key] = RemoteAccount(key[1], "u", True, None, 0, 0)

    orphans, _ = await prov.find_orphans(session_factory, registry, settings, delete=True, now=NOW)

    assert [(o.email, o.deleted) for o in orphans] == [(key[1], True)]


# ---------------------------------------------------------------------------------------------
# 2. Used traffic is reset when a new paid period starts
# ---------------------------------------------------------------------------------------------
async def _traffic_world(
    session_factory: SessionFactory, *, traffic_gb: int = 100
) -> tuple[FakeRegistry, FakePanel, tuple[int | None, str], int, int]:
    async with session_factory.begin() as s:
        await add_user(s, 42, balance_kop=100_000)
        panel, (inbound,) = await add_xui_panel(s)
        plan = await add_plan(s, days=30, price_kop=10_000, traffic_gb=traffic_gb)
        user = await s.get(User, 42)
        assert user is not None
        await subscriptions.purchase_plan(s, make_settings(), user, plan, now=NOW)
        sub = await subscriptions.get_for_user(s, 42)
        assert sub is not None
        sub_id, plan_id = sub.id, plan.id
    fake = FakePanel()
    return FakeRegistry({panel.id: fake}), fake, (inbound.remote_id, f"tg42-{inbound.id}"), sub_id, plan_id


async def _buy_again(session_factory: SessionFactory, plan_id: int, now: Any) -> None:
    async with session_factory.begin() as s:
        user, plan = await s.get(User, 42), await s.get(Plan, plan_id)
        assert user is not None and plan is not None
        await subscriptions.purchase_plan(s, make_settings(), user, plan, now=now)


async def test_renewal_resets_used_traffic_before_the_account_is_pushed(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, fake, key, sub_id, plan_id = await _traffic_world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert ("reset_traffic", key[0], key[1]) not in fake.calls  # a brand-new account needs no reset
    fake.accounts[key].used_bytes = 100 * GIB  # quota of period 1 used up

    await _buy_again(session_factory, plan_id, NOW + days(1))
    fake.calls.clear()
    await prov.reconcile(session_factory, registry, settings, now=NOW + days(1))

    assert fake.calls == [("reset_traffic", key[0], key[1]), ("upsert", key[0], key[1])]
    assert fake.accounts[key].used_bytes == 0
    async with session_factory() as s:
        sub = await s.get(Subscription, sub_id)
        (row,) = (await s.scalars(select(Provision))).all()
        assert sub is not None and row.traffic_epoch == sub.traffic_epoch == 1

    fake.calls.clear()
    await prov.reconcile(session_factory, registry, settings, now=NOW + days(1))
    assert fake.calls == []  # reset once per period


async def test_unlimited_plans_and_free_days_do_not_reset_traffic(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, fake, _, sub_id, plan_id = await _traffic_world(session_factory, traffic_gb=0)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    await _buy_again(session_factory, plan_id, NOW)
    async with session_factory.begin() as s:
        user = await s.get(User, 42)
        assert user is not None
        await subscriptions.grant_days(s, settings, user, 5, kind=PurchaseKind.PROMO, now=NOW)
        sub = await s.get(Subscription, sub_id)
        assert sub is not None and sub.traffic_epoch == 1  # the promo days did not start a new period
    fake.calls.clear()

    await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert [c[0] for c in fake.calls] == ["upsert"]  # unlimited: nothing to reset


async def test_traffic_reset_failure_is_retried(session_factory: SessionFactory, settings: Settings) -> None:
    registry, fake, _, _, plan_id = await _traffic_world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    await _buy_again(session_factory, plan_id, NOW)
    fake.fail_next = 1  # the reset fails

    report = await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert report.failed == 1
    report = await prov.reconcile(session_factory, registry, settings, now=NOW + timedelta(minutes=1))
    assert report.synced == 1
    assert [c[0] for c in fake.calls][-2:] == ["reset_traffic", "upsert"]


# ---------------------------------------------------------------------------------------------
# 3. Panel / inbound configuration changes are re-pushed
# ---------------------------------------------------------------------------------------------
def _ws_inbound() -> InboundInfo:
    return InboundInfo(
        remote_id=1,
        remark="in-443",
        protocol="vless",
        port=443,
        enabled=True,
        params={"type": "ws", "security": "tls", "sni": "x.example.com", "path": "/ws", "flow_allowed": False},
    )


async def test_inbound_flow_cleared_by_refresh_is_repushed(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        panel, (inbound,) = await add_xui_panel(s)
        await add_sub(s, 1, expires_at=NOW + days(10))
    fake = FakePanel()
    registry = FakeRegistry({panel.id: fake})
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    original_upsert = fake.upsert_account
    pushed: list[AccountSpec] = []

    async def recording(target: int | None, spec: AccountSpec) -> RemoteAccount:
        pushed.append(spec)
        return await original_upsert(target, spec)

    fake.upsert_account = recording  # type: ignore[method-assign]
    fake.inbounds = [_ws_inbound()]
    await inventory.refresh_inbounds(session_factory, registry, panel.id)

    report = await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert report.synced == 1
    assert [spec.flow for spec in pushed] == [""]
    assert inbound.id is not None
    again = await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert again.synced == 0


async def test_remnawave_squad_added_on_panel_is_repushed(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 77)
        panel = await add_rw_panel(s)  # no squad_uuids: "all squads"
        await add_sub(s, 77, expires_at=NOW + days(10))
    fake = SquadPanel()
    registry = FakeRegistry({panel.id: fake})
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    fake.calls.clear()
    assert (await prov.reconcile(session_factory, registry, settings, now=NOW)).synced == 0

    fake.squads = ["sq-a", "sq-b"]  # the admin adds a country
    report = await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert report.synced == 1
    assert fake.calls == [("upsert", None, "tg_77")]
    assert fake.squad_cache_resets == 1  # the backend's "all squads" cache was dropped before the push


async def test_panel_option_change_is_repushed(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 77)
        panel = await add_rw_panel(s, options={"squad_uuids": ["sq-a"]})
        await add_sub(s, 77, expires_at=NOW + days(10))
    fake = FakePanel(kind=PanelKind.REMNAWAVE)
    registry = FakeRegistry({panel.id: fake})
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    async with session_factory.begin() as s:
        await s.execute(
            update(Panel).values(options=json.dumps({"squad_uuids": ["sq-a"], "traffic_limit_strategy": "MONTH"}))
        )
    fake.calls.clear()

    report = await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert report.synced == 1 and fake.calls == [("upsert", None, "tg_77")]


async def test_mark_dirty_targets_one_inbound(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        panel, inbounds = await add_xui_panel(s, inbounds=2)
        await add_sub(s, 1, expires_at=NOW + days(10))
    fake = FakePanel()
    registry = FakeRegistry({panel.id: fake})
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    async with session_factory.begin() as s:
        assert await prov.mark_dirty(s, panel_id=panel.id, inbound_id=inbounds[1].id) == 1
    fake.calls.clear()

    await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert fake.calls == [("upsert", inbounds[1].remote_id, f"tg1-{inbounds[1].id}")]


# ---------------------------------------------------------------------------------------------
# 4. Panel account flow agrees with the link flow
# ---------------------------------------------------------------------------------------------
MLKEM = "mlkem768x25519plus.native.0rtt.abc"


@pytest.mark.parametrize(
    "params",
    [
        {"type": "tcp", "security": "reality", "flow_allowed": True},
        {"type": "tcp", "security": "tls", "flow_allowed": True},
        {"type": "raw", "security": "tls", "flow_allowed": True},
        {"type": "tcp", "security": "none", "flow_allowed": False},
        {"type": "tcp", "security": "none", "encryption": MLKEM, "flow_allowed": True},
        {"type": "xhttp", "security": "tls", "encryption": MLKEM, "path": "/x", "flow_allowed": True},
        {"type": "xhttp", "security": "reality", "flow_allowed": False},
        {"type": "grpc", "security": "tls", "serviceName": "g", "flow_allowed": False},
        {"type": "tcp", "security": "reality", "flow_allowed": False},  # disableFlow on the panel
        {"type": "tcp", "security": "reality"},  # legacy cached params without the hint
        {},
    ],
)
def test_account_flow_matches_link_flow(params: dict[str, Any]) -> None:
    inbound = Inbound(id=1, panel_id=1, remote_id=1, name="i", params=json.dumps(params), flow=VISION)
    link = links.build_vless("11111111-2222-4333-8444-555555555555", "h.example.com", 443, params, "r", None, VISION)
    link_flow = parse_qs(urlsplit(link).query).get("flow", [""])[0]
    assert prov.account_flow(inbound) == link_flow


def test_vless_encryption_inbound_gets_vision_on_the_account() -> None:
    params = {"type": "xhttp", "security": "tls", "encryption": MLKEM, "flow_allowed": True}
    inbound = Inbound(id=1, panel_id=1, remote_id=1, name="i", params=json.dumps(params), flow=VISION)
    assert prov.account_flow(inbound) == VISION


# ---------------------------------------------------------------------------------------------
# 5. Accounts on deactivated inbounds / panels are drained
# ---------------------------------------------------------------------------------------------
async def test_deactivated_inbound_accounts_are_removed_and_restored(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        panel, (inbound,) = await add_xui_panel(s)
        sub_id = (await add_sub(s, 1, expires_at=NOW + days(10))).id
    fake = FakePanel()
    registry = FakeRegistry({panel.id: fake})
    key = (inbound.remote_id, f"tg1-{inbound.id}")
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert fake.accounts[key].enabled

    async with session_factory.begin() as s:
        await s.execute(update(Inbound).values(is_active=False))
    report = await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert report.synced == 1 and key not in fake.accounts
    (row,) = await provisions_of(session_factory, sub_id)
    assert (row.desired_state, row.actual_state) == (ProvisionState.ABSENT, ProvisionState.ABSENT)
    fake.calls.clear()
    await _bump(session_factory, sub_id, status=SubscriptionStatus.DISABLED)  # admin block: nothing left to do
    assert (await prov.reconcile(session_factory, registry, settings, now=NOW)).synced == 0
    assert fake.calls == []

    await _bump(session_factory, sub_id, status=SubscriptionStatus.ACTIVE)
    async with session_factory.begin() as s:
        await s.execute(update(Inbound).values(is_active=True))
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert fake.accounts[key].enabled  # reactivation recreates the account


async def test_blocked_user_on_deactivated_panel_loses_access(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 77)
        panel = await add_rw_panel(s)
        sub_id = (await add_sub(s, 77, expires_at=NOW + days(10))).id
    fake = FakePanel(kind=PanelKind.REMNAWAVE)
    registry = FakeRegistry({panel.id: fake})
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    async with session_factory.begin() as s:
        await s.execute(update(Panel).values(is_active=False))
    await _bump(session_factory, sub_id, status=SubscriptionStatus.DISABLED)

    await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert (None, "tg_77") not in fake.accounts


async def test_sync_subscription_drains_deactivated_targets(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        panel, (inbound,) = await add_xui_panel(s)
        sub_id = (await add_sub(s, 1, expires_at=NOW + days(10))).id
    fake = FakePanel()
    registry = FakeRegistry({panel.id: fake})
    await prov.sync_subscription(session_factory, registry, settings, sub_id, now=NOW)
    async with session_factory.begin() as s:
        await s.execute(update(Inbound).values(is_active=False))

    result = await prov.sync_subscription(session_factory, registry, settings, sub_id, now=NOW)

    assert result.ok and (inbound.remote_id, f"tg1-{inbound.id}") not in fake.accounts


# ---------------------------------------------------------------------------------------------
# 6. A stale reconcile snapshot never overwrites a newer push
# ---------------------------------------------------------------------------------------------
async def test_stale_reconcile_job_is_skipped_after_sync_pushed_newer_revision(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        panel, (inbound,) = await add_xui_panel(s)
        ids = []
        for uid in (1, 2):
            await add_user(s, uid)
            sub = await add_sub(s, uid, expires_at=NOW - days(1), status=SubscriptionStatus.EXPIRED, revision=2)
            ids.append(sub.id)
    fake = GatedPanel()
    registry = FakeRegistry({panel.id: fake})
    first = f"tg1-{inbound.id}"
    fake.gate = lambda spec: spec.email == first
    task = asyncio.create_task(prov.reconcile(session_factory, registry, settings, now=NOW, scan_orphans=False))
    await fake.entered.wait()  # reconcile holds a DISABLED snapshot of user 2 as well

    await _bump(session_factory, ids[1], status=SubscriptionStatus.ACTIVE, expires_at=NOW + days(30))
    result = await prov.sync_subscription(session_factory, registry, settings, ids[1], now=NOW)
    assert result.ok
    fake.release.set()
    report = await task

    second = (inbound.remote_id, f"tg2-{inbound.id}")
    assert fake.accounts[second].enabled is True
    assert report.skipped == 1
    (row,) = await provisions_of(session_factory, ids[1])
    assert (row.synced_revision, row.actual_state) == (3, ProvisionState.ENABLED)


async def test_stale_push_cannot_leave_panel_diverged_from_db(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 100)
        pa, (inbound,) = await add_xui_panel(s, name="a")
        pb = await add_rw_panel(s, name="b")
        sub = await add_sub(s, 100, expires_at=NOW - days(1), status=SubscriptionStatus.EXPIRED, revision=2)
        await prov.ensure_provisions(s, sub)
        sub_id = sub.id
    fa, fb = GatedPanel(), GatedPanel(kind=PanelKind.REMNAWAVE)
    registry = FakeRegistry({pa.id: fa, pb.id: fb})
    key = (inbound.remote_id, f"tg100-{inbound.id}")

    fa.gate = lambda spec: not spec.enabled  # reconcile's DISABLED push to A blocks mid-call
    rtask = asyncio.create_task(prov.reconcile(session_factory, registry, settings, now=NOW, scan_orphans=False))
    await fa.entered.wait()
    await _bump(session_factory, sub_id, status=SubscriptionStatus.ACTIVE, expires_at=NOW + days(30))
    fb.gate = lambda spec: spec.enabled  # panel B is slow for the purchase sync
    stask = asyncio.create_task(prov.sync_subscription(session_factory, registry, settings, sub_id, now=NOW))
    await fb.entered.wait()
    fa.release.set()
    await rtask
    await asyncio.wait_for(fa.enabled_pushed.wait(), 5)  # the purchase sync pushes A after the stale push
    fb.release.set()
    await stask

    assert fa.accounts[key].enabled is True
    await prov.reconcile(session_factory, registry, settings, now=NOW, scan_orphans=False)
    assert fa.accounts[key].enabled is True
    rows = await provisions_of(session_factory, sub_id)
    assert {(r.synced_revision, r.actual_state) for r in rows} == {(3, ProvisionState.ENABLED)}


async def test_store_does_not_overwrite_a_newer_outcome(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        panel, _ = await add_xui_panel(s)
        sub = await add_sub(s, 1, expires_at=NOW + days(10))
        await prov.ensure_provisions(s, sub)
    async with session_factory() as s:
        row = (await s.execute(prov._work_query())).one()
    job = prov._make_job(row, settings, NOW)
    assert job is not None
    async with session_factory.begin() as s:  # somebody else stored a newer revision meanwhile
        await s.execute(update(Provision).values(synced_revision=5, actual_state=ProvisionState.DISABLED))
    run = prov._PanelRun(panel.id, outcomes=[prov._Outcome(job)])

    await prov._store_outcomes(session_factory, [run], NOW)

    (stored,) = await provisions_of(session_factory)
    assert (stored.synced_revision, stored.actual_state) == (5, ProvisionState.DISABLED)


# ---------------------------------------------------------------------------------------------
# 7. Reconcile batches are per panel and failing rows do not pin the head of the queue
# ---------------------------------------------------------------------------------------------
async def test_reconcile_batch_limit_is_per_panel(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        p1, _ = await add_xui_panel(s, name="one")
        p2, _ = await add_xui_panel(s, name="two")
        for uid in range(1, 5):
            await add_user(s, uid)
            await add_sub(s, uid, expires_at=NOW + days(10))
    f1, f2 = FakePanel(), FakePanel()
    registry = FakeRegistry({p1.id: f1, p2.id: f2})

    report = await prov.reconcile(session_factory, registry, settings, now=NOW, batch_size=2, scan_orphans=False)

    assert report.synced == 4
    assert (len(f1.accounts), len(f2.accounts)) == (2, 2)


async def test_failing_rows_do_not_pin_the_head_of_the_queue(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        panel, (inbound,) = await add_xui_panel(s)
        for uid in range(1, 6):
            await add_user(s, uid)
            await add_sub(s, uid, expires_at=NOW + days(10))
    fake = PickyPanel(broken={f"tg{uid}-{inbound.id}" for uid in (1, 2, 3)})
    registry = FakeRegistry({panel.id: fake})
    first = await prov.reconcile(session_factory, registry, settings, now=NOW, batch_size=3, scan_orphans=False)
    assert first.failed == 3

    later = NOW + timedelta(seconds=31)  # the failed rows are due again
    second = await prov.reconcile(session_factory, registry, settings, now=later, batch_size=3, scan_orphans=False)

    assert second.synced == 2
    assert {email for _, email in fake.accounts} == {f"tg4-{inbound.id}", f"tg5-{inbound.id}"}
