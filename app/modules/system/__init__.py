"""System module (always on): process status for admins and housekeeping jobs (maintenance windows)."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, BotCommandSpec, FeatureModule
from app.core.permissions import Permission
from app.modules.system.admin_bot import router as admin_router
from app.modules.system.jobs import JOBS

module = FeatureModule(
    name="system",
    title_ru="Система",
    title_en="System",
    required=True,
    admin_bot_routers=[admin_router],
    jobs=list(JOBS),
    admin_menu=[
        AdminMenuItem(
            key="system",
            title_key="system.title",
            icon="⚙️",
            permission=Permission.SYSTEM,
            web_path="/system",
            order=1000,
        )
    ],
    commands=[BotCommandSpec("status", "Состояние системы", "System status", admin=True)],
    order=0,
)
