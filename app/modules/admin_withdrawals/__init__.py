"""Partner payout requests in the bot admin: the queue with anti-fraud flags and the approve / paid / reject flow."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.keyboards import Nav
from app.modules.admin_withdrawals.admin_bot import WITHDRAWALS_NAV, router

module = FeatureModule(
    name="admin_withdrawals",
    title_ru="Выводы партнёров",
    title_en="Partner payouts",
    admin_bot_routers=[router],
    admin_menu=[
        AdminMenuItem(
            key="withdrawals",
            title_key="admin_withdrawals.title",
            icon="💸",
            permission=Permission.WITHDRAWALS,
            web_path="/referral/withdrawals",
            bot_callback=Nav(to=WITHDRAWALS_NAV).pack(),
            order=70,
        )
    ],
    shop_only=True,
    order=26,
)
