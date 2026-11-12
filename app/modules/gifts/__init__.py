"""Gifts: buy a subscription for someone else and share the code; the "Gift / promo code" hub."""

from __future__ import annotations

from app.config import Settings
from app.core.modules import FeatureModule
from app.modules.gifts.bot import router as bot_router


def _enabled(settings: Settings) -> bool:
    return settings.gifts_enabled


module = FeatureModule(
    name="gifts",
    title_ru="Подарки",
    title_en="Gifts",
    enabled=_enabled,
    bot_routers=[bot_router],
    shop_only=True,
    order=60,
)
