"""Regression tests: per-account 3x-ui subIds, Remnawave prefix changes, subscription-wide traffic quota."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    Inbound,
    Panel,
    PanelKind,
    Plan,
    ProvisionState,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.db.session import SessionFactory
from app.services import provisioning as prov
from app.services import subscriptions
from app.services.panels.base import AccountSpec, PanelError, RemoteAccount
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


# ---------------------------------------------------------------------------------------------
# 3x-ui subId must be unique per client
# ---------------------------------------------------------------------------------------------
@dataclass
class SubIdPanel(FakePanel):
    """Modern 3x-ui: a subId already used by a client with another email is rejected."""

    sub_ids: dict[str, str] = field(default_factory=dict)  # subId -> email

    async def upsert_account(self, target: int | None, spec: AccountSpec) -> RemoteAccount:
        owner = self.sub_ids.get(spec.sub_id)
        if owner is not None and owner != spec.email:
            self.calls.append(("upsert", target, spec.email))
            raise PanelError(f"subId already in use: {spec.sub_id}")
        account = await super().upsert_account(target, spec)
        self.sub_ids = {k: v for k, v in self.sub_ids.items() if v != spec.email}
        self.sub_ids[spec.sub_id] = spec.email
        return account


async def test_every_inbound_client_of_a_subscription_gets_its_own_sub_id(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 42)
        panel, inbounds = await add_xui_panel(s, inbounds=3)
        sub = await add_sub(s, 42, expires_at=NOW + days(10))
        await prov.ensure_provisions(s, sub)
        sub_id = sub.id
    fake = SubIdPanel()
    registry = FakeRegistry({panel.id: fake})

    result = await prov.sync_subscription(session_factory, registry, settings, sub_id, now=NOW)

    assert result.ok, result.errors
    assert {email for _, email in fake.accounts} == {f"tg42-{inbound.id}" for inbound in inbounds}
    rows = await provisions_of(session_factory, sub_id)
    assert all(row.last_error is None and row.actual_state == ProvisionState.ENABLED for row in rows)


def test_derive_sub_id_differs_per_account_and_is_stable() -> None:
    uuid = "0b7e3f0e-8c1d-4b8e-9d55-3c2f1f0a9e11"
    first, second = prov.derive_sub_id(uuid, "tg42-5"), prov.derive_sub_id(uuid, "tg42-6")
    assert first != second
    assert first == prov.derive_sub_id(uuid, "tg42-5")
    assert len(first) == 16 and all(c in "0123456789abcdef" for c in first)


# ---------------------------------------------------------------------------------------------
# Remnawave username prefix change
# ---------------------------------------------------------------------------------------------
@dataclass
class PrefixPanel(FakePanel):
    """Remnawave-like fake whose listing only returns users with the backend's current prefix."""

    prefix: str = "tg_"

    async def list_accounts(self, target: int | None) -> list[RemoteAccount]:
        return [acc for acc in await super().list_accounts(target) if acc.email.startswith(self.prefix)]


async def test_remnawave_prefix_change_does_not_repush_old_accounts_forever(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 42)
        panel = await add_rw_panel(s, options={"username_prefix": "tg_"})
        await add_sub(s, 42, expires_at=NOW + days(10))
        panel_id = panel.id

    fake = PrefixPanel(kind=PanelKind.REMNAWAVE)
    registry = FakeRegistry({panel_id: fake})
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    settled = await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert (settled.synced, settled.drifted) == (0, 0)

    async with session_factory.begin() as s:
        await _set_prefix(s, panel_id, "vpn_")
    fake.prefix = "vpn_"  # the registry rebuilds the backend with the new prefix
    reports = [await prov.reconcile(session_factory, registry, settings, now=NOW) for _ in range(3)]

    # The option change itself re-pushes once (config fingerprint); after that the old account is left alone.
    assert [r.drifted for r in reports] == [0, 0, 0]
    assert [r.synced for r in reports[1:]] == [0, 0]
    assert set(fake.accounts) == {(None, "tg_42")}
    (row,) = await provisions_of(session_factory)
    assert row.remote_email == "tg_42"


async def test_old_prefix_account_is_not_an_orphan_and_new_accounts_use_the_new_prefix(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 42)
        await add_user(s, 43)
        panel = await add_rw_panel(s, options={"username_prefix": "tg_"})
        await add_sub(s, 42, expires_at=NOW + days(10))
        panel_id = panel.id

    fake = PrefixPanel(kind=PanelKind.REMNAWAVE)
    registry = FakeRegistry({panel_id: fake})
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    async with session_factory.begin() as s:
        await _set_prefix(s, panel_id, "vpn_")
        await add_sub(s, 43, expires_at=NOW + days(10))
    fake.prefix = "vpn_"
    for _ in range(3):
        report = await prov.reconcile(session_factory, registry, make_settings(delete_orphan_clients=True), now=NOW)
        assert report.orphans == [] and report.drifted == 0
    assert set(fake.accounts) == {(None, "tg_42"), (None, "vpn_43")}


# ---------------------------------------------------------------------------------------------
# Traffic quota is per subscription
# ---------------------------------------------------------------------------------------------
async def _quota_world(
    session_factory: SessionFactory, *, inbounds: int = 2, traffic_gb: int = 50
) -> tuple[FakeRegistry, FakePanel, list[tuple[int | None, str]], int, int]:
    async with session_factory.begin() as s:
        await add_user(s, 42, balance_kop=100_000)
        panel, created = await add_xui_panel(s, inbounds=inbounds)
        plan = await add_plan(s, days=30, price_kop=10_000, traffic_gb=traffic_gb)
        user = await s.get(User, 42)
        assert user is not None
        await subscriptions.purchase_plan(s, make_settings(), user, plan, now=NOW)
        sub = await subscriptions.get_for_user(s, 42)
        assert sub is not None
        sub_id, plan_id = sub.id, plan.id
    fake = FakePanel()
    keys: list[tuple[int | None, str]] = [(i.remote_id, f"tg42-{i.id}") for i in created]
    return FakeRegistry({panel.id: fake}), fake, keys, sub_id, plan_id


async def _sub(session_factory: SessionFactory, sub_id: int) -> Subscription:
    async with session_factory() as s:
        sub = await s.get(Subscription, sub_id)
        assert sub is not None
        return sub


def test_desired_state_of_subscription_out_of_traffic_is_disabled() -> None:
    sub = Subscription(
        status=SubscriptionStatus.ACTIVE,
        expires_at=NOW + days(5),
        traffic_limit_gb=50,
        traffic_epoch=2,
        traffic_exhausted_epoch=2,
    )
    assert prov.is_traffic_exhausted(sub)
    assert prov.desired_state(sub, NOW) == ProvisionState.DISABLED
    sub.traffic_epoch = 3  # a new period starts with a fresh quota
    assert not prov.is_traffic_exhausted(sub)
    assert prov.desired_state(sub, NOW) == ProvisionState.ENABLED
    sub.traffic_epoch, sub.traffic_limit_gb = 2, 0  # unlimited
    assert prov.desired_state(sub, NOW) == ProvisionState.ENABLED


async def test_summed_usage_over_the_plan_quota_disables_every_account(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, fake, keys, sub_id, _ = await _quota_world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    for key in keys:
        fake.accounts[key].used_bytes = 30 * GIB  # 60 GiB in total, each account under its own 50 GiB

    await prov.reconcile(session_factory, registry, settings, now=NOW)  # the scan sees the quota used up
    assert prov.is_traffic_exhausted(await _sub(session_factory, sub_id))
    await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert all(fake.accounts[key].enabled is False for key in keys)
    fake.calls.clear()
    for _ in range(2):
        report = await prov.reconcile(session_factory, registry, settings, now=NOW)
        assert (report.synced, report.drifted, report.failed) == (0, 0, 0)
    assert fake.calls == []


async def test_usage_under_the_quota_keeps_accounts_enabled(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, fake, keys, sub_id, _ = await _quota_world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    for key in keys:
        fake.accounts[key].used_bytes = 20 * GIB
    fake.calls.clear()

    for _ in range(2):
        await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert not prov.is_traffic_exhausted(await _sub(session_factory, sub_id))
    assert fake.calls == []
    assert all(fake.accounts[key].enabled for key in keys)


async def test_new_period_lifts_the_quota_block(session_factory: SessionFactory, settings: Settings) -> None:
    registry, fake, keys, sub_id, plan_id = await _quota_world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    for key in keys:
        fake.accounts[key].used_bytes = 30 * GIB
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert all(fake.accounts[key].enabled is False for key in keys)

    async with session_factory.begin() as s:
        user, plan = await s.get(User, 42), await s.get(Plan, plan_id)
        assert user is not None and plan is not None
        await subscriptions.purchase_plan(s, make_settings(), user, plan, now=NOW)
    for _ in range(3):
        await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert all(fake.accounts[key].enabled and fake.accounts[key].used_bytes == 0 for key in keys)
    assert not prov.is_traffic_exhausted(await _sub(session_factory, sub_id))


async def test_raised_quota_in_the_same_period_reenables_accounts(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, fake, keys, sub_id, _ = await _quota_world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    for key in keys:
        fake.accounts[key].used_bytes = 30 * GIB
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    await prov.reconcile(session_factory, registry, settings, now=NOW)

    async with session_factory.begin() as s:  # an admin raises the quota without starting a new period
        await s.execute(
            update(Subscription)
            .where(Subscription.id == sub_id)
            .values(traffic_limit_gb=100, revision=Subscription.revision + 1)
        )
    for _ in range(3):
        await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert not prov.is_traffic_exhausted(await _sub(session_factory, sub_id))
    assert all(fake.accounts[key].enabled for key in keys)
    assert all(fake.accounts[key].traffic_limit_bytes == 100 * GIB for key in keys)


async def test_quota_block_is_not_lifted_while_an_account_is_unseen(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, fake, keys, sub_id, _ = await _quota_world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    for key in keys:
        fake.accounts[key].used_bytes = 30 * GIB
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    await prov.reconcile(session_factory, registry, settings, now=NOW)

    # One account vanished from the panel: the remaining usage (30 GiB) is only a lower bound.
    fake.accounts.pop(keys[1])
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert prov.is_traffic_exhausted(await _sub(session_factory, sub_id))


async def test_usage_of_the_previous_period_is_not_counted(session_factory: SessionFactory, settings: Settings) -> None:
    registry, fake, keys, sub_id, plan_id = await _quota_world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    for key in keys:
        fake.accounts[key].used_bytes = 30 * GIB
    async with session_factory.begin() as s:
        user, plan = await s.get(User, 42), await s.get(Plan, plan_id)
        assert user is not None and plan is not None
        await subscriptions.purchase_plan(s, make_settings(), user, plan, now=NOW)

    # Only the scan runs: the counters still hold the old period's usage (the reset is not pushed yet).
    await prov.find_orphans(session_factory, registry, settings, now=NOW)

    assert not prov.is_traffic_exhausted(await _sub(session_factory, sub_id))


async def test_new_provision_of_an_exhausted_subscription_starts_disabled(
    session_factory: SessionFactory, settings: Settings
) -> None:
    registry, fake, keys, sub_id, _ = await _quota_world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    for key in keys:
        fake.accounts[key].used_bytes = 30 * GIB
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    async with session_factory.begin() as s:
        panel = await s.scalar(select(Panel))
        assert panel is not None

        s.add(Inbound(panel_id=panel.id, remote_id=9, name="in9", params="{}", flow=""))
    await prov.reconcile(session_factory, registry, settings, now=NOW)

    rows = await provisions_of(session_factory, sub_id)
    assert len(rows) == 3
    assert {row.desired_state for row in rows} == {ProvisionState.DISABLED}
    assert all(not acc.enabled for acc in fake.accounts.values())


def test_device_limit_is_per_account_and_documented() -> None:
    assert "per account" in (prov.build_spec.__doc__ or "")


async def test_remnawave_account_counts_towards_the_same_quota(
    session_factory: SessionFactory, settings: Settings
) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 42)
        xui, (inbound,) = await add_xui_panel(s)
        rw = await add_rw_panel(s)
        await add_sub(s, 42, expires_at=NOW + days(10), traffic_gb=10)
        xui_id, rw_id = xui.id, rw.id

    xui_fake, rw_fake = FakePanel(), FakePanel(kind=PanelKind.REMNAWAVE)
    registry = FakeRegistry({xui_id: xui_fake, rw_id: rw_fake})
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    xui_key = (inbound.remote_id, f"tg42-{inbound.id}")
    xui_fake.accounts[xui_key] = replace(xui_fake.accounts[xui_key], used_bytes=6 * GIB)
    rw_fake.accounts[(None, "tg_42")].used_bytes = 5 * GIB

    await prov.reconcile(session_factory, registry, settings, now=NOW)
    await prov.reconcile(session_factory, registry, settings, now=NOW)

    assert xui_fake.accounts[xui_key].enabled is False
    assert rw_fake.accounts[(None, "tg_42")].enabled is False


async def _set_prefix(session: AsyncSession, panel_id: int, prefix: str) -> None:
    options = json.dumps({"username_prefix": prefix})
    await session.execute(update(Panel).where(Panel.id == panel_id).values(options=options))
