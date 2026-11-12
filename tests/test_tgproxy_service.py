"""Telegram proxy service: access policy, lazy accounts, reconcile, rotation, scan (usage/drift/orphans/abuse)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from app.config import Settings
from app.db.models import (
    Admin,
    AdminRole,
    Inbound,
    Location,
    Panel,
    PanelKind,
    Plan,
    ProvisionState,
    Subscription,
    SubscriptionStatus,
    TgProxyAccess,
    User,
)
from app.db.session import SessionFactory
from app.services import tg_proxy
from app.services.panels.base import PanelError
from app.services.panels.mtproto import ProxyAccount, ProxyLink, ProxyUserSpec, fake_tls_secret, new_key
from app.services.tg_proxy import (
    AbuseDetector,
    AbuseEvent,
    AccessReason,
    Policy,
    RotationTooSoonError,
    UserFacts,
    evaluate,
)
from app.utils.crypto import SecretBox
from tests.conftest import make_settings

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
DOMAIN = "petrovich.ru"
TAG = "c" * 32
BOX = SecretBox("test-secret-key-0123456789abcdef")


# ---------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------
@dataclass
class FakeProxyPanel:
    """In-memory telemt / 3x-ui mtproto backend (implements MtprotoProxyBackend)."""

    kind: PanelKind = PanelKind.TELEMT
    host: str = "proxy.example.com"
    accounts: dict[tuple[int | None, str], ProxyAccount] = field(default_factory=dict)
    specs: list[ProxyUserSpec] = field(default_factory=list)
    calls: list[tuple[str, int | None, str]] = field(default_factory=list)
    fail_next: int = 0
    on_upsert: Any = None

    def _maybe_fail(self) -> None:
        if self.fail_next:
            self.fail_next -= 1
            raise PanelError("boom")

    def _links(self, key: str) -> list[ProxyLink]:
        return [ProxyLink(self.host, 443, fake_tls_secret(key, DOMAIN))] if self.kind == PanelKind.TELEMT else []

    async def upsert_proxy_user(self, target: int | None, spec: ProxyUserSpec) -> ProxyAccount:
        self.calls.append(("upsert", target, spec.name))
        self._maybe_fail()
        if self.on_upsert is not None:
            await self.on_upsert(spec)
        self.specs.append(spec)
        current = self.accounts.get((target, spec.name))
        key = spec.key or (current.key if current else None) or new_key()
        account = ProxyAccount(
            name=spec.name,
            enabled=spec.enabled,
            key=key,
            links=self._links(key),
            tls_secret=fake_tls_secret(key, spec.tls_domain or DOMAIN) if self.kind == PanelKind.XUI else None,
            expires_at=spec.expires_at,
            max_unique_ips=spec.max_unique_ips,
            rate_limit_bps=spec.rate_limit_bps,
            ad_tag=spec.ad_tag,
            used_bytes=current.used_bytes if current else 0,
            recent_unique_ips=current.recent_unique_ips if current else 0,
        )
        self.accounts[(target, spec.name)] = account
        return replace(account)

    async def get_proxy_user(self, target: int | None, name: str) -> ProxyAccount | None:
        return self.accounts.get((target, name))

    async def list_proxy_users(self, target: int | None) -> list[ProxyAccount]:
        self.calls.append(("list", target, ""))
        self._maybe_fail()
        return [replace(a) for (t, _), a in self.accounts.items() if t == target]

    async def delete_proxy_user(self, target: int | None, name: str) -> None:
        self.calls.append(("delete", target, name))
        self._maybe_fail()
        self.accounts.pop((target, name), None)

    def account(self, name: str, target: int | None = None) -> ProxyAccount:
        return self.accounts[(target, name)]


class StubRegistry:
    def __init__(self, panels: dict[int, Any]) -> None:
        self.panels = panels

    async def get(self, panel_id: int) -> Any:
        if panel_id not in self.panels:
            raise LookupError(panel_id)
        return self.panels[panel_id]


@dataclass
class World:
    session_factory: SessionFactory
    settings: Settings
    telemt_id: int
    backup_id: int
    xui_id: int
    mt_inbound_id: int
    telemt: FakeProxyPanel
    backup: FakeProxyPanel
    xui: FakeProxyPanel

    @property
    def registry(self) -> StubRegistry:
        return StubRegistry({self.telemt_id: self.telemt, self.backup_id: self.backup, self.xui_id: self.xui})

    async def open(self, user_id: int, **kw: Any) -> tg_proxy.UserProxyView:
        kw.setdefault("now", NOW)
        return await tg_proxy.open_proxy(self.session_factory, self.registry, self.settings, BOX, user_id, **kw)

    async def reconcile(self, now: datetime = NOW) -> tg_proxy.SyncReport:
        return await tg_proxy.reconcile(self.session_factory, self.registry, self.settings, BOX, now=now)

    async def scan(self, **kw: Any) -> tg_proxy.ScanReport:
        kw.setdefault("now", NOW)
        return await tg_proxy.scan(self.session_factory, self.registry, self.settings, BOX, **kw)

    async def rows(self, user_id: int | None = None) -> list[TgProxyAccess]:
        async with self.session_factory() as session:
            stmt = select(TgProxyAccess).order_by(TgProxyAccess.id)
            if user_id is not None:
                stmt = stmt.where(TgProxyAccess.user_id == user_id)
            return list(await session.scalars(stmt))

    async def execute(self, stmt: Any) -> None:
        async with self.session_factory.begin() as session:
            await session.execute(stmt)


async def add_user(world: World, tg_id: int, **fields: Any) -> None:
    async with world.session_factory.begin() as session:
        session.add(User(tg_id=tg_id, referral_code=f"r{tg_id}", sub_token=f"t{tg_id}", created_at=NOW, **fields))


async def add_sub(world: World, tg_id: int, *, days: int = 30, plan_proxy: bool | None = None, **fields: Any) -> None:
    async with world.session_factory.begin() as session:
        plan_id = None
        if plan_proxy is not None:
            plan = Plan(title="1m", duration_days=30, price_kop=10000, includes_tg_proxy=plan_proxy)
            session.add(plan)
            await session.flush()
            plan_id = plan.id
        session.add(
            Subscription(
                user_id=tg_id,
                plan_id=plan_id,
                client_uuid=str(uuid.uuid4()),
                status=fields.pop("status", SubscriptionStatus.ACTIVE),
                expires_at=NOW + timedelta(days=days),
                **fields,
            )
        )


@pytest.fixture
async def world(session_factory: SessionFactory) -> World:
    async with session_factory.begin() as session:
        de = Location(name="Германия", flag="🇩🇪", sort_order=0)
        fi = Location(name="Финляндия", flag="🇫🇮", sort_order=1)
        session.add_all([de, fi])
        await session.flush()
        telemt = Panel(
            name="tg-de",
            kind=PanelKind.TELEMT,
            url="http://telemt:9091",
            options=json.dumps({"location_id": de.id, "web_host": "web.example.com"}),
        )
        backup = Panel(
            name="tg-fi", kind=PanelKind.TELEMT, url="http://t2:9091", options=json.dumps({"location_id": fi.id})
        )
        xui = Panel(
            name="xui-nl", kind=PanelKind.XUI, url="https://nl.example.com:2053/p", options='{"tg_proxy_port": 993}'
        )
        session.add_all([telemt, backup, xui])
        await session.flush()
        params = {
            "protocol": "mtproto",
            "port": 8443,
            "fake_tls_domain": "wb.ru",
            "local": True,
            "panel_enabled": True,
            "tg_proxy": True,
        }
        mt = Inbound(panel_id=xui.id, remote_id=9, name="tg", params=json.dumps(params), is_active=False)
        vless = Inbound(panel_id=xui.id, remote_id=1, name="vpn", params='{"protocol": "vless"}', is_active=True)
        session.add_all([mt, vless])
        await session.flush()
        ids = telemt.id, fi.id, backup.id, xui.id, mt.id
    w = World(
        session_factory=session_factory,
        settings=make_settings(admin_ids=[1000], tg_proxy_free_rate_limit_mbps=2, tg_proxy_free_ad_tag=TAG),
        telemt_id=ids[0],
        backup_id=ids[2],
        xui_id=ids[3],
        mt_inbound_id=ids[4],
        telemt=FakeProxyPanel(),
        backup=FakeProxyPanel(host="fi.example.com"),
        xui=FakeProxyPanel(kind=PanelKind.XUI),
    )
    for tg_id in (1, 2, 3, 1000):
        await add_user(w, tg_id)
    return w


def facts(**overrides: Any) -> UserFacts:
    values: dict[str, Any] = {"user_id": 1, "is_banned": False, "is_staff": False}
    values.update(overrides)
    return UserFacts(**values)


# ---------------------------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["everyone", "subscribers", "off"])
def test_staff_always_allowed_unless_banned(mode: str) -> None:
    settings = make_settings(tg_proxy_access=mode)
    assert evaluate(settings, facts(is_staff=True), NOW) == Policy(ProvisionState.ENABLED, AccessReason.STAFF)
    banned = evaluate(settings, facts(is_staff=True, is_banned=True), NOW)
    assert banned.state == ProvisionState.DISABLED and banned.reason == AccessReason.BANNED


def test_everyone_mode() -> None:
    settings = make_settings(tg_proxy_access="everyone")
    free = evaluate(settings, facts(), NOW)
    assert free == Policy(ProvisionState.ENABLED, AccessReason.FREE, limited=True)
    active = facts(sub_status=SubscriptionStatus.ACTIVE, sub_expires_at=NOW + timedelta(days=1))
    assert evaluate(settings, active, NOW) == Policy(ProvisionState.ENABLED, AccessReason.SUBSCRIBER)
    excluded = replace(active, plan_includes_proxy=False)
    assert evaluate(settings, excluded, NOW).limited


def test_personal_mode_gives_the_free_proxy_to_members_only() -> None:
    settings = make_settings(app_mode="personal", tg_proxy_access="everyone", expired_client_retention_days=3)
    assert evaluate(settings, facts(), NOW) == Policy(ProvisionState.ABSENT, AccessReason.NO_SUBSCRIPTION)
    removed = facts(sub_status=SubscriptionStatus.DISABLED, sub_expires_at=NOW - timedelta(days=4, seconds=1))
    assert evaluate(settings, removed, NOW) == Policy(ProvisionState.ABSENT, AccessReason.NO_SUBSCRIPTION)
    paused = facts(sub_status=SubscriptionStatus.DISABLED, sub_expires_at=NOW + timedelta(days=5))
    assert evaluate(settings, paused, NOW) == Policy(ProvisionState.ENABLED, AccessReason.FREE, limited=True)
    expired = facts(sub_status=SubscriptionStatus.EXPIRED, sub_expires_at=NOW - timedelta(days=40))
    assert evaluate(settings, expired, NOW).reason == AccessReason.FREE
    active = facts(sub_status=SubscriptionStatus.ACTIVE, sub_expires_at=NOW + timedelta(days=1))
    assert evaluate(settings, active, NOW).reason == AccessReason.SUBSCRIBER
    assert evaluate(settings, facts(is_staff=True), NOW).reason == AccessReason.STAFF
    shop = make_settings(tg_proxy_access="everyone")
    assert evaluate(shop, facts(), NOW).reason == AccessReason.FREE


def test_subscribers_mode() -> None:
    settings = make_settings(tg_proxy_access="subscribers", expired_client_retention_days=30)
    until = NOW + timedelta(days=1)
    active = facts(sub_status=SubscriptionStatus.ACTIVE, sub_expires_at=until)
    assert evaluate(settings, active, NOW) == Policy(ProvisionState.ENABLED, AccessReason.SUBSCRIBER, expires_at=until)
    assert evaluate(settings, facts(), NOW).state == ProvisionState.DISABLED
    assert evaluate(settings, replace(active, plan_includes_proxy=False), NOW).state == ProvisionState.DISABLED
    blocked = replace(active, sub_status=SubscriptionStatus.DISABLED)
    assert evaluate(settings, blocked, NOW).reason == AccessReason.NO_SUBSCRIPTION
    expired = facts(sub_status=SubscriptionStatus.EXPIRED, sub_expires_at=NOW - timedelta(days=10))
    assert evaluate(settings, expired, NOW).state == ProvisionState.DISABLED
    gone = replace(expired, sub_expires_at=NOW - timedelta(days=31))
    assert evaluate(settings, gone, NOW).state == ProvisionState.ABSENT


def test_off_mode_and_ad_tag_validation() -> None:
    assert evaluate(make_settings(tg_proxy_access="off"), facts(), NOW).reason == AccessReason.OFF
    assert tg_proxy.free_ad_tag(make_settings(tg_proxy_free_ad_tag="nope")) is None
    assert tg_proxy.free_ad_tag(make_settings(tg_proxy_free_ad_tag=TAG.upper())) == TAG
    assert tg_proxy.free_ad_tag(make_settings()) is None


def test_backoff_grows_and_caps() -> None:
    assert tg_proxy.backoff_delay(1) == timedelta(seconds=30)
    assert tg_proxy.backoff_delay(3) == timedelta(minutes=2)
    assert tg_proxy.backoff_delay(50) == timedelta(minutes=30)


# ---------------------------------------------------------------------------------------------
# Lazy accounts + first push
# ---------------------------------------------------------------------------------------------
async def test_open_proxy_creates_accounts_on_best_targets_and_returns_links(world: World) -> None:
    view = await world.open(1)
    assert view.policy.reason == AccessReason.FREE
    titles = [e.title for e in view.entries]
    assert titles == ["🇩🇪 Германия", "🇫🇮 Финляндия"]  # location order; one per panel; max 2
    assert all(e.ready for e in view.entries) and len(view.ready) == 2
    de = view.entries[0]
    (link,) = de.links
    key = world.telemt.account("tg1").key
    assert link.tme_url == f"https://t.me/proxy?server=proxy.example.com&port=443&secret={fake_tls_secret(key, DOMAIN)}"  # type: ignore[arg-type]
    assert de.web_url == f"tg://webproxy?server=web.example.com&secret=dd{key}"
    assert view.entries[1].web_url is None
    spec = world.telemt.specs[-1]
    assert (spec.rate_limit_bps, spec.ad_tag, spec.max_unique_ips, spec.expires_at) == (2_000_000, TAG, 3, None)
    assert spec.key == key and spec.comment == "tg:1 proxy"
    rows = await world.rows(1)
    assert [r.actual_state for r in rows] == [ProvisionState.ENABLED] * 2
    assert all(r.synced_revision == r.revision for r in rows)
    assert "links" not in (rows[0].links_cache or "") and (rows[0].links_cache or "").startswith("enc:")


async def test_ensure_access_is_idempotent_and_balances_load(world: World) -> None:
    await tg_proxy.ensure_access(world.session_factory, world.settings, BOX, 1, max_targets=1, now=NOW)
    again = await tg_proxy.ensure_access(world.session_factory, world.settings, BOX, 1, max_targets=1, now=NOW)
    assert again.created == []
    # The DE target has one account now; with equal location order the least loaded wins.
    await world.execute(update(Location).values(sort_order=0))
    second = await tg_proxy.ensure_access(world.session_factory, world.settings, BOX, 2, max_targets=1, now=NOW)
    (row,) = await world.rows(2)
    assert second.created == [row.id] and row.panel_id != world.telemt_id


async def test_ensure_access_skips_unhealthy_and_unselected_targets(world: World) -> None:
    await world.execute(update(Panel).where(Panel.id == world.telemt_id).values(consecutive_failures=99))
    await world.execute(update(Panel).where(Panel.id == world.backup_id).values(is_active=False))
    result = await tg_proxy.ensure_access(world.session_factory, world.settings, BOX, 1, now=NOW)
    (row,) = await world.rows(1)
    assert result.created == [row.id] and row.panel_id == world.xui_id and row.inbound_id == world.mt_inbound_id
    assert row.remote_name == f"tg1-{world.mt_inbound_id}-tg"


async def test_xui_target_links_are_built_locally(world: World) -> None:
    await world.execute(update(Panel).where(Panel.kind == PanelKind.TELEMT).values(is_active=False))
    view = await world.open(1)
    (entry,) = view.entries
    key = world.xui.account(f"tg1-{world.mt_inbound_id}-tg", 9).key
    assert entry.links == (ProxyLink("nl.example.com", 993, fake_tls_secret(key, "wb.ru")),)  # type: ignore[arg-type]
    assert world.xui.specs[-1].tls_domain == "wb.ru"


async def test_no_targets_and_not_allowed(world: World) -> None:
    await world.execute(update(Panel).values(is_active=False))
    result = await tg_proxy.ensure_access(world.session_factory, world.settings, BOX, 1, now=NOW)
    assert result.no_targets and result.created == []
    world.settings = make_settings(tg_proxy_access="off")
    off = await tg_proxy.ensure_access(world.session_factory, world.settings, BOX, 1, now=NOW)
    assert not off.policy.allowed and not off.no_targets
    with pytest.raises(LookupError):
        await tg_proxy.ensure_access(world.session_factory, world.settings, BOX, 404, now=NOW)


async def test_staff_and_subscribers_get_no_limits(world: World) -> None:
    await world.open(1000)
    staff_spec = world.telemt.specs[-1]
    assert (staff_spec.rate_limit_bps, staff_spec.ad_tag, staff_spec.max_unique_ips) == (0, None, 0)
    await add_sub(world, 2, plan_proxy=True)
    await world.open(2)
    sub_spec = world.telemt.specs[-1]
    assert (sub_spec.rate_limit_bps, sub_spec.ad_tag, sub_spec.max_unique_ips) == (0, None, 3)
    async with world.session_factory.begin() as session:
        session.add(Admin(tg_id=3, role=AdminRole.ADMIN))
    await world.open(3)
    assert world.telemt.specs[-1].max_unique_ips == 0


# ---------------------------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------------------------
async def test_settled_accounts_cause_no_calls(world: World) -> None:
    await world.open(1)
    world.telemt.calls.clear()
    world.backup.calls.clear()
    report = await world.reconcile()
    assert report.synced == 0 and world.telemt.calls == [] and world.backup.calls == []


async def test_ban_and_unban_are_picked_up_without_any_trigger(world: World) -> None:
    await world.open(1)
    await world.execute(update(User).where(User.tg_id == 1).values(is_banned=True))
    report = await world.reconcile()
    assert report.synced == 2 and not world.telemt.account("tg1").enabled
    rows = await world.rows(1)
    assert {r.actual_state for r in rows} == {ProvisionState.DISABLED}
    view = await tg_proxy.user_view(world.session_factory, world.settings, BOX, 1, now=NOW)
    assert view.policy.reason == AccessReason.BANNED and view.ready == ()
    await world.execute(update(User).where(User.tg_id == 1).values(is_banned=False))
    await world.reconcile()
    assert world.telemt.account("tg1").enabled


async def test_subscription_and_settings_changes_reshape_accounts(world: World) -> None:
    await world.open(1)
    await add_sub(world, 1)  # plan-less (trial-like) subscription includes the proxy
    await world.reconcile()
    assert world.telemt.account("tg1").rate_limit_bps == 0 and world.telemt.account("tg1").ad_tag is None
    world.settings = make_settings(tg_proxy_max_unique_ips=5)
    await world.reconcile()
    assert world.telemt.account("tg1").max_unique_ips == 5


async def test_subscribers_mode_disables_then_deletes_after_retention(world: World) -> None:
    world.settings = make_settings(tg_proxy_access="subscribers", expired_client_retention_days=30)
    await add_sub(world, 1, days=10)
    await world.open(1)
    assert world.telemt.account("tg1").expires_at == NOW + timedelta(days=10)
    later = NOW + timedelta(days=20)
    await world.reconcile(later)
    assert not world.telemt.account("tg1").enabled
    await world.reconcile(NOW + timedelta(days=41))
    assert await world.rows(1) == [] and ("delete", None, "tg1") in world.telemt.calls


async def test_disabled_user_without_server_account_is_not_created(world: World) -> None:
    await tg_proxy.ensure_access(world.session_factory, world.settings, BOX, 1, now=NOW)
    await world.execute(update(User).where(User.tg_id == 1).values(is_banned=True))
    report = await world.reconcile()
    assert report.synced == 2 and world.telemt.calls == []
    rows = await world.rows(1)
    assert {(r.actual_state, r.desired_state) for r in rows} == {(ProvisionState.ABSENT, ProvisionState.DISABLED)}
    assert (await world.reconcile()).synced == 0


async def test_deactivated_target_is_drained(world: World) -> None:
    await world.open(1)
    await world.execute(update(Panel).where(Panel.id == world.backup_id).values(is_active=False))
    report = await world.reconcile()
    assert report.deleted == 1 and ("delete", None, "tg1") in world.backup.calls
    assert [r.panel_id for r in await world.rows(1)] == [world.telemt_id]
    view = await world.open(1)  # the next visit adds a replacement
    assert len(view.entries) == 2 and view.entries[1].panel_id == world.xui_id


async def test_deselected_xui_inbound_is_drained(world: World) -> None:
    await world.execute(update(Panel).where(Panel.kind == PanelKind.TELEMT).values(is_active=False))
    await world.open(1)
    async with world.session_factory.begin() as session:
        assert await tg_proxy.set_xui_inbound(session, world.mt_inbound_id, False)
        assert not await tg_proxy.set_xui_inbound(session, 999, True)
    await world.reconcile()
    assert await world.rows(1) == [] and world.xui.accounts == {}


async def test_failures_back_off_and_recover(world: World) -> None:
    await tg_proxy.ensure_access(world.session_factory, world.settings, BOX, 1, max_targets=1, now=NOW)
    world.telemt.fail_next = 1
    report = await world.reconcile()
    assert report.failed == 1 and report.errors
    (row,) = await world.rows(1)
    assert row.attempts == 1 and row.last_error == "boom" and row.next_attempt_at == NOW + timedelta(seconds=30)
    assert (await world.reconcile(NOW + timedelta(seconds=10))).synced == 0  # still backing off
    assert (await world.reconcile(NOW + timedelta(seconds=31))).synced == 1
    (row,) = await world.rows(1)
    assert row.attempts == 0 and row.last_error is None and row.actual_state == ProvisionState.ENABLED


async def test_panel_marked_down_is_skipped(world: World) -> None:
    await tg_proxy.ensure_access(world.session_factory, world.settings, BOX, 1, max_targets=1, now=NOW)
    await world.execute(update(Panel).where(Panel.id == world.telemt_id).values(consecutive_failures=99))
    report = await world.reconcile()
    assert report.skipped == 1 and world.telemt.calls == []


async def test_missing_backend_is_a_failure_not_a_crash(world: World) -> None:
    await tg_proxy.ensure_access(world.session_factory, world.settings, BOX, 1, max_targets=1, now=NOW)
    report = await tg_proxy.reconcile(world.session_factory, StubRegistry({}), world.settings, BOX, now=NOW)
    assert report.failed == 1


async def test_stale_snapshot_is_not_stored(world: World) -> None:
    await tg_proxy.ensure_access(world.session_factory, world.settings, BOX, 1, max_targets=1, now=NOW)

    async def rotate_meanwhile(spec: ProxyUserSpec) -> None:
        world.telemt.on_upsert = None
        await tg_proxy.rotate(world.session_factory, BOX, 1, now=NOW)

    world.telemt.on_upsert = rotate_meanwhile
    await world.reconcile()
    (row,) = await world.rows(1)
    assert row.synced_revision != row.revision  # the push of the old key was not recorded as synced
    await world.reconcile()
    (row,) = await world.rows(1)
    assert row.synced_revision == row.revision
    assert world.telemt.specs[-1].key != world.telemt.specs[0].key


async def test_undecryptable_cache_issues_a_new_secret(world: World) -> None:
    await world.open(1, max_targets=1)
    old = world.telemt.account("tg1").key
    await world.execute(update(TgProxyAccess).values(links_cache="enc:garbage"))
    await world.reconcile()
    assert world.telemt.account("tg1").key != old
    view = await tg_proxy.user_view(world.session_factory, world.settings, BOX, 1)
    assert view.entries[0].ready


async def test_mark_user_dirty_forces_a_push(world: World) -> None:
    await world.open(1, max_targets=1)
    world.telemt.calls.clear()
    async with world.session_factory.begin() as session:
        assert await tg_proxy.mark_user_dirty(session, 1) == 1
    report = await tg_proxy.sync_user(world.session_factory, world.registry, world.settings, BOX, 1, now=NOW)
    assert report.ok and report.synced == 1 and world.telemt.calls == [("upsert", None, "tg1")]


# ---------------------------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------------------------
async def test_rotate_hides_old_links_until_pushed(world: World) -> None:
    view = await world.open(1)
    old_links = {link for e in view.entries for link in e.links}
    assert await tg_proxy.rotate(world.session_factory, BOX, 1, now=NOW) == 2
    pending = await tg_proxy.user_view(world.session_factory, world.settings, BOX, 1)
    assert pending.ready == () and all(e.links == () for e in pending.entries)
    fresh = await world.open(1)
    new_links = {link for e in fresh.entries for link in e.links}
    assert len(new_links) == 2 and not new_links & old_links
    assert all(r.rotated_at == NOW for r in await world.rows(1))
    with pytest.raises(RotationTooSoonError) as info:
        await tg_proxy.rotate(world.session_factory, BOX, 1, min_interval=timedelta(minutes=10), now=NOW)
    assert info.value.retry_at == NOW + timedelta(minutes=10)
    later = NOW + timedelta(minutes=11)
    assert await tg_proxy.rotate(world.session_factory, BOX, 1, min_interval=timedelta(minutes=10), now=later) == 2


# ---------------------------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------------------------
async def test_scan_records_usage_and_detects_drift(world: World) -> None:
    await world.open(1)
    world.telemt.account("tg1").used_bytes = 4096
    report = await world.scan()
    assert report.accounts == 2 and report.drifted == 0
    rows = {r.panel_id: r for r in await world.rows(1)}
    assert rows[world.telemt_id].used_bytes == 4096
    del world.telemt.accounts[(None, "tg1")]  # deleted on the server by hand
    report = await world.scan()
    assert report.drifted == 1
    await world.reconcile()
    assert world.telemt.account("tg1").enabled


async def test_scan_deletes_orphans_only_when_enabled(world: World) -> None:
    await world.open(1)
    stray = ProxyAccount(name="tg77", enabled=True, key=new_key())
    foreign = ProxyAccount(name="alice", enabled=True, key=new_key())
    world.telemt.accounts[(None, "tg77")] = stray
    world.telemt.accounts[(None, "alice")] = foreign
    report = await world.scan()
    assert report.orphans == [f"{world.telemt_id}:tg77"] and report.orphans_deleted == 0
    world.settings = make_settings(delete_orphan_clients=True)
    report = await world.scan()
    assert report.orphans_deleted == 1 and (None, "tg77") not in world.telemt.accounts
    assert (None, "alice") in world.telemt.accounts  # not ours: never touched


async def test_abuse_rotates_after_strikes_and_notifies(world: World) -> None:
    await world.open(1, max_targets=1)
    await world.open(1000, max_targets=1)
    world.telemt.account("tg1").recent_unique_ips = 3
    world.telemt.account("tg1000").recent_unique_ips = 50  # staff: never flagged
    events: list[AbuseEvent] = []

    async def notify(event: AbuseEvent) -> None:
        events.append(event)
        raise RuntimeError("callback failures are swallowed")

    detector = AbuseDetector(strikes=2)
    first = await world.scan(detector=detector, on_abuse=notify)
    assert first.abuse == [] and events == []
    second = await world.scan(detector=detector, on_abuse=notify)
    (event,) = second.abuse
    assert events == [event] and event.user_id == 1 and event.rotated and event.unique_ips == 3
    (row,) = await world.rows(1)
    assert row.rotated_at == NOW and row.synced_revision != row.revision
    await world.reconcile()
    assert world.telemt.specs[-1].key != world.telemt.specs[0].key
    # Within the cooldown it is only reported.
    world.telemt.account("tg1").recent_unique_ips = 3
    await world.scan(detector=detector)
    again = await world.scan(detector=detector, now=NOW + timedelta(hours=1))
    assert [e.rotated for e in again.abuse] == [False]


async def test_abuse_without_auto_rotation(world: World) -> None:
    world.settings = make_settings(tg_proxy_auto_rotate_on_abuse=False, tg_proxy_max_unique_ips=0)
    await world.open(1, max_targets=1)
    world.telemt.account("tg1").recent_unique_ips = tg_proxy.ABUSE_DEFAULT_UNIQUE_IPS
    report = await world.scan(detector=AbuseDetector(strikes=1))
    assert [e.rotated for e in report.abuse] == [False]


def test_detector_resets_on_clean_scans() -> None:
    detector = AbuseDetector(strikes=2)
    assert not detector.observe(1, True)
    assert not detector.observe(1, False)
    assert not detector.observe(1, True)
    assert detector.observe(1, True)
    detector.observe(2, True)
    detector.forget([1])
    assert not detector.observe(2, True)


async def test_scan_errors_are_reported_per_panel(world: World) -> None:
    await world.open(1)
    world.telemt.fail_next = 1
    report = await world.scan()
    assert len(report.errors) == 1 and report.errors[0].startswith(f"panel {world.telemt_id}")


async def test_never_pushed_rows_on_drained_targets_are_removed(world: World) -> None:
    await tg_proxy.ensure_access(world.session_factory, world.settings, BOX, 1, now=NOW)
    await world.execute(update(Panel).values(is_active=False))
    report = await world.reconcile()
    assert report.deleted == 2 and await world.rows(1) == []


async def test_keyed_locks_stay_exclusive_while_a_waiter_is_queued() -> None:
    import asyncio

    locks = tg_proxy._KeyedLocks()
    inside = 0
    peak = 0

    async def push() -> None:
        nonlocal inside, peak
        async with locks.hold(42):
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            inside -= 1

    first = asyncio.create_task(push())
    await asyncio.sleep(0)  # the first push holds the lock
    second = asyncio.create_task(push())
    await asyncio.sleep(0)  # the second one waits for it
    await asyncio.sleep(0)
    await asyncio.sleep(0)  # the first one released: the waiter is woken but has not run yet
    third = asyncio.create_task(push())
    await asyncio.gather(first, second, third)
    assert peak == 1
    assert locks._locks == {}
