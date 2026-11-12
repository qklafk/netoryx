"""Broadcasts: messages to user segments composed in the bot (any message type), scheduled and resumable."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, BotCommandSpec, FeatureModule
from app.core.permissions import Permission
from app.modules.broadcasts.admin_bot import router as admin_router
from app.modules.broadcasts.callbacks import BcCb
from app.modules.broadcasts.jobs import JOBS

module = FeatureModule(
    name="broadcasts",
    title_ru="Рассылки",
    title_en="Broadcasts",
    admin_bot_routers=[admin_router],
    jobs=list(JOBS),
    admin_menu=[
        AdminMenuItem(
            key="broadcasts",
            title_key="broadcasts.title",
            icon="📣",
            permission=Permission.BROADCASTS,
            web_path="/broadcasts",
            bot_callback=BcCb(a="home").pack(),
            order=110,
        )
    ],
    commands=[BotCommandSpec("broadcast", "Рассылка", "Broadcast", admin=True)],
    order=150,
)
