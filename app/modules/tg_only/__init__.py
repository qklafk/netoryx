"""Shared Telegram link: only Telegram goes through the VPN (admin screen, API, jobs); the public page is /tg."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.modules.tg_only.admin_bot import TgOnlyCb
from app.modules.tg_only.admin_bot import router as admin_router
from app.modules.tg_only.api import router as api_router
from app.modules.tg_only.jobs import JOBS

module = FeatureModule(
    name="tg_only",
    title_ru="Доступ к Telegram",
    title_en="Telegram access",
    admin_bot_routers=[admin_router],
    admin_api_routers=[api_router],
    jobs=list(JOBS),
    admin_menu=[
        AdminMenuItem(
            key="tg_only",
            title_key="tg_only.title",
            icon="✈️",
            permission=Permission.PANELS,
            web_path="/telegram-only",
            bot_callback=TgOnlyCb(a="home").pack(),
            order=55,
        )
    ],
    order=65,
)
