"""Runtime settings in the bot: quick toggles and every registered setting with validation."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.modules.admin_settings.admin_bot import SetCb
from app.modules.admin_settings.admin_bot import router as admin_router

module = FeatureModule(
    name="admin_settings",
    title_ru="Настройки",
    title_en="Settings",
    required=True,
    admin_bot_routers=[admin_router],
    admin_menu=[
        AdminMenuItem(
            key="settings",
            title_key="admin_settings.title",
            icon="⚙️",
            permission=Permission.SETTINGS,
            web_path="/settings",
            bot_callback=SetCb(a="home").pack(),
            order=850,
        )
    ],
    order=905,
)
