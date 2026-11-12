"""Administrators and roles (owner only)."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.modules.admin_admins.admin_bot import AdmCb
from app.modules.admin_admins.admin_bot import router as admin_router

module = FeatureModule(
    name="admin_admins",
    title_ru="Администраторы",
    title_en="Administrators",
    required=True,
    admin_bot_routers=[admin_router],
    admin_menu=[
        AdminMenuItem(
            key="admins",
            title_key="admin_admins.title",
            icon="👥",
            permission=Permission.ADMINS,
            web_path="/admins",
            bot_callback=AdmCb(a="home").pack(),
            order=890,
        )
    ],
    order=908,
)
