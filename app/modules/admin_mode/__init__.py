"""Product mode wizard for the owner: personal VPN <-> shop."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.modules.admin_mode.admin_bot import ModeCb
from app.modules.admin_mode.admin_bot import router as admin_router

module = FeatureModule(
    name="admin_mode",
    title_ru="Режим работы",
    title_en="Product mode",
    required=True,
    admin_bot_routers=[admin_router],
    admin_menu=[
        AdminMenuItem(
            key="mode",
            title_key="admin_mode.title",
            icon="🔀",
            permission=Permission.SETTINGS_SECRETS,
            web_path="/settings/mode",
            bot_callback=ModeCb(a="home").pack(),
            order=880,
        )
    ],
    order=907,
)
