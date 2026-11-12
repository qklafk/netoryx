"""Payments: method picker, invoices (provider pages, Telegram Stars incl. 30-day subscriptions), donations,
provider webhooks and the reconcile job."""

from __future__ import annotations

from app.core.modules import FeatureModule
from app.modules.payments import confirm
from app.modules.payments.bot import router as bot_router
from app.modules.payments.jobs import JOBS
from app.modules.payments.web import router as web_router

module = FeatureModule(
    name="payments",
    title_ru="Платежи",
    title_en="Payments",
    bot_routers=[bot_router],
    web_routers=[web_router],
    jobs=list(JOBS),
    on_startup=[confirm.on_startup],
    on_shutdown=[confirm.on_shutdown],
    shop_only=True,
    order=40,
)
