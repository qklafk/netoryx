"""Telegram proxy: a personal MTProto proxy per user (screen "🛡 Прокси для Telegram", /proxy) and its jobs."""

from __future__ import annotations

from app.core.modules import BotCommandSpec, FeatureModule
from app.modules.tg_proxy.bot import router
from app.modules.tg_proxy.jobs import JOBS

module = FeatureModule(
    name="tg_proxy",
    title_ru="Прокси для Telegram",
    title_en="Telegram proxy",
    bot_routers=[router],
    jobs=list(JOBS),
    commands=[BotCommandSpec("proxy", "Прокси для Telegram", "Telegram proxy")],
    order=60,
)
