"""Referral program: invite links and stats; rewards in days, on the balance or on a partner balance with transfers
and payout requests."""

from __future__ import annotations

from app.config import Settings
from app.core.modules import FeatureModule
from app.modules.referral.bot import router as bot_router


def _enabled(settings: Settings) -> bool:
    return settings.referral_enabled


module = FeatureModule(
    name="referral",
    title_ru="Реферальная программа",
    title_en="Referral program",
    enabled=_enabled,
    bot_routers=[bot_router],
    shop_only=True,
    order=50,
)
