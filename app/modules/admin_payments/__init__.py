"""Payments in the bot admin: list, details, provider check, manual credit, re-run of a stuck payment, Stars refund."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.keyboards import Nav
from app.modules.admin_payments.admin_bot import PAYMENTS_NAV, router

module = FeatureModule(
    name="admin_payments",
    title_ru="Платежи",
    title_en="Payments",
    admin_bot_routers=[router],
    admin_menu=[
        AdminMenuItem(
            key="payments",
            title_key="admin_payments.title",
            icon="💳",
            permission=Permission.PAYMENTS_VIEW,
            web_path="/payments",
            bot_callback=Nav(to=PAYMENTS_NAV).pack(),
            order=50,
        )
    ],
    shop_only=True,
    order=24,
)
