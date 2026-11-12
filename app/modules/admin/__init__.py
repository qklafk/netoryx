"""Bot admin menu: entries contributed by every enabled module (filtered by role) and the web admin login link."""

from __future__ import annotations

from app.core.modules import BotCommandSpec, FeatureModule
from app.modules.admin.admin_bot import router

module = FeatureModule(
    name="admin",
    title_ru="Админ-меню",
    title_en="Admin menu",
    required=True,
    admin_bot_routers=[router],
    commands=[
        BotCommandSpec("admin", "Админ-панель", "Admin panel", admin=True),
        BotCommandSpec("web", "Вход в веб-админку", "Web admin login", admin=True),
    ],
    order=5,
)
