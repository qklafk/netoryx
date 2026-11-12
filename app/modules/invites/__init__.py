"""Invitations: free access for family and friends (``/start inv_<code>``), created and managed by admins."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.keyboards import Nav
from app.modules.invites.admin_api import router as api_router
from app.modules.invites.admin_bot import router as admin_router
from app.modules.invites.callbacks import NAV
from app.modules.invites.redeem import router as redeem_router

module = FeatureModule(
    name="invites",
    title_ru="Приглашения",
    title_en="Invitations",
    bot_routers=[redeem_router],
    admin_api_routers=[api_router],
    admin_bot_routers=[admin_router],
    admin_menu=[
        AdminMenuItem(
            key="invites",
            title_key="invites.menu",
            icon="✉️",
            permission=Permission.USERS_EDIT,
            web_path="/invites",
            bot_callback=Nav(to=NAV).pack(),
            order=40,
        )
    ],
    order=5,  # before the generic /start handler: it answers "inv_" deep links itself
)
