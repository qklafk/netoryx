"""Maintenance controls for admins: full / partial shutdown with a message and ETA, testers, planned windows."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, BotCommandSpec, FeatureModule
from app.core.permissions import Permission
from app.modules.admin_maintenance.admin_bot import MntCb
from app.modules.admin_maintenance.admin_bot import router as admin_router

module = FeatureModule(
    name="admin_maintenance",
    title_ru="Техработы",
    title_en="Maintenance",
    required=True,
    admin_bot_routers=[admin_router],
    admin_menu=[
        AdminMenuItem(
            key="maintenance",
            title_key="admin_maintenance.title",
            icon="🛠",
            permission=Permission.SETTINGS,
            web_path="/settings/maintenance",
            bot_callback=MntCb(a="home").pack(),
            order=900,
        )
    ],
    commands=[BotCommandSpec("maintenance", "Техработы", "Maintenance", admin=True)],
    order=910,
)
