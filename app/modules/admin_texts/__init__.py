"""Bot texts editor: find any text, edit it per language with validation, reset to the built-in version."""

from __future__ import annotations

from app.core.modules import AdminMenuItem, FeatureModule
from app.core.permissions import Permission
from app.modules.admin_texts.admin_bot import TxCb
from app.modules.admin_texts.admin_bot import router as admin_router

module = FeatureModule(
    name="admin_texts",
    title_ru="Тексты бота",
    title_en="Bot texts",
    required=True,
    admin_bot_routers=[admin_router],
    admin_menu=[
        AdminMenuItem(
            key="texts",
            title_key="admin_texts.title",
            icon="📝",
            permission=Permission.TEXTS,
            web_path="/texts",
            bot_callback=TxCb(a="home").pack(),
            order=860,
        )
    ],
    order=909,
)
