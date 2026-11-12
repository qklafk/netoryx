"""Subscription module (always on): the subscription card, trial, plans and checkout, connection guide, devices,
plus the jobs that keep subscriptions and panels in shape (expiry, auto-renewal, reminders, reconcile, usage,
inbound refresh, panel health)."""

from __future__ import annotations

from app.core.modules import BotCommandSpec, FeatureModule
from app.modules.subscription.addons import router as addons_router
from app.modules.subscription.bot import router
from app.modules.subscription.checkout import router as checkout_router
from app.modules.subscription.jobs import JOBS

module = FeatureModule(
    name="subscription",
    title_ru="Подписка",
    title_en="Subscription",
    required=True,
    bot_routers=[router, checkout_router, addons_router],
    jobs=list(JOBS),
    commands=[
        BotCommandSpec("sub", "Моя подписка", "My subscription"),
        BotCommandSpec("connect", "Подключить VPN", "Connect VPN"),
    ],
    order=10,
)
