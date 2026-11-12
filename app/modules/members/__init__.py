"""Members of a private VPN (personal mode): list and per-member actions for the owner."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.keyboards import Nav
from app.modules.members.admin_api import router as api_router
from app.modules.members.admin_bot import router
from app.modules.members.callbacks import NAV
from app.modules.members.jobs import JOBS

module = FeatureModule(
    name="members",
    title_ru="Участники",
    title_en="Members",
    enabled=lambda settings: settings.app_mode == "personal",
    admin_api_routers=[api_router],
    admin_bot_routers=[router],
    jobs=list(JOBS),
    admin_menu=[
        AdminMenuItem(
            key="members",
            title_key="members.menu",
            icon="👥",
            permission=Permission.USERS_EDIT,
            web_path="/members",
            bot_callback=Nav(to=NAV).pack(),
            order=30,
        )
    ],
    order=40,
)
