from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.core.locks import LockManager
from app.core.plugins import CoreRuntime
from app.core.settings_provider import KV_PREFIX, SettingsProvider
from app.db.models import AdminAuditLog, AdminRole, Panel, PanelKind, Subscription, TextOverride, User
from app.modules.admin_api import version as version_module
from app.modules.admin_api.deps import drain_background
from app.modules.admin_api.routers import backups as backups_router
from app.services import kv
from app.utils.crypto import SecretBox
from app.web.app import create_web_app
from tests.test_mod_webapi_support import OWNER, Api, add_plan, add_user, api, as_role, build_ctx, ctx

__all__ = ["api", "ctx"]

KEY = "admin_api.notify.balance_credited"


# ---------------------------------------------------------------------------------------------
# Texts
# ---------------------------------------------------------------------------------------------
async def test_texts_edit_validate_reset(api: Api) -> None:
    ctx = api.ctx
    modules = {m["module"]: m for m in (await api.get("/texts/modules")).json()}
    assert modules["admin_api"]["keys"] >= 5 and modules["admin_api"]["overridden"] == 0
    listed = (await api.get("/texts", params={"module": "admin_api", "q": "credited"})).json()
    assert [t["key"] for t in listed["items"]] == [KEY]
    item = (await api.get(f"/texts/{KEY}")).json()
    assert item["placeholders"] == ["amount"] and "{amount}" in item["default"]["ru"]

    bad = await api.put(f"/texts/{KEY}/ru", json={"value": "Пополнено на {sum}"})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_text"
    saved = (await api.put(f"/texts/{KEY}/ru", json={"value": "💰 +{amount} на балансе"})).json()
    assert saved["override"]["ru"] == "💰 +{amount} на балансе" and saved["effective"]["ru"] == saved["override"]["ru"]
    assert ctx.i18n.t(KEY, "ru", amount="5 ₽") == "💰 +5 ₽ на балансе"
    only = (await api.get("/texts", params={"overridden": True})).json()
    assert [t["key"] for t in only["items"]] == [KEY]

    preview = (await api.post(f"/texts/{KEY}/ru/preview", json={"values": {"amount": "100 ₽"}})).json()
    assert preview == {"ok": True, "rendered": "💰 +100 ₽ на балансе", "error": None, "placeholders": ["amount"]}
    broken = (await api.post(f"/texts/{KEY}/ru/preview", json={"value": "<b>open"})).json()
    assert broken["ok"] is False and broken["error"]

    reset = (await api.delete(f"/texts/{KEY}/ru")).json()
    assert reset["override"]["ru"] is None
    async with ctx.session_factory() as s:
        assert await s.scalar(select(TextOverride)) is None
    assert (await api.get("/texts/unknown.key")).status_code == 404
    assert (await api.put("/texts/unknown.key/ru", json={"value": "x"})).status_code == 404
    support = await as_role(api, AdminRole.SUPPORT)
    assert (await support.get("/texts")).status_code == 403


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------
async def test_settings_registry_driven(api: Api) -> None:
    ctx = api.ctx
    groups = {g["key"]: g for g in (await api.get("/settings", params={"lang": "en"})).json()}
    trial = {s["key"]: s for s in groups["trial"]["settings"]}["trial_days"]
    assert trial["origin"] in ("default", "env") and trial["editable"] is True and trial["max"] == 365
    secret = {s["key"]: s for s in groups["payments_cryptobot"]["settings"]}["cryptobot_token"]
    assert secret["secret"] is True and secret["value"] is None and secret["is_set"] is False

    changed = (await api.put("/settings/trial_days", json={"value": 5})).json()
    assert changed["value"] == 5 and changed["origin"] == "db" and ctx.settings.trial_days == 5
    too_big = await api.put("/settings/trial_days", json={"value": 1000})
    assert too_big.status_code == 400 and too_big.json()["error"] == "invalid_value"
    assert (await api.put("/settings/bot_token", json={"value": "x"})).status_code == 404
    reset = (await api.delete("/settings/trial_days")).json()
    assert reset["origin"] != "db"

    stored = (await api.put("/settings/cryptobot_token", json={"value": "123:SECRET"})).json()
    assert stored["value"] is None and stored["is_set"] is True
    async with ctx.session_factory() as s:
        entry = await s.scalar(select(AdminAuditLog).where(AdminAuditLog.target_id == "cryptobot_token"))
    assert entry is not None and "SECRET" not in (entry.details or "") and entry.ip == "203.0.113.5"

    admin = await as_role(api, AdminRole.ADMIN)
    assert (await admin.put("/settings/cryptobot_token", json={"value": "y"})).status_code == 403
    assert (await admin.put("/settings/trial_days", json={"value": 2})).status_code == 200
    env = {e["key"]: e for e in (await api.get("/settings/env")).json()}
    assert env["bot_token"]["hidden"] is True and env["bot_token"]["value"] is None
    assert env["public_base_url"]["value"] == "https://bot.example.com"


# ---------------------------------------------------------------------------------------------
# Admins & sessions
# ---------------------------------------------------------------------------------------------
async def test_admins_and_sessions(api: Api) -> None:
    await add_user(api.ctx, 3000, username="helper")
    added = await api.post("/admins", json={"tg_id": 3000, "role": "support", "note": "night shift"})
    assert added.status_code == 201
    entries = {a["tg_id"]: a for a in added.json()}
    assert entries[OWNER]["from_env"] is True and entries[3000]["username"] == "helper"
    assert (await api.post("/admins", json={"tg_id": 3000, "role": "viewer"})).status_code == 409
    assert (await api.post("/admins", json={"tg_id": OWNER, "role": "viewer"})).status_code == 409
    updated = {a["tg_id"]: a for a in (await api.patch("/admins/3000", json={"role": "admin"})).json()}
    assert updated[3000]["role"] == "admin"
    roles = {r["role"]: r for r in (await api.get("/admins/roles")).json()}
    assert "admins" in roles["owner"]["permissions"] and "admins" not in roles["admin"]["permissions"]

    helper = await Api(api.ctx, api.client).login(3000)
    own = (await helper.get("/sessions")).json()
    assert len(own) == 1 and own[0]["current"] is True
    assert (await helper.get("/sessions", params={"all_admins": True})).status_code == 403
    every = (await api.get("/sessions", params={"all_admins": True})).json()
    assert {s["admin_id"] for s in every} == {OWNER, 3000}
    await Api(api.ctx, api.client).login(OWNER)  # a second owner session
    assert (await api.post("/sessions/revoke-others")).json() == {"count": 1}

    assert (await api.delete("/admins/3000")).json() == {"ok": True}
    assert (await helper.get("/sessions")).status_code == 401
    assert (await api.delete("/admins/3000")).status_code == 404
    assert (await api.delete(f"/admins/{OWNER}")).status_code == 409


# ---------------------------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------------------------
async def test_backups_create_download_upload_restore(api: Api) -> None:
    ctx = api.ctx
    await add_user(ctx, 50, username="keepme")
    created = await api.post("/backups")
    assert created.status_code == 201, created.text
    name = created.json()["name"]
    assert created.json()["kind"] == "sqlite"
    listing = (await api.get("/backups")).json()
    assert [b["name"] for b in listing["items"]] == [name] and listing["scheduled"] is False

    admin = await as_role(api, AdminRole.ADMIN)
    assert (await admin.get(f"/backups/{name}")).status_code == 403
    assert (await admin.post("/backups/restore", json={"name": name, "confirm": "RESTORE"})).status_code == 403
    download = await api.get(f"/backups/{name}")
    assert download.status_code == 200 and len(download.content) == created.json()["size"]
    assert (await api.get("/backups/..%2Fapi.db")).status_code in (400, 404)
    assert (await api.get("/backups/backup-x.sqlite.gz")).status_code == 404

    uploaded = await api.post(
        "/backups/upload",
        params={"filename": "old.sqlite.gz"},
        content=download.content,
        headers={"content-type": "application/octet-stream"},
    )
    assert uploaded.status_code == 201
    assert (await api.post("/backups/upload", params={"filename": "x.zip"}, content=b"1")).status_code == 400

    async with ctx.session_factory.begin() as s:
        user = await s.get(User, 50)
        assert user is not None
        user.username = "changed"
    no_confirm = await api.post("/backups/restore", json={"name": name})
    assert no_confirm.status_code == 422
    restored = (await api.post("/backups/restore", json={"name": uploaded.json()["name"], "confirm": "RESTORE"})).json()
    assert restored["wiped"] is True and restored["rows"] > 0
    async with ctx.session_factory() as s:
        user = await s.get(User, 50)
        audit = await s.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "backup.restore"))
    assert user is not None and user.username == "keepme" and audit is not None
    await api.login()  # sessions came back from the backup
    assert (await api.delete(f"/backups/{name}")).json() == {"ok": True}
    assert (await api.delete(f"/backups/{name}")).status_code == 404


# ---------------------------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------------------------
async def test_plugin_catalog_is_read_only_and_rbac_protected(api: Api) -> None:
    unavailable = await api.get("/system/plugin-catalog")
    assert unavailable.status_code == 200
    unavailable_body = unavailable.json()
    assert unavailable_body["core_available"] is False and unavailable_body["plugins"] == []
    assert unavailable_body["legacy"][0]["kind"] == "legacy"
    assert unavailable_body["legacy"][0]["runtime_status"].startswith("legacy-")

    core = CoreRuntime(Path(api.ctx.settings.backup_dir).parent / "core-state.db")
    api.ctx.plugin_core = core
    try:
        response = await api.get("/system/plugin-catalog")
        assert response.status_code == 200
        body = response.json()
        backups = next(item for item in body["plugins"] if item["id"] == "infrastructure.backups")
        assert body["core_available"] is True
        assert backups["partially_migrated"] is True
        assert backups["runtime_status"] == "legacy-owned"
        assert backups["migration_status"] == "partial"
        assert {"id", "title", "description", "version", "compatibility"} <= backups.keys()
        assert not {"token", "secret", "private_key", "password"} & backups.keys()
        assert backups["actions_available"] == []
    finally:
        core.close()

    anonymous = Api(api.ctx, api.client)
    assert (await anonymous.get("/system/plugin-catalog")).status_code == 401
    support = await as_role(api, AdminRole.SUPPORT)
    assert (await support.get("/system/plugin-catalog")).status_code == 403


async def test_audit_jobs_logs_webhook(api: Api) -> None:
    await add_user(api.ctx, 60, sub_days=1)
    await api.post("/users/60/rotate-link")
    page = (await api.get("/system/audit", params={"limit": 1})).json()
    assert len(page["items"]) == 1 and page["next_before_id"] is not None
    filtered = (await api.get("/system/audit", params={"action": "user.", "source": "web"})).json()
    assert [i["action"] for i in filtered["items"]] == ["user.rotate_link"]

    jobs = (await api.get("/system/jobs")).json()
    assert jobs["available"] is True and {j["id"] for j in jobs["jobs"]} >= {"provisioning.reconcile"}
    assert (await api.post("/system/jobs/payments.reconcile/run")).json() == {"started": True}
    assert (await api.post("/system/jobs/nope/run")).status_code == 404
    api.ctx.scheduler = None
    assert (await api.get("/system/jobs")).json() == {"available": False, "jobs": []}

    import logging

    logging.getLogger("app.test").addHandler(api.ctx.log_buffer)
    logging.getLogger("app.test").warning("hello from the test")
    lines = (await api.get("/system/logs", params={"lines": 5})).json()
    assert lines["available"] is True and any("hello from the test" in line for line in lines["lines"])
    hook = (await api.get("/system/webhook")).json()
    assert hook["mode"] == "polling" and hook["pending_update_count"] == 0
    support = await as_role(api, AdminRole.SUPPORT)
    assert (await support.get("/system/audit")).status_code == 403


async def test_version_and_update_check(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    plain = (await api.get("/system/version", params={"check": False})).json()
    assert plain["version"] and plain["mode"] == "shop" and plain["database"] == "sqlite"

    async def fake_fetch(http: object, repo: str) -> version_module.Release:
        return version_module.Release("v99.0.0", "https://example.com/release", datetime.now(UTC))

    monkeypatch.setattr(version_module, "_fetch", fake_fetch)
    version_module.clear_cache()
    release = await version_module.latest_release(None, "owner/repo")
    assert release is not None and release.version == "v99.0.0"
    assert version_module.is_newer("v1.10.0", "1.9.9") and not version_module.is_newer("1.0.0", "1.0.0")
    assert await version_module.latest_release(None, "not a repo") is None


async def test_maintenance_controls(api: Api) -> None:
    state = (await api.get("/system/maintenance")).json()
    assert state["active"] is False
    eta = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    on = (await api.post("/system/maintenance/enable", json={"message_ru": "Обновляемся", "eta": eta})).json()
    assert on["active"] is True and on["message_ru"] == "Обновляемся" and on["started_by"] == OWNER
    allow = (await api.put("/system/maintenance/allowlist", json={"user_ids": [5, 5, 6]})).json()
    assert allow["allowlist"] == [5, 6]
    assert (await api.put("/system/maintenance/modules", json={"modules": ["nope"]})).status_code == 400
    partial = (await api.put("/system/maintenance/modules", json={"modules": ["admin_api"]})).json()
    assert partial["disabled_modules"] == ["admin_api"]
    off = (await api.post("/system/maintenance/disable")).json()
    assert off["enabled"] is False
    start = datetime.now(UTC) + timedelta(days=1)
    window = {"starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=1)).isoformat()}
    planned = await api.post("/system/maintenance/windows", json=window)
    assert planned.status_code == 201
    window_id = planned.json()["windows"][0]["id"]
    wrong = {"starts_at": window["ends_at"], "ends_at": window["starts_at"]}
    assert (await api.post("/system/maintenance/windows", json=wrong)).status_code == 400
    assert (await api.delete(f"/system/maintenance/windows/{window_id}")).json()["windows"] == []
    assert (await api.delete(f"/system/maintenance/windows/{window_id}")).status_code == 404


async def test_modules_list_and_toggle(api: Api) -> None:
    modules = {m["name"]: m for m in (await api.get("/system/modules")).json()}
    assert modules["admin_api"]["enabled"] is True and modules["admin_api"]["required"] is False
    admin = await as_role(api, AdminRole.ADMIN)
    assert (await admin.put("/system/modules/admin_api", json={"enabled": True})).status_code == 403
    assert (await api.put("/system/modules/unknown", json={"enabled": False})).status_code == 404
    on = {m["name"]: m for m in (await api.put("/system/modules/admin_api", json={"enabled": True})).json()}
    assert on["admin_api"]["switched_off"] is False


# ---------------------------------------------------------------------------------------------
# Mode switch
# ---------------------------------------------------------------------------------------------
async def test_mode_switch_and_rollback(api: Api) -> None:
    ctx = api.ctx
    readiness = (await api.get("/mode/readiness")).json()
    assert readiness["current_mode"] == "shop" and readiness["ready"] is False and "plans" in readiness["missing"]
    assert (await api.post("/mode/preview", json={"target": "shop"})).status_code == 409

    await add_plan(ctx)
    await add_user(ctx, 70, sub_days=10)
    preview = (await api.post("/mode/preview", json={"target": "personal"})).json()
    assert preview["settings_changes"]["app_mode"] == "personal" and preview["auto_renew_off"] == 1
    assert (await api.post("/mode/apply", json={"target": "personal"})).status_code == 422
    admin = await as_role(api, AdminRole.ADMIN)
    assert (await admin.post("/mode/apply", json={"target": "personal", "confirm": "SWITCH"})).status_code == 403

    applied = (await api.post("/mode/apply", json={"target": "personal", "confirm": "SWITCH"})).json()
    assert applied["target"] == "personal" and applied["auto_renew_disabled"] == 1 and applied["settings_failed"] == []
    assert ctx.settings.app_mode == "personal"
    async with ctx.session_factory() as s:
        assert (await s.scalar(select(Subscription.auto_renew).where(Subscription.user_id == 70))) is False
    last = (await api.get("/mode/last")).json()
    assert last["available"] is True and last["target"] == "personal" and last["subscriptions"] == 1

    shop = (await api.post("/mode/preview", json={"target": "shop"})).json()
    assert shop["readiness"]["ready"] is False
    refused = await api.post("/mode/apply", json={"target": "shop", "confirm": "SWITCH"})
    assert refused.status_code == 409 and refused.json()["error"] == "not_ready"

    assert (await api.post("/mode/rollback")).json() == {
        "available": False,
        "target": None,
        "at": None,
        "actor_id": None,
        "settings": {},
        "subscriptions": 0,
    }
    assert ctx.settings.app_mode == "shop"
    async with ctx.session_factory() as s:
        assert (await s.scalar(select(Subscription.auto_renew).where(Subscription.user_id == 70))) is True
    assert (await api.post("/mode/rollback")).status_code == 409


async def test_restore_takes_safety_backup_first(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = api.ctx
    await add_user(ctx, 51, username="before")
    name = (await api.post("/backups")).json()["name"]
    async with ctx.session_factory.begin() as s:
        user = await s.get(User, 51)
        assert user is not None
        user.username = "after"

    async def broken(*_: Any, **__: Any) -> Any:
        raise OSError("disk full")

    real_create = backups_router.backups.create
    monkeypatch.setattr(backups_router.backups, "create", broken)
    failed = await api.post("/backups/restore", json={"name": name, "confirm": "RESTORE"})
    assert failed.status_code == 409 and failed.json()["error"] == "safety_backup_failed"
    async with ctx.session_factory() as s:
        user = await s.get(User, 51)
        assert user is not None and user.username == "after"  # nothing was replaced

    monkeypatch.setattr(backups_router.backups, "create", real_create)
    restored = (await api.post("/backups/restore", json={"name": name, "confirm": "RESTORE"})).json()
    safety = restored["safety_backup"]
    assert safety and safety != name and restored["restart_recommended"] is False
    await api.login()
    assert safety in [b["name"] for b in (await api.get("/backups")).json()["items"]]
    # The safety copy holds the data as it was right before the restore.
    back = (await api.post("/backups/restore", json={"name": safety, "confirm": "RESTORE"})).json()
    assert back["wiped"] is True
    async with ctx.session_factory() as s:
        user = await s.get(User, 51)
        assert user is not None and user.username == "after"


async def test_restore_is_refused_while_another_process_restores(api: Api) -> None:
    ctx = api.ctx
    name = (await api.post("/backups")).json()["name"]
    ctx.locks = LockManager(ctx.session_factory, owner="web")  # type: ignore[attr-defined]
    other = LockManager(ctx.session_factory, owner="bot")
    assert await other.acquire(backups_router.RESTORE_LOCK, 600)
    busy = await api.post("/backups/restore", json={"name": name, "confirm": "RESTORE"})
    assert busy.status_code == 409 and busy.json()["error"] == "busy"
    await other.release(backups_router.RESTORE_LOCK)
    assert (await api.post("/backups/restore", json={"name": name, "confirm": "RESTORE"})).status_code == 200


@pytest.fixture
async def keyless_api(tmp_path: Path) -> AsyncIterator[Api]:
    """A web admin without SECRET_KEY: the key lives in the database, like on a default install."""
    context, engine = await build_ctx(tmp_path, secret_key=None)
    await kv.get_or_create_secret(context.session_factory)
    context.secret_box = SecretBox(await kv.get_or_create_secret(context.session_factory))
    context.settings_provider = SettingsProvider(
        context.settings_provider.env, context.session_factory, context.secret_box
    )
    await context.settings_provider.load()
    app = create_web_app(context)  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app, client=("203.0.113.5", 50000))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield await Api(context, client).login()
    finally:
        await drain_background()
        await engine.dispose()


async def test_restore_from_another_install_switches_to_its_secret_key(keyless_api: Api) -> None:
    api = keyless_api
    ctx = api.ctx
    running_key = await kv.get_or_create_secret(ctx.session_factory)
    other = SecretBox("key-of-the-old-server")
    # Make the backup look like it came from another install: its own key, values encrypted with it.
    async with ctx.session_factory.begin() as s:
        await kv.set(s, kv.SECRET_KEY, "key-of-the-old-server")
        await kv.set(s, KV_PREFIX + "yookassa_secret_key", json.dumps(other.encrypt("shop-secret")))
        s.add(Panel(name="old", kind=PanelKind.XUI, url="https://old.example.com", password=other.encrypt("pw")))
    name = (await api.post("/backups")).json()["name"]
    async with ctx.session_factory.begin() as s:
        await kv.set(s, kv.SECRET_KEY, running_key)
        await kv.delete(s, KV_PREFIX + "yookassa_secret_key")

    restored = await api.post("/backups/restore", json={"name": name, "confirm": "RESTORE"})
    assert restored.status_code == 200, restored.text
    assert restored.json()["restart_recommended"] is True
    async with ctx.session_factory() as s:
        panel = await s.scalar(select(Panel))
    assert panel is not None and panel.password is not None
    assert ctx.secret_box.decrypt(panel.password) == "pw"
    secret = ctx.settings.yookassa_secret_key
    assert secret is not None and secret.get_secret_value() == "shop-secret"
