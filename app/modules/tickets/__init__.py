"""Support tickets: users write to support from the bot, admins answer from the bot (or the web admin)."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, BotCommandSpec, FeatureModule
from app.core.permissions import Permission
from app.modules.tickets.admin_bot import notice_router
from app.modules.tickets.admin_bot import router as admin_router
from app.modules.tickets.bot import router as user_router
from app.modules.tickets.callbacks import TicketAdminCb

module = FeatureModule(
    name="tickets",
    title_ru="Поддержка",
    title_en="Support",
    bot_routers=[user_router],
    admin_bot_routers=[admin_router, notice_router],
    admin_menu=[
        AdminMenuItem(
            key="tickets",
            title_key="tickets.title",
            icon="🆘",
            permission=Permission.TICKETS,
            web_path="/tickets",
            bot_callback=TicketAdminCb(a="inbox").pack(),
            order=120,
        )
    ],
    commands=[
        BotCommandSpec("support", "Написать в поддержку", "Contact support"),
        BotCommandSpec("tickets", "Обращения пользователей", "Support inbox", admin=True),
    ],
    order=140,
)
