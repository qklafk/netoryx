"""Promo codes in the bot admin: create step by step, statistics, deactivate; gift codes overview."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.keyboards import Nav
from app.modules.admin_promo.admin_bot import PROMO_NAV, router

module = FeatureModule(
    name="admin_promo",
    title_ru="Промокоды",
    title_en="Promo codes",
    admin_bot_routers=[router],
    admin_menu=[
        AdminMenuItem(
            key="promo",
            title_key="admin_promo.title",
            icon="🏷",
            permission=Permission.PROMO,
            web_path="/promo",
            bot_callback=Nav(to=PROMO_NAV).pack(),
            order=60,
        )
    ],
    shop_only=True,
    order=25,
)
