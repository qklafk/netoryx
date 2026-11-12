"""Cost sharing (optional, off by default): monthly reminders of each member's share of the server bill."""

from __future__ import annotations

from app.config import Settings
from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.keyboards import Nav
from app.modules.cost_sharing.bot import NAV, admin, router
from app.modules.cost_sharing.jobs import JOBS


def _enabled(settings: Settings) -> bool:
    return settings.app_mode == "personal" and settings.cost_sharing_enabled


module = FeatureModule(
    name="cost_sharing",
    title_ru="Складчина",
    title_en="Cost sharing",
    enabled=_enabled,
    bot_routers=[router],
    admin_bot_routers=[admin],
    jobs=list(JOBS),
    admin_menu=[
        AdminMenuItem(
            key="cost_sharing",
            title_key="cost_sharing.menu",
            icon="💸",
            permission=Permission.USERS_EDIT,
            web_path="/cost-sharing",
            bot_callback=Nav(to=NAV).pack(),
            order=45,
        )
    ],
    order=80,
)
