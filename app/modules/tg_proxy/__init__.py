"""Telegram proxy: a personal MTProto proxy per user (screen "🛡 Прокси для Telegram", /proxy) and its jobs."""

from __future__ import annotations

from app.config import Settings
from app.core.modules import BotCommandSpec, FeatureModule
from app.modules.tg_proxy.bot import router
from app.modules.tg_proxy.jobs import JOBS


def _proxies_on(settings: Settings) -> bool:
    """The command is listed only while a Telegram proxy (MTProto or SOCKS5) is offered to users."""
    return settings.tg_proxy_access != "off" or settings.tg_socks_enabled


module = FeatureModule(
    name="tg_proxy",
    title_ru="Прокси для Telegram",
    title_en="Telegram proxy",
    bot_routers=[router],
    jobs=list(JOBS),
    commands=[BotCommandSpec("proxy", "Прокси для Telegram", "Telegram proxy", when=_proxies_on)],
    order=60,
)
