"""Wallet: balance (main and partner), top-ups and the operations history."""

from __future__ import annotations

from app.core.modules import FeatureModule
from app.modules.wallet.bot import router as bot_router

module = FeatureModule(
    name="wallet",
    title_ru="Баланс",
    title_en="Balance",
    bot_routers=[bot_router],
    shop_only=True,
    order=30,
)
