"""Shadowsocks-2022 end to end inside the bot: provisioning, drift, subscription entries and the admin endpoint."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote_plus, unquote, urlsplit

import pytest
from sqlalchemy import select

from app.db.models import (
    AdminRole,
    Endpoint,
    EndpointKind,
    EndpointProfile,
    Inbound,
    Location,
    Panel,
    Provision,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.db.session import SessionFactory
from app.modules.admin_api.routers import panels as panels_router
from app.services import connection_links
from app.services import connection_links as connection_groups
from app.services import provisioning as prov
from app.services import subscription_page as sp
from app.services.panels import shadowsocks
from tests.conftest import make_settings
from tests.fakes import FakeCapablePanel, FakePanel
from tests.test_mod_webapi_support import Api, api, as_role, ctx
from tests.test_subs_support import FakeRegistry, add_sub, add_user, add_xui_panel, days

__all__ = ["api", "ctx"]

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
METHOD = "2022-blake3-aes-128-gcm"
SS_PARAMS = {
    "protocol": "shadowsocks",
    "port": 8388,
    "type": "tcp",
    "security": "none",
    "ss_method": METHOD,
    "ss_password": "AAECAwQFBgcICQoLDA0ODw==",
}
VLESS_PARAMS = {"protocol": "vless", "port": 443, "type": "tcp", "security": "reality", "pbk": "K", "sni": "a.example"}


async def world(
    session_factory: SessionFactory, *, ss_params: dict[str, Any] | None = None
) -> tuple[FakeRegistry, Panel, list[Inbound], Subscription]:
    """One panel with a VLESS inbound (remote 1) and a Shadowsocks inbound (remote 2), one active subscription."""
    async with session_factory.begin() as s:
        panel, (vless, ss) = await add_xui_panel(s, inbounds=2, params=VLESS_PARAMS)
        ss.params = json.dumps(ss_params or SS_PARAMS)
        ss.flow = ""
        await add_user(s, 1)
        sub = await add_sub(s, 1, expires_at=NOW + days(10))
    return FakeRegistry({panel.id: FakePanel()}), panel, [vless, ss], sub


async def test_reconcile_gives_the_subscription_its_own_shadowsocks_client(
    session_factory: SessionFactory,
) -> None:
    settings = make_settings()
    registry, panel, (vless, ss), sub = await world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    fake = registry.panels[panel.id]
    key = shadowsocks.derive_client_key(sub.client_uuid, ss.id, METHOD)
    assert fake.accounts[(2, f"tg1-{ss.id}")].uuid == key
    assert fake.accounts[(1, f"tg1-{vless.id}")].uuid == sub.client_uuid

    fake.calls.clear()
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert fake.calls == []  # stable keys: nothing to re-push


async def test_spec_carries_a_key_not_a_flow(session_factory: SessionFactory) -> None:
    _, panel, (vless, ss), sub = await world(session_factory)
    async with session_factory() as s:
        user = await s.get(User, 1)
        assert user is not None
        for inbound in (vless, ss):
            provision = Provision(subscription_id=sub.id, panel_id=panel.id, inbound_id=inbound.id)
            spec = prov.build_spec(sub, user, provision, panel, inbound, make_settings(), now=NOW)
            assert (spec.password != "") is (inbound is ss)
            assert (spec.flow != "") is (inbound is vless)


async def test_expired_or_banned_subscriptions_are_disabled_on_the_shadowsocks_inbound(
    session_factory: SessionFactory,
) -> None:
    settings = make_settings()
    registry, panel, (vless, ss), sub = await world(session_factory)
    vless_id = vless.id
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    fake = registry.panels[panel.id]
    async with session_factory.begin() as s:
        row = await s.get(Subscription, sub.id)
        assert row is not None
        row.status = SubscriptionStatus.EXPIRED  # what expire_due does
        row.revision += 1
    await prov.reconcile(session_factory, registry, settings, now=NOW + days(12))
    assert fake.accounts[(2, f"tg1-{ss.id}")].enabled is False

    async with session_factory.begin() as s:
        row = await s.get(Subscription, sub.id)
        assert row is not None
        row.status = SubscriptionStatus.ACTIVE
        row.expires_at = NOW + days(30)
        row.revision += 1
        user = await s.get(User, 1)
        assert user is not None
        user.is_banned = True
    await prov.reconcile(session_factory, registry, settings, now=NOW + days(12))
    assert fake.accounts[(2, f"tg1-{ss.id}")].enabled is False
    assert fake.accounts[(1, f"tg1-{vless_id}")].enabled is False


async def test_rotation_rekeys_and_drift_is_repaired(session_factory: SessionFactory) -> None:
    settings = make_settings()
    registry, panel, (_, ss), sub = await world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    fake = registry.panels[panel.id]
    account = fake.accounts[(2, f"tg1-{ss.id}")]
    first = account.uuid

    account.uuid = "tampered"  # somebody changed the key on the panel
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    assert fake.accounts[(2, f"tg1-{ss.id}")].uuid == first

    async with session_factory.begin() as s:
        row = await s.get(Subscription, sub.id)
        assert row is not None
        row.client_uuid = "11111111-2222-4333-8444-555555555555"
        row.revision += 1
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    rotated = fake.accounts[(2, f"tg1-{ss.id}")].uuid
    assert rotated != first and rotated == shadowsocks.derive_client_key(row.client_uuid, ss.id, METHOD)


async def test_an_unsupported_inbound_is_drained(session_factory: SessionFactory) -> None:
    settings = make_settings()
    registry, panel, (_, ss), _ = await world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    fake = registry.panels[panel.id]
    assert (2, f"tg1-{ss.id}") in fake.accounts
    async with session_factory.begin() as s:
        row = await s.get(Inbound, ss.id)
        assert row is not None
        row.params = json.dumps({**SS_PARAMS, "ss_method": "aes-256-gcm", "unsupported": "deprecated"})
    await prov.reconcile(session_factory, registry, settings, now=NOW + timedelta(seconds=1))
    assert (2, f"tg1-{ss.id}") not in fake.accounts


async def test_method_change_marks_the_target_for_a_re_push(session_factory: SessionFactory) -> None:
    settings = make_settings()
    registry, panel, (_, ss), _ = await world(session_factory)
    await prov.reconcile(session_factory, registry, settings, now=NOW)
    fake = registry.panels[panel.id]
    old = fake.accounts[(2, f"tg1-{ss.id}")].uuid
    async with session_factory.begin() as s:
        row = await s.get(Inbound, ss.id)
        assert row is not None
        row.params = json.dumps({**SS_PARAMS, "ss_method": "2022-blake3-aes-256-gcm"})
    await prov.reconcile(session_factory, registry, settings, now=NOW + timedelta(seconds=1))
    new = fake.accounts[(2, f"tg1-{ss.id}")].uuid
    assert new != old and len(new) == 44


# ---------------------------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------------------------
async def seed_page(session_factory: SessionFactory, *, ss_kind: EndpointKind = EndpointKind.DIRECT) -> Subscription:
    async with session_factory.begin() as s:
        s.add_all(
            [
                Location(id=1, name="Германия", flag="🇩🇪", sort_order=0),
                Location(id=2, name="Нидерланды", flag="🇳🇱", sort_order=1),
            ]
        )
        _panel, (vless_de, ss_de, vless_nl) = await add_xui_panel(s, inbounds=3, params=VLESS_PARAMS)
        ss_de.params = json.dumps(SS_PARAMS)
        ss_de.flow = ""
        vless_de.location_id, ss_de.location_id, vless_nl.location_id = 1, 1, 2
        await s.flush()
        s.add_all(
            [
                Endpoint(
                    id=1,
                    inbound_id=vless_nl.id,
                    kind=EndpointKind.DIRECT,
                    profile=EndpointProfile.STANDARD,
                    address="198.51.100.2",
                ),
                Endpoint(
                    id=2,
                    inbound_id=ss_de.id,
                    kind=ss_kind,
                    profile=EndpointProfile.STANDARD,
                    address="203.0.113.9",
                    port=None,
                    overrides=json.dumps({"security": "tls", "sni": "x.example"}),
                ),
                Endpoint(
                    id=3,
                    inbound_id=vless_de.id,
                    kind=EndpointKind.DIRECT,
                    profile=EndpointProfile.STANDARD,
                    address="203.0.113.10",
                ),
                Endpoint(
                    id=4,
                    inbound_id=vless_de.id,
                    kind=EndpointKind.RELAY,
                    profile=EndpointProfile.WHITELIST,
                    address="84.201.1.2",
                    port=8443,
                ),
            ]
        )
        await add_user(s, 42)
        sub = await add_sub(s, 42, expires_at=NOW + days(10))
    return sub


async def test_subscription_lists_shadowsocks_after_vless_of_the_same_location(
    session_factory: SessionFactory,
) -> None:
    sp.clear_caches()
    sub = await seed_page(session_factory)
    settings = make_settings(reality_compact_hello=True)
    rendered = await sp.render(session_factory, FakeRegistry({}), settings, "token42", now=NOW)
    assert rendered is not None
    names = [unquote(urlsplit(link).fragment) for link in rendered.links]
    assert names == ["🇩🇪 Германия", "🇩🇪 Германия · LTE", "🇩🇪 Германия · SS", "🇳🇱 Нидерланды"]
    ss_link = rendered.links[2]
    assert ss_link.startswith("ss://2022-blake3-aes-128-gcm:AAECAwQFBgcICQoLDA0ODw%3D%3D:")
    key = shadowsocks.derive_client_key(sub.client_uuid, 2, METHOD)
    assert f":{quote_plus(key, safe='')}@203.0.113.9:8388?type=tcp#" in ss_link
    assert "security" not in ss_link and "fp=" not in ss_link  # endpoint kind / overrides and compact hello: not for SS
    assert rendered.links[0].startswith("vless://")


async def test_unsupported_inbound_is_not_in_the_subscription(session_factory: SessionFactory) -> None:
    sp.clear_caches()
    await seed_page(session_factory)
    async with session_factory.begin() as s:
        row = await s.get(Inbound, 2)
        assert row is not None
        row.params = json.dumps({**SS_PARAMS, "ss_method": "aes-256-gcm"})
    rendered = await sp.render(session_factory, FakeRegistry({}), make_settings(), "token42", now=NOW)
    assert rendered is not None and not any(link.startswith("ss://") for link in rendered.links)


async def test_server_profiles_include_shadowsocks(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    sp.clear_caches()
    await seed_page(session_factory)

    async def no_qr(_: str) -> bytes:
        raise ValueError

    monkeypatch.setattr(connection_links, "make_qr_png_async", no_qr)
    async with session_factory.begin() as s:
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == 42))
        assert sub is not None
        sub.expires_at = datetime.now(UTC) + days(10)  # connection_groups renders with the real clock
    async with session_factory() as s:
        user = await s.get(User, 42)
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == 42))
    assert user is not None
    settings = make_settings(sub_show_server_profiles=True, sub_show_server_subscriptions=True)
    ctx_like = SimpleNamespace(
        settings=settings, session_factory=session_factory, panels=FakeRegistry({}), secret_box=None
    )
    groups = await connection_groups.connection_groups(ctx_like, user, sub)
    uris = [p["uri"] for g in groups for p in g["profiles"]]
    assert any(u.startswith("ss://") for u in uris) and any(u.startswith("vless://") for u in uris)


# ---------------------------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def fake_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panels_router, "build_backend", lambda panel, box, settings: FakeCapablePanel(kind=panel.kind))


async def create_panel(api: Api) -> dict[str, Any]:
    body = {"name": "DE", "kind": "xui", "url": "https://de.example.com:2053/p", "username": "a", "password": "b"}
    resp = await api.post("/panels", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_admin_creates_a_shadowsocks_inbound(api: Api) -> None:
    panel = await create_panel(api)
    created = await api.post(
        f"/panels/{panel['id']}/inbounds/shadowsocks",
        json={"port": 8388, "method": "2022-blake3-aes-256-gcm", "endpoint_address": "de.example.com"},
    )
    assert created.status_code == 201, created.text
    inbound = created.json()
    assert inbound["protocol"] == "shadowsocks" and inbound["port"] == 8388
    assert inbound["ss_method"] == "2022-blake3-aes-256-gcm" and inbound["unsupported_reason"] is None
    assert inbound["is_active"] is True and inbound["endpoints"] == 1
    assert ("create_shadowsocks_inbound", 8388, "2022-blake3-aes-256-gcm") in api.ctx.panels.backend(panel["id"]).calls
    taken = await api.post(f"/panels/{panel['id']}/inbounds/shadowsocks", json={"port": 8388})
    assert taken.status_code == 409


async def test_admin_endpoint_validation_and_rbac(api: Api) -> None:
    panel = await create_panel(api)
    url = f"/panels/{panel['id']}/inbounds/shadowsocks"
    assert (await api.post(url, json={"port": 8388, "method": "aes-256-gcm"})).status_code == 422
    assert (await api.post(url, json={"port": 8388, "method": "2022-blake3-chacha20-poly1305"})).status_code == 422
    assert (await api.post(url, json={"port": 0})).status_code == 422
    assert (await api.post("/panels/999/inbounds/shadowsocks", json={"port": 8388})).status_code == 404
    inactive = await api.post(url, json={"port": 8389, "activate": False})
    assert inactive.status_code == 201 and inactive.json()["is_active"] is False
    support = await as_role(api, AdminRole.SUPPORT)
    assert (await support.post(url, json={"port": 8390})).status_code == 403


async def test_a_deprecated_shadowsocks_inbound_cannot_be_activated(api: Api) -> None:
    panel = await create_panel(api)
    async with api.ctx.session_factory.begin() as s:
        bad = Inbound(
            panel_id=panel["id"],
            remote_id=50,
            name="old-ss",
            params=json.dumps({**SS_PARAMS, "ss_method": "aes-256-gcm", "unsupported": "method is deprecated"}),
            is_active=False,
        )
        s.add(bad)
        await s.flush()
        bad_id = bad.id
    resp = await api.patch(f"/inbounds/{bad_id}", json={"is_active": True})
    assert resp.status_code == 400 and resp.json()["error"] == "shadowsocks_unsupported"
    listed = {i["id"]: i for i in (await api.get("/inbounds")).json()}
    assert listed[bad_id]["unsupported_reason"] == "method is deprecated"
