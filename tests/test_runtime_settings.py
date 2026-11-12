from __future__ import annotations

import json
from datetime import time
from typing import Any

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.permissions import Permission
from app.core.settings_provider import KV_PREFIX, VERSION_KEY, SettingsProvider, SettingValidationError
from app.core.settings_registry import (
    ENV_ONLY,
    GROUPS,
    REGISTRY,
    SettingSpec,
    by_group,
    get_spec,
    infer_type,
    register,
    undeclared_fields,
)
from app.db.models import AdminAuditLog
from app.db.session import SessionFactory
from app.services import kv
from app.utils.crypto import SecretBox
from tests.conftest import make_settings

BOX = SecretBox("runtime-test-secret")


def provider_for(session_factory: SessionFactory, **env: Any) -> SettingsProvider:
    return SettingsProvider(make_settings(**env), session_factory, BOX)


# ---------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------
def test_every_field_is_declared_or_env_only() -> None:
    assert undeclared_fields() == set()
    assert not (set(REGISTRY) & ENV_ONLY)
    assert set(Settings.model_fields) >= ENV_ONLY


def test_notification_backup_and_consent_settings() -> None:
    s = make_settings(admin_notify_mute="users, tickets", admin_chat_id="-1001", admin_topic_alerts="7")
    assert s.admin_notify_mute == ["users", "tickets"]
    assert s.admin_chat_id == -1001 and s.admin_topic_alerts == 7
    assert s.legal_consent_required is False and s.legal_consent_version == "1"
    assert get_spec("backup_password").secret and get_spec("backup_password").owner_only
    assert get_spec("admin_notify_mute").type == "list[str]"
    assert "backup_dir" in ENV_ONLY  # a filesystem path is never editable from the admin UI
    for bad in (
        {"broadcast_rate_per_sec": 0},
        {"broadcast_rate_per_sec": 31},
        {"backup_keep": 0},
        {"device_reset_cooldown_min": -1},
        {"legal_consent_version": " "},
    ):
        with pytest.raises(ValueError):
            make_settings(**bad)


def test_registry_groups_and_labels() -> None:
    for spec in REGISTRY.values():
        assert spec.group in GROUPS
        assert spec.label_ru and spec.label_en
    grouped = by_group()
    assert sum(len(v) for v in grouped.values()) == len(REGISTRY)
    assert next(iter(grouped)) == "mode"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("cryptobot_token", ("secret", None, True)),
        ("tg_proxy_access", ("choice", ("everyone", "subscribers", "off"), False)),
        ("backup_time", ("time", None, False)),
        ("expiry_reminder_days", ("list[int]", None, False)),
        ("withdrawal_methods", ("list[str]", None, False)),
        ("required_channel_id", ("int", None, True)),
        ("stars_rub_per_star", ("float", None, False)),
        ("gifts_enabled", ("bool", None, False)),
        ("brand_name", ("str", None, False)),
        ("default_plans", ("json", None, False)),
    ],
)
def test_type_inference(key: str, expected: tuple[Any, ...]) -> None:
    assert infer_type(key) == expected


def test_secret_and_owner_only_need_secret_permission() -> None:
    assert get_spec("cryptobot_token").secret
    assert get_spec("cryptobot_token").permission is Permission.SETTINGS_SECRETS
    assert get_spec("app_mode").permission is Permission.SETTINGS_SECRETS
    assert get_spec("brand_name").permission is Permission.SETTINGS
    assert get_spec("brand_name").label("en") == "Service name"
    with pytest.raises(KeyError):
        get_spec("bot_token")


def test_register_module_settings() -> None:
    spec = REGISTRY["brand_name"]
    register([spec])  # identical re-declaration is fine
    with pytest.raises(ValueError, match="environment-only"):
        register([SettingSpec("database_url", "system", "str", "a", "b")])
    with pytest.raises(KeyError):
        register([SettingSpec("no_such_field", "system", "str", "a", "b")])
    with pytest.raises(ValueError, match="already declared"):
        register([SettingSpec("brand_name", "system", "str", "x", "y")])


# ---------------------------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------------------------
async def test_load_without_overrides_returns_env(session_factory: SessionFactory) -> None:
    provider = provider_for(session_factory, brand_name="Env")
    settings = await provider.load()
    assert settings.brand_name == "Env"
    assert provider.effective("brand_name") == ("Env", "env")
    assert provider.effective("trial_days") == (3, "default")
    with pytest.raises(KeyError):
        provider.effective("nope")


async def test_set_persists_audits_and_reloads(session_factory: SessionFactory) -> None:
    provider = provider_for(session_factory)
    await provider.load()
    new = await provider.set("trial_days", "5", actor_id=1000, source="web", ip="1.2.3.4")
    assert new.trial_days == 5
    assert provider.current().trial_days == 5
    assert provider.effective("trial_days") == (5, "db")
    async with session_factory() as s:
        assert await kv.get(s, KV_PREFIX + "trial_days") == "5"
        assert await kv.get(s, VERSION_KEY) == provider.version
        entry = await s.scalar(select(AdminAuditLog))
    assert entry is not None and entry.action == "settings.set" and entry.target_id == "trial_days"
    assert entry.source == "web" and entry.ip == "1.2.3.4"
    assert json.loads(entry.details or "{}") == {"value": 5, "previous": 3}

    other = provider_for(session_factory)
    assert (await other.load()).trial_days == 5


async def test_typed_inputs_are_normalized(session_factory: SessionFactory) -> None:
    provider = provider_for(session_factory)
    await provider.load()
    assert (await provider.set("expiry_reminder_days", "5, 2", actor_id=1)).expiry_reminder_days == [5, 2]
    assert (await provider.set("backup_time", "05:30", actor_id=1)).backup_time == time(5, 30)
    assert (await provider.set("withdrawal_methods", ["card"], actor_id=1)).withdrawal_methods == ["card"]
    assert (await provider.set("gifts_enabled", "false", actor_id=1)).gifts_enabled is False
    assert (await provider.set("support_url", "https://t.me/help", actor_id=1)).support_url == "https://t.me/help"
    assert (await provider.set("support_url", "", actor_id=1)).support_url is None
    async with session_factory() as s:
        assert await kv.get(s, KV_PREFIX + "backup_time") == '"05:30:00"'
        assert await kv.get(s, KV_PREFIX + "support_url") == "null"
    reloaded = await provider_for(session_factory).load()
    assert reloaded.backup_time == time(5, 30)
    assert reloaded.expiry_reminder_days == [5, 2]


async def test_secret_is_encrypted_and_redacted(session_factory: SessionFactory) -> None:
    provider = provider_for(session_factory)
    await provider.load()
    await provider.set("cryptobot_token", "123:abc-secret", actor_id=1)
    settings = await provider.set("cryptobot_enabled", True, actor_id=1)
    assert settings.cryptobot_enabled
    assert settings.cryptobot_token is not None
    assert settings.cryptobot_token.get_secret_value() == "123:abc-secret"
    async with session_factory() as s:
        raw = await kv.get(s, KV_PREFIX + "cryptobot_token")
        details = [e.details for e in await s.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id))]
    assert raw is not None and "abc-secret" not in raw and json.loads(raw).startswith("enc:")
    assert all("abc-secret" not in (d or "") for d in details)
    reloaded = await provider_for(session_factory).load()
    assert reloaded.cryptobot_token is not None
    assert reloaded.cryptobot_token.get_secret_value() == "123:abc-secret"


async def test_validation_errors(session_factory: SessionFactory) -> None:
    provider = provider_for(session_factory)
    await provider.load()
    with pytest.raises(SettingValidationError) as info:
        await provider.set("trial_days", "many", actor_id=1)
    assert info.value.key == "trial_days"
    with pytest.raises(SettingValidationError, match="at least"):
        await provider.set("trial_days", -1, actor_id=1)
    with pytest.raises(SettingValidationError, match="at most"):
        await provider.set("sub_announce", "x" * 201, actor_id=1)
    with pytest.raises(SettingValidationError, match="MIN_TOPUP"):
        await provider.set("min_topup_rub", 999_999, actor_id=1)
    with pytest.raises(SettingValidationError, match="CRYPTOBOT_TOKEN"):
        await provider.set("cryptobot_enabled", True, actor_id=1)
    with pytest.raises(SettingValidationError):
        await provider.set("tg_proxy_access", "friends", actor_id=1)
    with pytest.raises(KeyError):
        await provider.set("database_url", "sqlite://", actor_id=1)
    assert provider.overridden_keys() == set()
    async with session_factory() as s:
        assert await s.scalar(select(AdminAuditLog)) is None


async def test_reset_returns_to_env(session_factory: SessionFactory) -> None:
    provider = provider_for(session_factory, brand_name="Env")
    await provider.load()
    await provider.set("brand_name", "Db", actor_id=1)
    settings = await provider.reset("brand_name", actor_id=1)
    assert settings.brand_name == "Env"
    assert provider.effective("brand_name") == ("Env", "env")
    async with session_factory() as s:
        assert await kv.get(s, KV_PREFIX + "brand_name") is None
    assert (await provider_for(session_factory, brand_name="Env").load()).brand_name == "Env"


async def test_subscribers_and_poll_changes(session_factory: SessionFactory) -> None:
    first = provider_for(session_factory)
    second = provider_for(session_factory)
    await first.load()
    await second.load()
    seen: list[str] = []

    async def on_change(settings: Settings) -> None:
        seen.append(settings.brand_name)

    async def broken(settings: Settings) -> None:
        raise RuntimeError("boom")

    unsubscribe = second.subscribe(on_change)
    second.subscribe(broken)
    assert await second.poll_changes() is False
    await first.set("brand_name", "Changed", actor_id=1)
    assert await second.poll_changes() is True
    assert second.current().brand_name == "Changed"
    assert seen == ["Changed"]
    assert await second.poll_changes() is False

    unsubscribe()
    await first.set("brand_name", "Again", actor_id=1)
    await second.poll_changes()
    assert seen == ["Changed"]


async def test_set_same_value_does_not_notify(session_factory: SessionFactory) -> None:
    provider = provider_for(session_factory)
    await provider.load()
    calls: list[Settings] = []

    async def on_change(settings: Settings) -> None:
        calls.append(settings)

    provider.subscribe(on_change)
    await provider.set("trial_days", 3, actor_id=1)
    assert calls == []
    await provider.set("trial_days", 4, actor_id=1)
    assert len(calls) == 1


async def test_bad_stored_values_are_skipped(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        await kv.set(s, KV_PREFIX + "trial_days", "not json")
        await kv.set(s, KV_PREFIX + "bot_token", '"123:XYZ"')  # environment-only: never applied
        await kv.set(s, KV_PREFIX + "removed_setting", "1")
        await kv.set(s, KV_PREFIX + "cryptobot_enabled", "true")  # needs a token: inconsistent
        await kv.set(s, KV_PREFIX + "platega_secret", json.dumps(SecretBox("other-key").encrypt("x")))
        await kv.set(s, KV_PREFIX + "brand_name", '"Kept"')
    provider = provider_for(session_factory)
    settings = await provider.load()
    assert settings.trial_days == 3
    assert settings.bot_token.get_secret_value() == "123456:TEST"
    assert settings.cryptobot_enabled is False
    assert settings.platega_secret is None
    assert settings.brand_name == "Kept"
    assert provider.overridden_keys() == {"brand_name"}


async def test_ephemeral_override(session_factory: SessionFactory) -> None:
    provider = provider_for(session_factory, public_base_url=None)
    await provider.load()
    settings = provider.set_ephemeral("public_base_url", "https://abc.trycloudflare.com/")
    assert settings.public_base_url == "https://abc.trycloudflare.com"
    assert provider.effective("public_base_url")[1] == "env"
    await provider.set("brand_name", "X", actor_id=1)
    assert provider.current().public_base_url == "https://abc.trycloudflare.com"
    with pytest.raises(KeyError):
        provider.set_ephemeral("nope", 1)
