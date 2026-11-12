"""Bot admin menu, web admin login link, leaving inputs and the dashboard."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Router
from aiogram.methods import SendMessage

from app.core.modules import AdminMenuItem, FeatureModule, load_modules
from app.core.permissions import Permission
from app.db.models import AdminRole, Payment, PaymentStatus, Plan
from app.db.session import SessionFactory
from app.modules.admin.common import parse_amount_kop, parse_int
from app.web.auth import redeem_login_token
from tests.test_middlewares_support import message_update
from tests.test_mod_admin_support import ADMIN_MODULES, OWNER, USER, Builder, add_admin, add_subscription, add_user


@pytest.fixture
def make(session_factory: SessionFactory, tmp_path: Path) -> Iterator[Builder]:
    builder = Builder(session_factory, tmp_path)
    yield builder
    builder.close()


async def test_admin_menu_lists_sections_for_owner(make: Any) -> None:
    h = make()
    await h.message("/admin")
    text = h.last_text()
    assert "Админ-панель" in text and "владелец" in text
    labels = list(h.buttons())
    for title in ("📊 Дашборд", "👥 Пользователи", "🌐 Веб-админка", "🏠 Главное меню"):
        assert title in labels


async def test_admin_menu_is_role_aware(make: Any, session_factory: SessionFactory) -> None:
    h = make()
    await add_admin(session_factory, 2002, AdminRole.VIEWER)
    await h.message("/admin", uid=2002)
    labels = list(h.buttons())
    assert "📊 Дашборд" in labels and "👥 Пользователи" in labels
    assert not any("Тарифы" in label or "Промокоды" in label for label in labels)


async def test_admin_commands_ignore_regular_users(make: Any) -> None:
    h = make()
    await h.message("/admin", uid=USER)
    await h.message("/web", uid=USER)
    assert not h.calls(SendMessage)


async def test_admin_menu_hides_disabled_and_personal_mode_sections(make: Any) -> None:
    h = make(app_mode="personal")
    await h.message("/admin")
    labels = " ".join(h.buttons())
    assert "Пользователи" in labels
    assert "Тарифы" not in labels and "Платежи" not in labels and "Промокоды" not in labels


async def test_menu_shows_items_of_other_modules(make: Builder) -> None:
    extra = FeatureModule(
        name="custom",
        title_ru="x",
        title_en="x",
        admin_bot_routers=[Router()],
        admin_menu=[
            AdminMenuItem("c", "nav.refresh", "🧩", Permission.SETTINGS, bot_callback="nav:custom:"),
            AdminMenuItem("w", "nav.close", "🌍", Permission.SETTINGS, web_path="/only-web"),
        ],
    )
    h = make(modules=[*load_modules(ADMIN_MODULES), extra])
    await h.message("/admin")
    buttons = h.buttons()
    assert buttons["🧩 🔄 Обновить"] == "nav:custom:"
    assert not any(label.startswith("🌍") for label in buttons)


async def test_nav_admin_reopens_menu(make: Any) -> None:
    h = make()
    await h.press("nav:admin:")
    assert "Админ-панель" in h.last_text()


async def test_web_command_issues_single_use_login_link(make: Any, session_factory: SessionFactory) -> None:
    h = make()
    await h.message("/web")
    url = h.buttons()["🔐 Войти в веб-админку"]
    assert url.startswith("https://bot.example.com/admin/login#token=")
    token = url.split("token=", 1)[1]
    async with session_factory.begin() as s:
        assert await redeem_login_token(s, token) == OWNER
    async with session_factory.begin() as s:
        assert await redeem_login_token(s, token) is None
    assert "одноразовая" in h.last_text()


async def test_web_link_respects_switch_and_relative_url(make: Any) -> None:
    h = make(web_admin_enabled=False)
    await h.message("/web")
    assert "выключена" in h.last_text()
    h2 = make(public_base_url=None)
    await h2.message("/web")
    assert "<code>/admin/login#token=" in h2.last_text()


async def test_web_link_is_not_sent_in_groups(make: Any) -> None:
    h = make()
    await h.feed(message_update(OWNER, "/web", chat_id=-100500, chat_type="supergroup"))
    assert not h.calls(SendMessage)


async def test_button_press_leaves_an_input(make: Any, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await h.press(f"admu:msg:{USER}:")
    await h.press("nav:admin:")  # leaving the screen abandons "send the message"
    h.reset()
    await h.message("hello")
    assert not h.calls(SendMessage)  # not treated as the message for the user


async def test_command_leaves_an_input(make: Any, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await h.press(f"admu:msg:{USER}:")
    await h.message("/admin")
    assert "Админ-панель" in h.last_text()
    h.reset()
    await h.message("hello")
    assert not h.calls(SendMessage)


# --- dashboard -------------------------------------------------------------------------------------------------------


async def test_dashboard_shows_kpis(make: Any, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await add_subscription(session_factory)
    async with session_factory.begin() as s:
        s.add(Plan(title="M", duration_days=30, price_kop=10000))
        s.add(
            Payment(
                id="p1",
                provider="stars",
                user_id=USER,
                amount_kop=15000,
                status=PaymentStatus.SUCCEEDED,
                credited_at=datetime.now(UTC),
            )
        )
        s.add(Payment(id="p2", provider="stars", user_id=USER, amount_kop=100, status=PaymentStatus.PENDING))
    await h.press("nav:adm_dash:")
    text = h.last_text()
    assert "Дашборд" in text
    assert "150 ₽" in text  # revenue
    assert "Ожидают оплаты: 1" in text
    assert "stars" in text
    await h.press("nav:adm_dash_days:")
    assert "Последние 7 дней" in h.last_text()


async def test_dashboard_in_personal_mode_has_no_money(make: Any) -> None:
    h = make(app_mode="personal")
    await h.press("nav:adm_dash:")
    assert "Выручка" not in h.last_text()


# --- parsing ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "signed", "expected"),
    [
        ("150", False, 15000),
        ("-99,50", True, -9950),
        ("1 000.5", False, 100050),
        ("-5", False, None),
        ("0", True, None),
    ],
)
def test_parse_amount(text: str, signed: bool, expected: int | None) -> None:
    assert parse_amount_kop(text, signed=signed) == expected


def test_parse_int_bounds() -> None:
    assert parse_int("12") == 12
    assert parse_int("-3") is None
    assert parse_int("-3", signed=True) == -3
    assert parse_int("101", maximum=100) is None
    assert parse_int("abc") is None
