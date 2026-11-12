"""aiogram filters for handlers, in one place.

``HasPermission`` / ``IsAdmin`` read the ``admin_role`` injected by the user middleware; ``ModuleEnabled`` hides a
module's handlers while the module is off or under partial maintenance. Typical admin handler::

    @router.callback_query(Nav.filter(F.to == "users"), HasPermission(Permission.USERS_VIEW))
"""

from __future__ import annotations

from typing import Any

from aiogram.filters import Filter

from app.core.modules import ModuleEnabled
from app.core.permissions import HasPermission, IsAdmin, Permission, has_permission
from app.db.models import AdminRole
from app.services.admins import at_least

__all__ = [
    "HasPermission",
    "IsAdmin",
    "IsPrivate",
    "ModuleEnabled",
    "Permission",
    "RoleAtLeast",
    "has_permission",
]


class RoleAtLeast(Filter):
    """Passes for administrators with ``role`` or a higher one (OWNER > ADMIN > SUPPORT > VIEWER)."""

    def __init__(self, role: AdminRole) -> None:
        self.role = role

    async def __call__(self, event: Any, admin_role: AdminRole | None = None) -> bool:
        return at_least(admin_role, self.role)

    def __str__(self) -> str:
        return f"RoleAtLeast({self.role.value})"


class IsPrivate(Filter):
    """Passes for updates from a private chat with the bot (or chat-less user events such as inline queries)."""

    async def __call__(self, event: Any, event_chat: Any = None) -> bool:
        return event_chat is None or getattr(event_chat, "type", None) == "private"
