"""Admin dashboard in the bot: headline numbers and the last days at a glance."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.keyboards import Nav
from app.modules.admin_dashboard.admin_bot import DASHBOARD_NAV, router

module = FeatureModule(
    name="admin_dashboard",
    title_ru="Дашборд",
    title_en="Dashboard",
    admin_bot_routers=[router],
    admin_menu=[
        AdminMenuItem(
            key="dashboard",
            title_key="admin_dashboard.title",
            icon="📊",
            permission=Permission.VIEW_DASHBOARD,
            web_path="/dashboard",
            bot_callback=Nav(to=DASHBOARD_NAV).pack(),
            order=10,
        )
    ],
    order=20,
)
