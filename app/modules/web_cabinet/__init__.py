"""Web cabinet: the customer area in a regular browser, for when Telegram is blocked or unavailable.

The interface is the Mini App served at ``/cabinet``; this module adds the ways to log in (a one-time link from the
bot, the Telegram Login Widget, an e-mail link), the sessions and the confirmed e-mail address of an account.
"""

from __future__ import annotations

from app.core.modules import BotCommandSpec, FeatureModule
from app.modules.web_cabinet.api import account_router, auth_router
from app.modules.web_cabinet.bot import router as bot_router
from app.modules.web_cabinet.jobs import JOBS
from app.modules.web_cabinet.pages import router as pages_router

module = FeatureModule(
    name="web_cabinet",
    title_ru="Веб-кабинет",
    title_en="Web cabinet",
    enabled=lambda settings: settings.cabinet_enabled,
    bot_routers=[bot_router],
    web_routers=[auth_router, account_router, pages_router],
    jobs=list(JOBS),
    commands=[BotCommandSpec("cabinet", "Веб-кабинет в браузере", "Web cabinet in a browser")],
    order=910,
)
