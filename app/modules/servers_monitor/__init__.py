"""Server monitoring: CPU / memory / disk / traffic / uptime of every server, Xray restart, load alerts."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.keyboards import Nav
from app.modules.servers_monitor.admin_bot import router
from app.modules.servers_monitor.jobs import JOBS
from app.modules.servers_monitor.views import NAV

module = FeatureModule(
    name="servers_monitor",
    title_ru="Мониторинг серверов",
    title_en="Server monitoring",
    admin_bot_routers=[router],
    jobs=list(JOBS),
    admin_menu=[
        AdminMenuItem(
            key="servers_monitor",
            title_key="servers_monitor.menu",
            icon="📡",
            permission=Permission.PANELS,
            web_path="/servers/status",
            bot_callback=Nav(to=NAV).pack(),
            order=55,
        )
    ],
    order=70,
)
