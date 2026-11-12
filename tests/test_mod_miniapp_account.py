from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.db.models import PanelKind, User
from app.db.session import SessionFactory
from app.services import devices
from app.services.devices import DeviceInfo, PanelDevices, ResetResult
from app.services.maintenance import MaintenanceService
from tests.test_mod_miniapp_support import (
    BASE,
    USER_ID,
    Env,
    add_member,
    add_user,
    auth,
    miniapp_env,
    stub_module,
    with_modules,
)


@pytest.fixture
async def env(tmp_path: Path, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    async with miniapp_env(tmp_path, session_factory, monkeypatch) as environment:
        yield environment


async def start_trial(env: Env) -> dict[str, Any]:
    resp = await env.post("/subscription/trial")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------------------------
# Auth & profile
# ---------------------------------------------------------------------------------------------
async def test_requires_init_data(env: Env) -> None:
    resp = await env.client.get(f"{BASE}/me")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"
    bad = await env.client.get(f"{BASE}/me", headers={"authorization": "tma user=1&hash=00"})
    assert bad.status_code == 401


async def test_me_registers_user_and_lists_features(env: Env) -> None:
    resp = await env.get("/me")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == USER_ID and body["username"] == "ann" and body["language"] == "ru"
    assert body["mode"] == "shop" and body["brand"] == "TestVPN" and body["bot_username"] == "test_vpn_bot"
    assert body["balance_kop"] == 0 and body["has_subscription"] is False
    assert body["trial_available"] is True and body["trial_days"] == 3
    assert body["features"] == {
        "subscription": True,
        "plans": True,
        "payments": True,
        "wallet": True,
        "referral": True,
        "promo": True,
        "gifts": True,
        "tickets": True,
        "tg_proxy": True,
    }


async def test_personal_mode_hides_selling_features(env: Env) -> None:
    env.ctx.box.update(app_mode="personal")
    unknown = await env.get("/me")
    assert unknown.status_code == 403 and unknown.json()["error"] == "not_registered"

    await add_user(env.ctx.session_factory, env.settings)
    stranger = await env.get("/me")
    assert stranger.status_code == 403 and stranger.json()["error"] == "invite_only"

    await add_member(env.ctx.session_factory)
    body = (await env.get("/me")).json()
    assert body["mode"] == "personal" and body["trial_available"] is False
    features = body["features"]
    assert features["subscription"] and features["tickets"] and features["tg_proxy"]
    assert not any(features[name] for name in ("plans", "payments", "wallet", "referral", "promo", "gifts"))
    resp = await env.get("/plans")
    assert resp.status_code == 404 and resp.json()["error"] == "feature_disabled"
    assert (await env.post("/subscription/trial")).status_code == 404


async def test_module_switches_are_respected(env: Env) -> None:
    with_modules(env, stub_module("subscription", enabled=False), stub_module("tickets"))
    resp = await env.get("/subscription")
    assert resp.status_code == 404
    assert resp.json() == {"error": "feature_disabled", "message": "Этот раздел сейчас недоступен."}
    features = (await env.get("/me")).json()["features"]
    assert features["subscription"] is False and features["tickets"] is True


async def test_language_switch(env: Env) -> None:
    resp = await env.post("/me/language", {"language": "en"})
    assert resp.status_code == 200 and resp.json() == {"language": "en"}
    async with env.ctx.session_factory() as session:
        user = await session.get(User, USER_ID)
        assert user is not None and user.language == "en"
    # Errors now come in English.
    missing = await env.get("/devices")
    assert missing.json()["message"] == "You don't have a subscription yet."
    assert (await env.post("/me/language", {"language": "de"})).status_code == 422


async def test_banned_user_is_refused(env: Env) -> None:
    await add_user(env.ctx.session_factory, env.settings, is_banned=True)
    resp = await env.get("/me")
    assert resp.status_code == 403 and resp.json()["error"] == "banned"


# ---------------------------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------------------------
async def test_maintenance_blocks_users_but_not_admins(env: Env) -> None:
    service = MaintenanceService(env.ctx.session_factory, cache_ttl=0)
    env.ctx.maintenance = service
    eta = datetime.now(UTC) + timedelta(hours=1)
    await service.enable(1000, eta=eta)
    resp = await env.get("/me")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "maintenance" and body["eta"] == eta.isoformat()
    assert body["message"].startswith("Идут технические работы")
    admin = await env.get("/me", user_id=1000)
    assert admin.status_code == 200
    await service.set_message(1000, message_ru="Обновляем серверы", message_en="Updating servers")
    assert (await env.get("/me")).json()["message"] == "Обновляем серверы"

    await service.disable(1000)
    await service.update(lambda s: s.disabled_modules.append("tickets"), actor_id=None, action="test")
    assert (await env.get("/tickets")).status_code == 404
    assert (await env.get("/tickets", user_id=1000)).status_code == 200


# ---------------------------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------------------------
async def test_subscription_without_one(env: Env) -> None:
    body = (await env.get("/subscription")).json()
    assert body["exists"] is False and body["state"] == "none" and body["trial_available"] is True


async def test_trial_flow(env: Env) -> None:
    card = await start_trial(env)
    assert card["exists"] and card["state"] == "active" and card["is_trial"] is True
    assert card["days_left"] == 3 and card["trial_available"] is False
    assert card["auto_renew_available"] is True

    again = await env.post("/subscription/trial")
    assert again.status_code == 409 and again.json()["error"] == "trial_used"
    body = (await env.get("/subscription")).json()
    assert body["state"] == "active" and body["traffic_used_bytes"] == 0
    me = (await env.get("/me")).json()
    assert me["has_subscription"] is True and me["trial_available"] is False


async def test_trial_disabled_by_settings(env: Env) -> None:
    env.ctx.box.update(trial_days=0)
    resp = await env.post("/subscription/trial")
    assert resp.status_code == 409 and resp.json()["error"] == "trial_disabled"


async def test_auto_renew_toggle(env: Env) -> None:
    assert (await env.post("/subscription/auto-renew", {"enabled": False})).status_code == 404
    await start_trial(env)
    resp = await env.post("/subscription/auto-renew", {"enabled": False})
    assert resp.status_code == 200 and resp.json() == {"auto_renew": False}
    assert (await env.get("/subscription")).json()["auto_renew"] is False
    assert (await env.post("/subscription/auto-renew", {"enabled": "maybe"})).status_code == 422


async def test_usage_series(env: Env) -> None:
    await start_trial(env)
    body = (await env.get("/subscription/usage", params={"days": 7})).json()
    assert len(body["days"]) == 7 and body["total_bytes"] == 0
    assert (await env.get("/subscription/usage", params={"days": 400})).status_code == 422


async def test_rotate_link_is_rate_limited(env: Env) -> None:
    connect = (await env.get("/connect")).json()
    seen = {connect["sub_url"]}
    for _ in range(3):
        resp = await env.post("/subscription/rotate-link")
        assert resp.status_code == 200
        seen.add(resp.json()["sub_url"])
    assert len(seen) == 4
    limited = await env.post("/subscription/rotate-link")
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0


# ---------------------------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------------------------
async def test_connect_data(env: Env) -> None:
    env.ctx.box.update(sub_whitelist_separate_url=True)
    await start_trial(env)
    body = (await env.get("/connect")).json()
    async with env.ctx.session_factory() as session:
        user = await session.get(User, USER_ID)
        assert user is not None
        token = user.sub_token
    assert body["state"] == "active"
    assert body["sub_url"] == f"https://bot.example.com/sub/{token}"
    assert body["whitelist_url"] == f"https://bot.example.com/sub/{token}/wl"
    assert body["qr"].startswith("data:image/png;base64,")
    assert body["incy_crypt_link"].startswith("incy://")
    assert body["happ_crypt_link"] is None  # no HTTP client in tests
    happ = next(app for app in body["apps"] if app["key"] == "happ")
    assert happ["open_url"] == f"https://bot.example.com/open/happ/{token}"
    assert happ["deeplink"].startswith("happ://")
    assert "ios" in body["platforms"] and body["platform"] is None

    ios = (await env.get("/connect", params={"platform": "ios"})).json()
    assert ios["platform"] == "ios"
    assert all("ios" in app["platforms"] for app in ios["apps"])
    assert all(d["platform"] == "ios" for app in ios["apps"] for d in app["downloads"])
    assert (await env.get("/connect", params={"platform": "symbian"})).status_code == 422


async def test_connect_needs_public_url(env: Env) -> None:
    env.ctx.box.update(public_base_url=None)
    resp = await env.get("/connect")
    assert resp.status_code == 503 and resp.json()["error"] == "web_unavailable"


# ---------------------------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------------------------
async def test_devices_need_subscription(env: Env) -> None:
    resp = await env.get("/devices")
    assert resp.status_code == 404 and resp.json()["error"] == "no_subscription"


async def test_devices_list_and_reset(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    await start_trial(env)
    created = datetime(2026, 11, 1, tzinfo=UTC)

    async def fake_list(*_: Any) -> list[PanelDevices]:
        return [
            PanelDevices(
                1, "Germany", PanelKind.REMNAWAVE, True, True, [DeviceInfo("hw1", "iOS", "iPhone", created_at=created)]
            ),
            PanelDevices(2, "Finland", PanelKind.XUI, False, False, error="timeout"),
        ]

    calls: list[dict[str, Any]] = []

    async def fake_reset(_f: Any, _r: Any, sub_id: int, **kw: Any) -> ResetResult:
        calls.append(kw)
        if len(calls) == 2:
            raise devices.ResetCooldown(datetime.now(UTC) + timedelta(minutes=10))
        return ResetResult(reset=1)

    monkeypatch.setattr(devices, "list_devices", fake_list)
    monkeypatch.setattr(devices, "reset", fake_reset)

    body = (await env.get("/devices")).json()
    assert body["device_limit"] == 1 and body["next_reset_at"] is None
    first, second = body["servers"]
    assert first["server"] == "Germany" and first["devices"][0]["id"] == "hw1" and not first["unavailable"]
    assert second["unavailable"] is True and second["devices"] == []

    resp = await env.post("/devices/reset", {"panel_id": 1, "device_id": "hw1"})
    assert resp.status_code == 200 and resp.json() == {"reset": 1, "failed": 0}
    assert calls[0]["device_id"] == "hw1" and calls[0]["panel_id"] == 1
    assert calls[0]["cooldown"] == timedelta(minutes=30)

    cooldown = await env.post("/devices/reset", {})
    assert cooldown.status_code == 429 and cooldown.json()["error"] == "devices_cooldown"
    assert "retry_at" in cooldown.json()
    assert (await env.post("/devices/reset", {"device_id": "x"})).status_code == 422


async def test_device_reset_reports_failures(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    await start_trial(env)
    results = [ResetResult(failed=1), ResetResult()]

    async def fake_reset(*_: Any, **__: Any) -> ResetResult:
        return results.pop(0)

    monkeypatch.setattr(devices, "reset", fake_reset)
    failed = await env.post("/devices/reset", {})
    assert failed.status_code == 502 and failed.json()["error"] == "servers_unavailable"
    nothing = await env.post("/devices/reset", {})
    assert nothing.status_code == 404 and nothing.json()["error"] == "no_devices"


async def test_other_users_are_isolated(env: Env) -> None:
    await start_trial(env)
    other = await env.client.get(f"{BASE}/subscription", headers=auth(5555))
    assert other.status_code == 200 and other.json()["exists"] is False


async def test_openapi_lists_every_endpoint(env: Env) -> None:
    paths = {p for p in env.app.openapi()["paths"] if p.startswith(BASE)}
    assert len(paths) == 32
    assert {f"{BASE}/me", f"{BASE}/plans/{{plan_id}}/buy", f"{BASE}/tickets/{{ticket_id}}/reply"} <= paths


async def test_me_exposes_brand_logo(env: Env) -> None:
    body = (await env.get("/me")).json()
    assert body["brand_logo_url"] == ""


def test_brand_logo_url_accepts_only_https() -> None:
    from pydantic import ValidationError

    from app.config import Settings

    base: dict[str, Any] = {"bot_token": "123:abc"}
    assert Settings.model_validate({**base, "brand_logo_url": " https://cdn.example.com/a.png "}).brand_logo_url == (
        "https://cdn.example.com/a.png"
    )
    assert Settings.model_validate(base).brand_logo_url == ""
    for bad in ("http://example.com/a.png", "javascript:alert(1)", "https://", "https://a b/c.png"):
        with pytest.raises(ValidationError):
            Settings.model_validate({**base, "brand_logo_url": bad})
