"""Subscriptions in the bot admin: who is about to expire, compensation days for everyone, bulk re-sync."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.keyboards import Nav
from app.modules.admin_subscriptions.admin_bot import SUBS_NAV, resume_on_startup, router, stop_jobs

module = FeatureModule(
    name="admin_subscriptions",
    title_ru="Подписки",
    title_en="Subscriptions",
    admin_bot_routers=[router],
    admin_menu=[
        AdminMenuItem(
            key="subscriptions",
            title_key="admin_subscriptions.title",
            icon="🗂",
            permission=Permission.SUBS_BULK,
            web_path="/subscriptions",
            bot_callback=Nav(to=SUBS_NAV).pack(),
            order=30,
        )
    ],
    on_startup=[resume_on_startup],
    on_shutdown=[stop_jobs],
    order=22,
)
