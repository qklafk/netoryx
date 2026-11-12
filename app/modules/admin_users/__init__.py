"""Users in the bot admin: search, the full user card and every action on a user."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, BotCommandSpec, FeatureModule
from app.core.permissions import Permission
from app.keyboards import Nav
from app.modules.admin_users.admin_bot import USERS_NAV, router

module = FeatureModule(
    name="admin_users",
    title_ru="Пользователи",
    title_en="Users",
    admin_bot_routers=[router],
    admin_menu=[
        AdminMenuItem(
            key="users",
            title_key="admin_users.title",
            icon="👥",
            permission=Permission.USERS_VIEW,
            web_path="/users",
            bot_callback=Nav(to=USERS_NAV).pack(),
            order=20,
        )
    ],
    commands=[BotCommandSpec("user", "Найти пользователя", "Find a user", admin=True)],
    order=21,
)
