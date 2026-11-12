"""Promo codes: enter a promo or gift code, get bonus money and/or subscription days."""

from __future__ import annotations

from app.core.modules import FeatureModule
from app.modules.promo.bot import router as bot_router

module = FeatureModule(
    name="promo",
    title_ru="Промокоды",
    title_en="Promo codes",
    bot_routers=[bot_router],
    shop_only=True,
    order=61,
)
