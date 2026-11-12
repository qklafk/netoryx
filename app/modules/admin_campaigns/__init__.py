"""Ad campaigns in the bot admin: create tracked start links and watch their funnel."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.keyboards import Nav
from app.modules.admin_campaigns.admin_bot import CAMPAIGNS_NAV, router

module = FeatureModule(
    name="admin_campaigns",
    title_ru="Рекламные кампании",
    title_en="Ad campaigns",
    admin_bot_routers=[router],
    admin_menu=[
        AdminMenuItem(
            key="campaigns",
            title_key="admin_campaigns.title",
            icon="📣",
            permission=Permission.PROMO,
            web_path="/campaigns",
            bot_callback=Nav(to=CAMPAIGNS_NAV).pack(),
            order=80,
        )
    ],
    shop_only=True,
    order=27,
)
