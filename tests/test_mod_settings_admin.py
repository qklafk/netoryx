"""Settings in the bot: quick toggles, module switches, every registry setting, validation, secrets, permissions."""

from __future__ import annotations

from aiogram.methods import DeleteMessage

from app.core.settings_registry import REGISTRY
from app.db.models import AdminRole
from app.db.session import SessionFactory
from app.modules.admin_settings.admin_bot import SetCb
from tests.test_mod_admin_tools_support import (
    Harness,
    make_harness,
    module,
    release_module_routers,  # noqa: F401 - autouse fixture
)


async def _harness(session_factory: SessionFactory, **settings: object) -> Harness:
    return await make_harness(
        session_factory, [module("admin_settings"), module("broadcasts"), module("tickets")], **settings
    )


def cb(action: str, key: str = "", value: str = "", origin: str = "q") -> str:
    return SetCb(a=action, k=key, v=value, o=origin).pack()


async def test_quick_screen_and_bool_toggle(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(SetCb(a="home").pack())
    assert "Настройки" in h.last()
    await h.click(h.button("Реферальная программа включена"))
    assert "Источник: по умолчанию" in h.last()
    await h.click(h.button("Выключить"))
    assert h.ctx.settings.referral_enabled is False
    assert "Сохранено" in h.last() and "Источник: изменено" in h.last()
    await h.click(h.button("По умолчанию"))
    assert h.ctx.settings.referral_enabled is True


async def test_choice_and_trial_presets(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(cb("spec", "referral_reward_mode"))
    await h.click(h.button("partner"))
    assert h.ctx.settings.referral_reward_mode == "partner"
    await h.click(cb("spec", "trial_days"))
    await h.click(h.button("7 дней"))
    assert h.ctx.settings.trial_days == 7


async def test_text_input_with_validation(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(cb("ask", "broadcast_rate_per_sec", origin="broadcasts"))
    await h.say("100")
    assert "Не подходит" in h.last()
    await h.say("10")
    assert h.ctx.settings.broadcast_rate_per_sec == 10
    await h.click(cb("ask", "expiry_reminder_days"))
    await h.say("5, 2, 1")
    assert h.ctx.settings.expiry_reminder_days == [5, 2, 1]


async def test_secret_is_write_only_and_message_deleted(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(cb("ask", "backup_password", origin="backups"))
    await h.say("hunter22")
    assert h.api.of(DeleteMessage)
    secret = h.ctx.settings.backup_password
    assert secret is not None and secret.get_secret_value() == "hunter22"
    assert "hunter22" not in h.last() and "задано" in h.last()


async def test_admin_cannot_change_owner_only_settings(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(cb("spec", "backup_password", origin="backups"), role=AdminRole.ADMIN)
    assert "Изменять эту настройку может только владелец" in h.last()
    assert "••••••" in h.last()
    await h.click(cb("ask", "backup_password", origin="backups"), role=AdminRole.ADMIN)
    assert "нет прав" in (h.popups()[-1] or "")


async def test_module_switches(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(SetCb(a="mods").pack())
    await h.click(h.button("Рассылки"))
    assert "broadcasts" in h.ctx.settings.disabled_modules
    assert not h.ctx.modules.is_enabled("broadcasts", h.ctx.settings)
    await h.click(h.button("Рассылки"))
    assert "broadcasts" not in h.ctx.settings.disabled_modules


async def test_groups_and_group_pages(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(SetCb(a="groups").pack())
    assert any("Платежи" in text for text, _ in h.buttons())
    await h.click(SetCb(a="group", k="payments_yookassa").pack())
    assert "ЮKassa" in h.last()


def test_callback_data_fits_for_every_setting() -> None:
    for key, spec in REGISTRY.items():
        for action, value in (("set", "c9"), ("ask", ""), ("reset", ""), ("spec", "")):
            assert len(SetCb(a=action, k=key, v=value, o=spec.group).pack().encode()) <= 64


async def test_viewer_has_no_access(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(SetCb(a="home").pack(), role=AdminRole.VIEWER)
    assert h.screens() == []
