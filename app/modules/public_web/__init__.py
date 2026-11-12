"""Public web routes: subscription URLs for VPN apps, app import pages, gift landing, Remnawave webhook."""

from __future__ import annotations

from app.core.modules import FeatureModule
from app.modules.public_web.web import router

module = FeatureModule(
    name="public_web",
    title_ru="Публичные страницы",
    title_en="Public pages",
    required=True,  # subscription URLs must keep working whatever else is switched off
    web_routers=[router],
    order=1,
)
