"""Receipts in "Мой налог" for self-employed sellers: registration job, buyer notices, web admin section."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.modules.npd_receipts.admin_api import router as api_router
from app.modules.npd_receipts.jobs import JOBS

module = FeatureModule(
    name="npd_receipts",
    title_ru="Чеки «Мой налог»",
    title_en="NPD receipts",
    admin_api_routers=[api_router],
    jobs=list(JOBS),
    admin_menu=[
        AdminMenuItem(
            key="npd_receipts",
            title_key="npd_receipts.menu",
            icon="🧾",
            permission=Permission.PAYMENTS_VIEW,
            web_path="/npd-receipts",
            order=55,
        )
    ],
    shop_only=True,
    order=60,
)
