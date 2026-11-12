"""Backups in the bot: create now, download, restore from an uploaded file (owner only)."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule, JobSpec
from app.core.permissions import Permission
from app.modules.admin_backups.admin_bot import SWEEP_INTERVAL_MIN, BkCb, sweep_uploads_job
from app.modules.admin_backups.admin_bot import router as admin_router

module = FeatureModule(
    name="admin_backups",
    title_ru="Резервные копии",
    title_en="Backups",
    required=True,
    admin_bot_routers=[admin_router],
    jobs=[
        JobSpec(
            id="backups.sweep_uploads",
            func=sweep_uploads_job,
            kwargs={"minutes": SWEEP_INTERVAL_MIN},
            run_on_start=True,
        )
    ],
    admin_menu=[
        AdminMenuItem(
            key="backups",
            title_key="admin_backups.title",
            icon="💾",
            permission=Permission.BACKUPS,
            web_path="/backups",
            bot_callback=BkCb(a="home").pack(),
            order=870,
        )
    ],
    order=906,
)
