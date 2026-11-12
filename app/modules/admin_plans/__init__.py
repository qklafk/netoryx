"""Plans in the bot admin: create, edit, hide, archive and reorder tariff plans."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.keyboards import Nav
from app.modules.admin_plans.admin_bot import PLANS_NAV, router

module = FeatureModule(
    name="admin_plans",
    title_ru="Тарифы",
    title_en="Plans",
    admin_bot_routers=[router],
    admin_menu=[
        AdminMenuItem(
            key="plans",
            title_key="admin_plans.title",
            icon="📦",
            permission=Permission.PLANS,
            web_path="/plans",
            bot_callback=Nav(to=PLANS_NAV).pack(),
            order=40,
        )
    ],
    shop_only=True,
    order=23,
)
