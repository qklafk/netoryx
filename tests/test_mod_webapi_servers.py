from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select

from app.db.models import (
    AdminRole,
    Endpoint,
    Inbound,
    Panel,
    PanelKind,
    Provision,
    ProvisionState,
    Subscription,
    TgProxyAccess,
)
from app.modules.admin_api.routers import locations as locations_router
from app.modules.admin_api.routers import panels as panels_router
from tests.fakes import FakeCapablePanel
from tests.test_mod_webapi_support import Api, add_user, api, as_role, ctx

__all__ = ["api", "ctx"]


@pytest.fixture(autouse=True)
def fake_backends(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    built: list[Any] = []

    def build(panel: Panel, box: Any, settings: Any) -> FakeCapablePanel:
        backend = FakeCapablePanel(kind=panel.kind)
        if "down" in panel.url:
            backend.down = True
        built.append(panel)
        return backend

    monkeypatch.setattr(panels_router, "build_backend", build)
    return built


async def _create_xui(api: Api, **extra: Any) -> dict[str, Any]:
    body = {
        "name": "DE-1",
        "kind": "xui",
        "url": "https://de.example.com:2053/panel/",
        "username": "admin",
        "password": "s3cret",
        "endpoint_address": "de.example.com",
        **extra,
    }
    resp = await api.post("/panels", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_panel_lifecycle(api: Api, fake_backends: list[Any]) -> None:
    ctx = api.ctx
    loc = (await api.post("/locations", json={"name": "Германия", "flag": "🇩🇪"})).json()
    panel = await _create_xui(api, location_id=loc["id"], options={"headers": {"X-Api-Key": "abc"}})
    assert panel["url"] == "https://de.example.com:2053/panel" and panel["has_password"] is True
    assert panel["version"] == "3.11.0" and panel["inbounds"] == 1
    assert panel["options"] == {"headers": {"X-Api-Key": "***"}}
    stored_panel = fake_backends[0]
    assert stored_panel.password.startswith("enc:") and "s3cret" not in stored_panel.password
    async with ctx.session_factory() as s:
        inbound = await s.scalar(select(Inbound))
        endpoint = await s.scalar(select(Endpoint))
    assert inbound is not None and inbound.location_id == loc["id"] and inbound.is_active
    assert endpoint is not None and endpoint.address == "de.example.com"

    failed = await api.post(
        "/panels", json={"name": "x", "kind": "xui", "url": "https://down.example.com", "password": "p"}
    )
    assert failed.status_code == 409 and failed.json()["error"] == "connection_failed"
    assert (await api.post("/panels", json={"name": "x", "kind": "xui", "url": "ftp://a.example"})).status_code == 422

    tested = await api.post(
        "/panels/test", json={"kind": "remnawave", "url": "https://rw.example.com", "api_token": "t"}
    )
    assert tested.json()["ok"] is True and tested.json()["version"] == "3.11.0"
    again = (await api.post("/panels/test", json={"panel_id": panel["id"]})).json()
    assert again["ok"] is True and again["inbounds"][0]["remote_id"] == 1

    kept = (await api.patch(f"/panels/{panel['id']}", json={"options": {"headers": {"X-Api-Key": "***"}}})).json()
    assert kept["options"]["headers"]["X-Api-Key"] == "***"
    async with ctx.session_factory() as s:
        row = await s.get(Panel, panel["id"])
        assert row is not None and json.loads(row.options or "{}")["headers"]["X-Api-Key"] == "abc"
    renamed = (await api.patch(f"/panels/{panel['id']}", json={"name": "DE-2", "is_active": False})).json()
    assert renamed["name"] == "DE-2" and renamed["is_active"] is False and panel["id"] in ctx.panels.invalidated

    admin = await as_role(api, AdminRole.ADMIN)
    assert (await admin.patch(f"/panels/{panel['id']}", json={"password": "new"})).status_code == 403
    assert (await admin.patch(f"/panels/{panel['id']}", json={"name": "ok"})).status_code == 200
    new_panel = {"name": "y", "kind": "xui", "url": "https://y.example.com"}
    assert (await admin.post("/panels", json=new_panel)).status_code == 403


async def test_panel_status_restart_sync_refresh(api: Api) -> None:
    ctx = api.ctx
    panel = await _create_xui(api)
    status = (await api.get(f"/panels/{panel['id']}/status")).json()
    assert status["supported"] is True and status["status"]["cpu_percent"] == 5.0
    assert status["status"]["core_running"] is True
    assert (await api.post(f"/panels/{panel['id']}/restart-core")).json() == {"ok": True}
    assert ctx.panels.backend(panel["id"]).restarts == 1
    synced = (await api.post(f"/panels/{panel['id']}/sync")).json()
    assert synced["marked"] == 0 and synced["started_jobs"] == ["provisioning.reconcile"]
    refresh = (await api.post(f"/panels/{panel['id']}/inbounds/refresh", json={})).json()
    assert refresh["created"] == [] and refresh["skipped_remote_ids"] == []
    checked = (await api.post(f"/panels/{panel['id']}/check")).json()
    assert checked["ok"] is True
    ctx.panels.backend(panel["id"]).down = True
    down = (await api.post(f"/panels/{panel['id']}/check")).json()
    assert down["ok"] is False
    async with ctx.session_factory() as s:
        row = await s.get(Panel, panel["id"])
        assert row is not None and row.last_error


async def test_create_reality_inbound(api: Api) -> None:
    ctx = api.ctx
    panel = await _create_xui(api)
    created = await api.post(
        f"/panels/{panel['id']}/inbounds/reality",
        json={"port": 8443, "sni": "www.microsoft.com", "endpoint_address": "de.example.com"},
    )
    assert created.status_code == 201, created.text
    inbound = created.json()
    assert inbound["port"] == 8443 and inbound["security"] == "reality" and inbound["flow"] == "xtls-rprx-vision"
    assert inbound["endpoints"] == 1 and inbound["is_active"] is True
    taken = await api.post(f"/panels/{panel['id']}/inbounds/reality", json={"port": 8443, "sni": "www.apple.com"})
    assert taken.status_code == 409
    assert ("create_reality_inbound", 8443, "www.microsoft.com") in ctx.panels.backend(panel["id"]).calls
    assert len((await api.get("/reality/sni")).json()) >= 5


async def test_inbound_update_and_list(api: Api) -> None:
    panel = await _create_xui(api)
    inbounds = (await api.get("/inbounds", params={"panel_id": panel["id"]})).json()
    assert len(inbounds) == 1 and inbounds[0]["tg_proxy"] is None
    inbound_id = inbounds[0]["id"]
    loc = (await api.post("/locations", json={"name": "NL", "flag": "🇳🇱"})).json()
    updated = (await api.patch(f"/inbounds/{inbound_id}", json={"location_id": loc["id"], "sort_order": 3})).json()
    assert updated["location_id"] == loc["id"] and updated["sort_order"] == 3
    cleared = (await api.patch(f"/inbounds/{inbound_id}", json={"clear_location": True})).json()
    assert cleared["location_id"] is None
    not_mtproto = await api.patch(f"/inbounds/{inbound_id}", json={"tg_proxy": True})
    assert not_mtproto.status_code == 400
    assert (await api.patch(f"/inbounds/{inbound_id}", json={"location_id": 999})).status_code == 400


async def test_delete_panel_with_accounts(api: Api) -> None:
    ctx = api.ctx
    panel = await _create_xui(api)
    await add_user(ctx, 31, sub_days=5)
    async with ctx.session_factory.begin() as s:
        inbound = await s.scalar(select(Inbound))
        sub = await s.scalar(select(Subscription))
        assert inbound is not None and sub is not None
        s.add(
            Provision(
                subscription_id=sub.id,
                panel_id=panel["id"],
                inbound_id=inbound.id,
                inbound_key=inbound.id,
                remote_email=f"tg31-{inbound.id}",
                desired_state=ProvisionState.ENABLED,
                actual_state=ProvisionState.ENABLED,
            )
        )
    listed = (await api.get("/panels")).json()
    assert listed[0]["accounts"] == 1
    backend = ctx.panels.backend(panel["id"])
    backend.down = True
    assert (await api.delete(f"/panels/{panel['id']}", params={"remove_accounts": True})).status_code == 409
    forced = (await api.delete(f"/panels/{panel['id']}", params={"remove_accounts": True, "force": True})).json()
    assert forced["deleted"] is True and forced["accounts_removed"] == 0 and forced["errors"]
    async with ctx.session_factory() as s:
        assert await s.scalar(select(func.count(Panel.id))) == 0
        assert await s.scalar(select(func.count(Provision.id))) == 0
    assert (await api.get(f"/panels/{panel['id']}")).status_code == 404


async def test_delete_panel_removes_accounts(api: Api) -> None:
    ctx = api.ctx
    panel = await _create_xui(api)
    await add_user(ctx, 32, sub_days=5)
    async with ctx.session_factory.begin() as s:
        inbound = await s.scalar(select(Inbound))
        sub = await s.scalar(select(Subscription))
        assert inbound is not None and sub is not None
        s.add(
            Provision(
                subscription_id=sub.id,
                panel_id=panel["id"],
                inbound_id=inbound.id,
                inbound_key=inbound.id,
                remote_email="tg32-1",
                desired_state=ProvisionState.ENABLED,
                actual_state=ProvisionState.ENABLED,
            )
        )
    result = (await api.delete(f"/panels/{panel['id']}", params={"remove_accounts": True})).json()
    assert result == {"deleted": True, "accounts_removed": 1, "errors": []}
    assert ("delete", 1, "tg32-1") in ctx.panels.backend(panel["id"]).calls


async def test_panel_test_new_target_needs_owner(api: Api, fake_backends: list[Any]) -> None:
    panel = await _create_xui(api, options={"headers": {"X-Api-Key": "abc"}})
    admin = await as_role(api, AdminRole.ADMIN)
    built = len(fake_backends)
    for change in (
        {"url": "https://attacker.example"},
        {"username": "other"},
        {"tls_verify": False},
        {"options": {"headers": {"X-Api-Key": "***"}, "base_path": "/x"}},
    ):
        resp = await admin.post("/panels/test", json={"panel_id": panel["id"], **change})
        assert resp.status_code == 403, (change, resp.text)
    assert len(fake_backends) == built  # stored credentials never left for another target
    # Re-testing the saved settings (or sending the stored values back) is fine for an admin.
    same = {"panel_id": panel["id"], "url": panel["url"], "username": "admin", "tls_verify": True}
    assert (await admin.post("/panels/test", json=same)).json()["ok"] is True
    assert (await admin.post("/panels/test", json={"panel_id": panel["id"]})).json()["ok"] is True
    owner_try = await api.post("/panels/test", json={"panel_id": panel["id"], "url": "https://other.example"})
    assert owner_try.json()["ok"] is True and fake_backends[-1].url == "https://other.example"


class _ProxyCapablePanel(FakeCapablePanel):
    async def delete_proxy_user(self, target: int | None, name: str) -> None:
        self.calls.append(("delete_proxy", target, name))
        self._maybe_fail("delete_proxy_user")


async def test_delete_panel_removes_proxy_accounts(api: Api) -> None:
    ctx = api.ctx
    resp = await api.post(
        "/panels", json={"name": "TG", "kind": "telemt", "url": "https://tg.example.com", "api_token": "t"}
    )
    assert resp.status_code == 201, resp.text
    panel = resp.json()
    await add_user(ctx, 33)
    async with ctx.session_factory.begin() as s:
        s.add(
            TgProxyAccess(
                user_id=33,
                panel_id=panel["id"],
                remote_name="tg33",
                desired_state=ProvisionState.ENABLED,
                actual_state=ProvisionState.ENABLED,
            )
        )
    backend = _ProxyCapablePanel(kind=PanelKind.TELEMT)
    ctx.panels.backends[panel["id"]] = backend
    result = (await api.delete(f"/panels/{panel['id']}", params={"remove_accounts": True})).json()
    assert result == {"deleted": True, "accounts_removed": 1, "errors": []}
    assert ("delete_proxy", None, "tg33") in backend.calls


async def test_delete_panel_proxy_failure_blocks_without_force(api: Api) -> None:
    ctx = api.ctx
    resp = await api.post(
        "/panels", json={"name": "TG", "kind": "telemt", "url": "https://tg.example.com", "api_token": "t"}
    )
    panel = resp.json()
    await add_user(ctx, 34)
    async with ctx.session_factory.begin() as s:
        s.add(
            TgProxyAccess(
                user_id=34,
                panel_id=panel["id"],
                remote_name="tg34",
                desired_state=ProvisionState.ENABLED,
                actual_state=ProvisionState.ENABLED,
            )
        )
    ctx.panels.backends[panel["id"]] = FakeCapablePanel(kind=PanelKind.TELEMT)  # cannot remove proxy users
    assert (await api.delete(f"/panels/{panel['id']}", params={"remove_accounts": True})).status_code == 409
    backend = _ProxyCapablePanel(kind=PanelKind.TELEMT)
    backend.down = True
    ctx.panels.backends[panel["id"]] = backend
    assert (await api.delete(f"/panels/{panel['id']}", params={"remove_accounts": True})).status_code == 409
    forced = (await api.delete(f"/panels/{panel['id']}", params={"remove_accounts": True, "force": True})).json()
    assert forced["deleted"] is True and forced["accounts_removed"] == 0 and forced["errors"]
    async with ctx.session_factory() as s:
        assert await s.scalar(select(func.count(TgProxyAccess.id))) == 0


# ---------------------------------------------------------------------------------------------
# Locations & endpoints
# ---------------------------------------------------------------------------------------------
async def test_locations_crud(api: Api) -> None:
    a = (await api.post("/locations", json={"name": "Германия", "flag": "🇩🇪"})).json()
    b = (await api.post("/locations", json={"name": "Нидерланды", "flag": "🇳🇱"})).json()
    assert b["sort_order"] == a["sort_order"] + 1
    order = (await api.post("/locations/reorder", json={"ids": [b["id"], a["id"]]})).json()
    assert [x["id"] for x in order] == [b["id"], a["id"]]
    assert (await api.patch(f"/locations/{a['id']}", json={"is_active": False})).json()["is_active"] is False
    assert (await api.delete(f"/locations/{a['id']}")).status_code == 200
    assert [x["id"] for x in (await api.get("/locations")).json()] == [b["id"]]
    assert (await api.post("/locations/reorder", json={"ids": [999]})).status_code == 400


async def test_endpoints_preview_and_probe(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    panel = await _create_xui(api, endpoint_address=None)
    inbound_id = (await api.get("/inbounds")).json()[0]["id"]
    presets = {p["key"]: p for p in (await api.get("/endpoints/presets")).json()}
    assert {"relay_l4", "yandex_cdn_xhttp", "reality_ru_sni"} <= set(presets)

    draft = {"inbound_id": inbound_id, "kind": "relay", "address": "relay.example.ru", "port": 9443}
    preview = (await api.post("/endpoints/preview", json={"draft": draft})).json()
    assert preview["ok"] is True and preview["link"].startswith("vless://")
    assert "@relay.example.ru:9443?" in preview["link"] and "pbk=PUBKEY" in preview["link"]

    created = await api.post("/endpoints", json=draft)
    assert created.status_code == 201
    endpoint = created.json()
    assert endpoint["panel_id"] == panel["id"]
    cdn = {
        "inbound_id": inbound_id,
        "kind": "cdn",
        "profile": "whitelist",
        "address": "cdn.example.ru",
        "overrides": presets["yandex_cdn_xhttp"]["overrides"],
    }
    cdn_preview = (await api.post("/endpoints/preview", json={"draft": cdn})).json()
    assert cdn_preview["ok"] and "security=tls" in cdn_preview["link"] and "sni=cdn.example.ru" in cdn_preview["link"]
    assert (await api.post("/endpoints", json={**draft, "inbound_id": 999})).status_code == 400

    patch = {"remark": "Relay RU", "clear_port": True}
    patched = (await api.patch(f"/endpoints/{endpoint['id']}", json=patch)).json()
    assert patched["remark"] == "Relay RU" and patched["port"] is None
    saved_preview = (await api.post("/endpoints/preview", json={"endpoint_id": endpoint["id"]})).json()
    assert ":443?" in saved_preview["link"] and "#Relay%20RU" in saved_preview["link"]

    async def fake_probe(host: str, port: int, timeout_sec: float = 3.0) -> str | None:
        return None if host == "relay.example.ru" else f"{host}:{port}: timeout"

    monkeypatch.setattr(locations_router.inventory, "tcp_probe", fake_probe)
    probe = (await api.post(f"/endpoints/{endpoint['id']}/probe")).json()
    assert probe["ok"] is True
    listed = (await api.get("/endpoints", params={"inbound_id": inbound_id})).json()
    assert listed[0]["last_ok_at"] is not None
    sni = (await api.post("/reality/sni/check", json={"sni": "www.apple.com"})).json()
    assert sni["reachable"] is False
    assert (await api.delete(f"/endpoints/{endpoint['id']}")).status_code == 200
    assert (await api.get("/endpoints")).json() == []


async def test_update_panel_disabling_tls_verify_needs_owner(api: Api) -> None:
    panel = await _create_xui(api)
    admin = await as_role(api, AdminRole.ADMIN)
    resp = await admin.patch(f"/panels/{panel['id']}", json={"tls_verify": False})
    assert resp.status_code == 403, resp.text
    async with api.ctx.session_factory() as s:
        row = await s.get(Panel, panel["id"])
        assert row is not None and row.tls_verify is True
    owner = await api.patch(f"/panels/{panel['id']}", json={"tls_verify": False})
    assert owner.status_code == 200 and owner.json()["tls_verify"] is False
    # Turning verification back on only makes the connection stricter, so an admin may do it.
    back = await admin.patch(f"/panels/{panel['id']}", json={"tls_verify": True})
    assert back.status_code == 200 and back.json()["tls_verify"] is True
