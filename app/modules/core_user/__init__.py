"""Core user module (always on): /start with deep links, the main menu, language, profile, help, legal pages."""

from __future__ import annotations

from app.core.modules import BotCommandSpec, FeatureModule
from app.modules.core_user.bot import router
from app.modules.core_user.fallback import install_fallback

module = FeatureModule(
    name="core_user",
    title_ru="Главное меню",
    title_en="Main menu",
    required=True,
    bot_routers=[router],
    commands=[
        BotCommandSpec("start", "Главное меню", "Main menu"),
        BotCommandSpec("help", "Помощь и инструкции", "Help and instructions"),
        BotCommandSpec("language", "Сменить язык", "Change language"),
        BotCommandSpec("paysupport", "Вопросы по оплате", "Payment support"),
        BotCommandSpec("terms", "Пользовательское соглашение", "Terms of service"),
        BotCommandSpec("privacy", "Политика конфиденциальности", "Privacy policy"),
    ],
    on_startup=[install_fallback],
    order=6,  # after the invitations module, which answers "inv_" links itself (bot routers run in order)
)
