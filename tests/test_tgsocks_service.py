"""Personal SOCKS5 accounts: lifecycle, access policy, rotation, drift and orphan repair."""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from app.config import Settings
from app.db.models import (
    Inbound,
    Location,
    Panel,
    PanelKind,
    ProvisionState,
    Subscription,
    SubscriptionStatus,
    TgSocksAccess,
    User,
)
from app.db.session import SessionFactory
from app.services import bans, tg_socks
from app.services.panels.base import PanelError
from app.services.panels.socks import SocksAccount, SocksProxyBackend
from app.utils.crypto import SecretBox
from app.utils.time import utcnow
from tests.conftest import make_settings

BOX = SecretBox("test-secret-key-0123456789abcdef")
NOW = utcnow()


@dataclass
class FakeSocksPanel:
    """In-memory 3x-ui ``mixed`` inbounds (remote id -> {user: password}); implements SocksProxyBackend."""

    accounts: dict[int, dict[str, str]] = field(default_factory=dict)
    calls: list[tuple[str, int | None, dict[str, str], list[str]]] = field(default_factory=list)
    fail_next: int = 0

    async def list_socks_users(self, target: int | None) -> list[SocksAccount]:
        self.calls.append(("list", target, {}, []))
        return [SocksAccount(u, p) for u, p in self.accounts.get(target or 0, {}).items()]

    async def apply_socks_users(
        self, target: int | None, put: Mapping[str, str], remove: Collection[str] = ()
    ) -> list[SocksAccount]:
        self.calls.append(("apply", target, dict(put), list(remove)))
        if self.fail_next:
            self.fail_next -= 1
            raise PanelError("boom")
        accounts = self.accounts.setdefault(target or 0, {})
        for name in remove:
            accounts.pop(name, None)
        accounts.update(put)
        return [SocksAccount(u, p) for u, p in accounts.items()]

    async def delete_socks_user(self, target: int | None, name: str) -> None:
        await self.apply_socks_users(target, {}, [name])

    def applies(self) -> list[tuple[str, int | None, dict[str, str], list[str]]]:
        return [c for c in self.calls if c[0] == "apply"]


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
    panel_ids: list[int]
    inbound_ids: list[int]
    fakes: list[FakeSocksPanel]

    @property
    def registry(self) -> StubRegistry:
        return StubRegistry(dict(zip(self.panel_ids, self.fakes, strict=True)))

    async def open(self, user_id: int, **kw: Any) -> tg_socks.SocksView:
        kw.setdefault("now", NOW)
        return await tg_socks.open_socks(self.session_factory, self.registry, self.settings, BOX, user_id, **kw)

    async def reconcile(self, now: Any = NOW) -> tg_socks.SyncReport:
        return await tg_socks.reconcile(self.session_factory, self.registry, self.settings, BOX, now=now)

    async def scan(self) -> tg_socks.ScanReport:
        return await tg_socks.scan(self.session_factory, self.registry, self.settings, BOX)

    async def rows(self, user_id: int | None = None) -> list[TgSocksAccess]:
        async with self.session_factory() as session:
            stmt = select(TgSocksAccess).order_by(TgSocksAccess.id)
            if user_id is not None:
                stmt = stmt.where(TgSocksAccess.user_id == user_id)
            return list(await session.scalars(stmt))

    async def execute(self, stmt: Any) -> None:
        async with self.session_factory.begin() as session:
            await session.execute(stmt)

    def password(self, row: TgSocksAccess) -> str:
        return BOX.decrypt(row.password_enc)


async def add_user(world: World, tg_id: int, **fields: Any) -> None:
    async with world.session_factory.begin() as session:
        session.add(User(tg_id=tg_id, referral_code=f"r{tg_id}", sub_token=f"t{tg_id}", created_at=NOW, **fields))


async def add_sub(world: World, tg_id: int, *, days: int = 30, **fields: Any) -> None:
    async with world.session_factory.begin() as session:
        session.add(
            Subscription(
                user_id=tg_id,
                client_uuid=str(uuid.uuid4()),
                status=fields.pop("status", SubscriptionStatus.ACTIVE),
                expires_at=NOW + timedelta(days=days),
                **fields,
            )
        )


def mixed_params(**overrides: Any) -> str:
    params = {
        "protocol": "mixed",
        "port": 1080,
        "local": True,
        "panel_enabled": True,
        "tg_socks": True,
        "socks_auth": "password",
    }
    params.update(overrides)
    return json.dumps(params)


@pytest.fixture
async def world(session_factory: SessionFactory) -> World:
    async with session_factory.begin() as session:
        de = Location(name="Германия", flag="🇩🇪", sort_order=0)
        fi = Location(name="Финляндия", flag="🇫🇮", sort_order=1)
        session.add_all([de, fi])
        await session.flush()
        panels = [
            Panel(
                name="xui-de",
                kind=PanelKind.XUI,
                url="https://de.example.com:2053/p",
                options=json.dumps({"tg_socks_host": "socks.de.example.com", "tg_socks_port": 4443}),
            ),
            Panel(name="xui-fi", kind=PanelKind.XUI, url="https://fi.example.com:2053/p"),
        ]
        session.add_all(panels)
        await session.flush()
        inbounds = [
            Inbound(panel_id=panels[0].id, remote_id=5, name="s", params=mixed_params(), location_id=de.id),
            Inbound(panel_id=panels[1].id, remote_id=6, name="s", params=mixed_params(port=1081), location_id=fi.id),
        ]
        vless = Inbound(panel_id=panels[0].id, remote_id=1, name="vpn", params='{"protocol": "vless"}', is_active=True)
        session.add_all([*inbounds, vless])
        await session.flush()
        panel_ids, inbound_ids = [p.id for p in panels], [i.id for i in inbounds]
    w = World(
        session_factory=session_factory,
        settings=make_settings(admin_ids=[1000]),
        panel_ids=panel_ids,
        inbound_ids=inbound_ids,
        fakes=[FakeSocksPanel(), FakeSocksPanel()],
    )
    for tg_id in (1, 2, 3, 1000):
        await add_user(w, tg_id)
    return w


# ---------------------------------------------------------------------------------------------
# Creation, links, secrets
# ---------------------------------------------------------------------------------------------
async def test_open_creates_accounts_pushes_them_and_builds_links(world: World) -> None:
    view = await world.open(1, max_targets=2)
    assert view.policy.allowed and len(view.ready) == 2
    first, second = view.entries
    assert first.title == "🇩🇪 Германия" and second.title == "🇫🇮 Финляндия"
    assert first.link is not None and second.link is not None
    # The first panel has a public address override, the second uses the panel URL host and the inbound port.
    assert (first.link.server, first.link.port, first.link.user) == ("socks.de.example.com", 4443, "tg1")
    assert (second.link.server, second.link.port) == ("fi.example.com", 1081)
    assert first.link.password != second.link.password and len(first.link.password) >= 16
    assert first.link.tme_url.startswith("https://t.me/socks?server=socks.de.example.com&port=4443&user=tg1&pass=")
    assert world.fakes[0].accounts == {5: {"tg1": first.link.password}}
    assert world.fakes[1].accounts == {6: {"tg1": second.link.password}}
    # The VLESS inbound of the panel is not a SOCKS5 target.
    rows = await world.rows(1)
    assert {r.inbound_id for r in rows} == set(world.inbound_ids)
    assert all(r.actual_state == ProvisionState.ENABLED and r.synced_revision == r.revision for r in rows)


async def test_password_is_stored_encrypted_and_survives_repeated_opens(world: World) -> None:
    view = await world.open(1, max_targets=1)
    (row,) = await world.rows(1)
    assert row.password_enc and view.entries[0].link is not None
    assert view.entries[0].link.password not in row.password_enc
    assert world.password(row) == view.entries[0].link.password

    again = await world.open(1, max_targets=1)
    assert again.entries[0].link == view.entries[0].link
    assert len(world.fakes[0].applies()) == 1  # nothing to push the second time
    assert len(await world.rows(1)) == 1


async def test_one_account_per_panel_and_least_loaded_first(world: World) -> None:
    await world.open(1, max_targets=1)
    await world.open(2, max_targets=1)
    rows = await world.rows()
    # Both users prefer the first location; a second server is used only when asked for.
    assert [r.panel_id for r in rows] == [world.panel_ids[0], world.panel_ids[0]]
    await world.open(1, max_targets=2)
    assert len(await world.rows(1)) == 2
    await world.open(1, max_targets=5)
    assert len(await world.rows(1)) == 2


async def test_no_targets_when_nothing_is_selected_or_disabled(world: World) -> None:
    for inbound_id in world.inbound_ids:
        async with world.session_factory.begin() as session:
            assert await tg_socks.set_inbound(session, inbound_id, False)
    ensured = await tg_socks.ensure_access(world.session_factory, world.settings, BOX, 1, now=NOW)
    assert ensured.policy.allowed and ensured.no_targets and ensured.created == []

    async with world.session_factory.begin() as session:
        assert await tg_socks.set_inbound(session, world.inbound_ids[0], True)
        assert not await tg_socks.set_inbound(session, 99999, True)
    off = make_settings(admin_ids=[1000], tg_socks_enabled=False)
    assert (await tg_socks.ensure_access(world.session_factory, off, BOX, 1, now=NOW)).no_targets


# ---------------------------------------------------------------------------------------------
# Access policy: modes, subscriptions, bans, removal
# ---------------------------------------------------------------------------------------------
async def test_subscribers_mode_disables_accounts_without_a_subscription(world: World) -> None:
    world.settings = make_settings(admin_ids=[1000], tg_proxy_access="subscribers")
    view = await world.open(1, max_targets=1)
    assert not view.policy.allowed and view.entries == () and await world.rows(1) == []

    await add_sub(world, 2, days=10)
    view = await world.open(2, max_targets=1)
    assert view.policy.allowed and len(view.ready) == 1
    password = view.ready[0].link.password  # type: ignore[union-attr]
    assert world.fakes[0].accounts[5]["tg2"] == password

    # The subscription ends: the account leaves the inbound (other accounts stay), the row keeps the password.
    world.fakes[0].accounts[5]["someone-else"] = "x"
    report = await world.reconcile(now=NOW + timedelta(days=11))
    assert report.synced == 1
    assert world.fakes[0].accounts[5] == {"someone-else": "x"}
    (row,) = await world.rows(2)
    assert row.actual_state == ProvisionState.DISABLED and world.password(row) == password
    shown = await tg_socks.user_view(world.session_factory, world.settings, BOX, 2, now=NOW + timedelta(days=11))
    assert shown.entries == () or not shown.ready

    # Renewing brings the very same credentials back.
    await world.execute(
        update(Subscription).where(Subscription.user_id == 2).values(expires_at=NOW + timedelta(days=40))
    )
    async with world.session_factory.begin() as session:
        await tg_socks.mark_user_dirty(session, 2)
    await world.reconcile(now=NOW + timedelta(days=12))
    assert world.fakes[0].accounts[5]["tg2"] == password


async def test_off_mode_disables_but_staff_keep_their_account(world: World) -> None:
    await world.open(1, max_targets=1)
    await world.open(1000, max_targets=1)
    world.settings = make_settings(admin_ids=[1000], tg_proxy_access="off")
    await world.reconcile()
    assert set(world.fakes[0].accounts[5]) == {"tg1000"}
    off_view = await world.open(1, max_targets=1)
    assert not off_view.policy.allowed and off_view.ready == ()
    rows = {r.user_id: r for r in await world.rows()}
    assert rows[1].actual_state == ProvisionState.DISABLED and rows[1000].actual_state == ProvisionState.ENABLED


async def test_ban_removes_the_account_and_unban_restores_it(world: World) -> None:
    view = await world.open(3, max_targets=1)
    password = view.ready[0].link.password  # type: ignore[union-attr]
    async with world.session_factory.begin() as session:
        user = await session.get(User, 3)
        assert user is not None
        assert await bans.set_banned(session, user, True)
    (row,) = await world.rows(3)
    assert row.revision > row.synced_revision  # the ban marks the account dirty
    await world.reconcile()
    assert world.fakes[0].accounts[5] == {}
    banned = await tg_socks.user_view(world.session_factory, world.settings, BOX, 3)
    assert not banned.policy.allowed and banned.ready == ()

    async with world.session_factory.begin() as session:
        user = await session.get(User, 3)
        assert user is not None
        assert await bans.set_banned(session, user, False)
    await world.reconcile()
    assert world.fakes[0].accounts[5] == {"tg3": password}


async def test_deselecting_an_inbound_drains_its_accounts(world: World) -> None:
    await world.open(1, max_targets=2)
    async with world.session_factory.begin() as session:
        assert await tg_socks.set_inbound(session, world.inbound_ids[0], False)
    report = await world.reconcile()
    assert report.deleted == 1
    assert world.fakes[0].accounts[5] == {} and world.fakes[1].accounts[6] != {}
    assert [r.inbound_id for r in await world.rows(1)] == [world.inbound_ids[1]]


async def test_global_switch_drains_everything(world: World) -> None:
    await world.open(1, max_targets=2)
    world.settings = make_settings(admin_ids=[1000], tg_socks_enabled=False)
    await world.reconcile()
    assert await world.rows() == [] and world.fakes[0].accounts[5] == {} and world.fakes[1].accounts[6] == {}


async def test_remove_user_deletes_accounts_and_keeps_rows_on_failure(world: World) -> None:
    await world.open(1, max_targets=2)
    await world.open(2, max_targets=1)
    world.fakes[1].fail_next = 1
    report = await tg_socks.remove_user(world.session_factory, world.registry, 1)
    assert (report.deleted, report.failed) == (1, 1)
    assert [r.panel_id for r in await world.rows(1)] == [world.panel_ids[1]]  # the failed one is kept for a retry
    assert set(world.fakes[0].accounts[5]) == {"tg2"}
    report = await tg_socks.remove_user(world.session_factory, world.registry, 1)
    assert report.ok and await world.rows(1) == [] and world.fakes[1].accounts[6] == {}


# ---------------------------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------------------------
async def test_rotate_replaces_the_password_everywhere_and_respects_the_cooldown(world: World) -> None:
    view = await world.open(1, max_targets=2)
    old = [e.link.password for e in view.entries if e.link]
    world.fakes[0].accounts[5]["other"] = "keep"

    assert await tg_socks.rotate(world.session_factory, BOX, 1, min_interval=timedelta(hours=1), now=NOW) == 2
    # Until the new password is pushed no link is shown (the old one is already revoked in the database).
    pending = await tg_socks.user_view(world.session_factory, world.settings, BOX, 1)
    assert pending.ready == ()
    with pytest.raises(tg_socks.RotationTooSoonError):
        await tg_socks.rotate(world.session_factory, BOX, 1, min_interval=timedelta(hours=1), now=NOW)

    view = await world.open(1, max_targets=2)
    new = [e.link.password for e in view.entries if e.link]
    assert len(new) == 2 and not set(new) & set(old)
    assert world.fakes[0].accounts[5] == {"tg1": new[0], "other": "keep"}
    assert world.fakes[1].accounts[6] == {"tg1": new[1]}
    assert await tg_socks.rotate(world.session_factory, BOX, 77, min_interval=timedelta(hours=1)) == 0


# ---------------------------------------------------------------------------------------------
# Failures, drift, orphans
# ---------------------------------------------------------------------------------------------
async def test_failed_push_backs_off_and_recovers(world: World) -> None:
    world.fakes[0].fail_next = 1
    view = await world.open(1, max_targets=1)
    assert view.ready == () and view.entries[0].error == "boom"
    (row,) = await world.rows(1)
    assert row.attempts == 1 and row.next_attempt_at is not None and row.next_attempt_at > NOW
    assert (await world.reconcile(now=NOW)).synced == 0  # still backing off
    report = await world.reconcile(now=NOW + timedelta(minutes=5))
    assert report.synced == 1
    assert len((await tg_socks.user_view(world.session_factory, world.settings, BOX, 1)).ready) == 1


async def test_unreadable_password_is_replaced(world: World) -> None:
    await world.open(1, max_targets=1)
    await world.execute(update(TgSocksAccess).values(password_enc="enc:garbage"))
    view = await world.open(1, max_targets=1)
    assert view.ready and view.ready[0].link is not None
    (row,) = await world.rows(1)
    assert world.password(row) == view.ready[0].link.password
    assert world.fakes[0].accounts[5]["tg1"] == view.ready[0].link.password


async def test_scan_repairs_drift_and_removes_only_orphans_we_named(world: World) -> None:
    world.settings = make_settings(admin_ids=[1000], delete_orphan_clients=True)
    view = await world.open(1, max_targets=1)
    password = view.ready[0].link.password  # type: ignore[union-attr]
    fake = world.fakes[0]
    fake.accounts[5].update({"admin-made": "keep", "tg999": "ghost"})
    fake.accounts[5]["tg1"] = "tampered"

    report = await world.scan()
    assert report.drifted == 1 and report.orphans == [f"{world.panel_ids[0]}:tg999"] and report.orphans_deleted == 1
    assert "tg999" not in fake.accounts[5] and fake.accounts[5]["admin-made"] == "keep"
    await world.reconcile()
    assert fake.accounts[5]["tg1"] == password

    fake.accounts[5].pop("tg1")  # deleted on the server
    assert (await world.scan()).drifted == 1
    await world.reconcile()
    assert fake.accounts[5]["tg1"] == password


async def test_scan_removes_accounts_that_should_not_be_there(world: World) -> None:
    await world.open(1, max_targets=1)
    async with world.session_factory.begin() as session:
        user = await session.get(User, 1)
        assert user is not None
        await bans.set_banned(session, user, True)
    await world.reconcile()
    assert world.fakes[0].accounts[5] == {}
    world.fakes[0].accounts[5]["tg1"] = "sneaked-back"
    assert (await world.scan()).drifted == 1
    await world.reconcile()
    assert world.fakes[0].accounts[5] == {}


async def test_scan_never_deletes_orphans_when_the_database_has_no_rows(world: World) -> None:
    world.settings = make_settings(admin_ids=[1000], delete_orphan_clients=True)
    world.fakes[0].accounts[5] = {"tg5": "x"}
    report = await world.scan()
    assert report.orphans == [f"{world.panel_ids[0]}:tg5"] and report.orphans_deleted == 0
    assert world.fakes[0].accounts[5] == {"tg5": "x"}


async def test_panel_without_socks_support_is_an_error_not_a_crash(world: World) -> None:
    world.fakes = [object(), FakeSocksPanel()]  # type: ignore[list-item]
    assert not isinstance(world.fakes[0], SocksProxyBackend)
    view = await world.open(1, max_targets=1)
    assert view.ready == () and "cannot host SOCKS5" in (view.entries[0].error or "")


async def test_deleting_a_user_removes_the_socks_account_but_not_others(world: World) -> None:
    from app.modules.admin_users import ops

    await world.open(2, max_targets=1)
    await world.open(3, max_targets=1)
    result = await ops.delete_user(world.session_factory, world.registry, world.settings, 2, 1000)
    assert result.deleted and result.removed == 1 and result.errors == []
    assert set(world.fakes[0].accounts[5]) == {"tg3"}
    assert [r.user_id for r in await world.rows()] == [3]
