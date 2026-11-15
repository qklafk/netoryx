"""Servers for admins: panels, inbounds (incl. creating VLESS-Reality), locations, endpoints and a health view."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.modules.admin_servers.admin_bot import router as panels_router
from app.modules.admin_servers.callbacks import SrvCb
from app.modules.admin_servers.endpoints import router as endpoints_router
from app.modules.admin_servers.locations import router as locations_router
from app.modules.admin_servers.node_join import router as node_join_router

module = FeatureModule(
    name="admin_servers",
    title_ru="Серверы",
    title_en="Servers",
    required=True,
    admin_bot_routers=[panels_router, locations_router, endpoints_router],
    web_routers=[node_join_router],  # a new server registers itself with a one-time join code
    admin_menu=[
        AdminMenuItem(
            key="servers",
            title_key="admin_servers.title",
            icon="🖥",
            permission=Permission.PANELS,
            web_path="/servers",
            bot_callback=SrvCb(a="home").pack(),
            order=50,
        )
    ],
    order=900,
)
