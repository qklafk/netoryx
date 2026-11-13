"""Admin roles and permissions shared by the bot admin menu and the web admin."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from aiogram.filters import Filter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Admin, AdminRole


class Permission(StrEnum):
    VIEW_DASHBOARD = "view_dashboard"
    USERS_VIEW = "users_view"
    USERS_EDIT = "users_edit"
    USERS_MONEY = "users_money"
    SUBS_BULK = "subs_bulk"
    SUBS_GRANT = "subs_grant"  # give service without payment: add days, move the expiry, raise limits, change plan
    PLANS = "plans"
    PANELS = "panels"
    PAYMENTS_VIEW = "payments_view"
    PAYMENTS_EDIT = "payments_edit"
    PROMO = "promo"
    REFERRAL = "referral"
    WITHDRAWALS = "withdrawals"
    BROADCASTS = "broadcasts"
    TICKETS = "tickets"
    TEXTS = "texts"
    SETTINGS = "settings"
    SETTINGS_SECRETS = "settings_secrets"
    ADMINS = "admins"
    BACKUPS = "backups"
    BACKUP_RESTORE = "backup_restore"
    SYSTEM = "system"
    WEBHOOKS = "webhooks"  # outgoing webhooks: endpoint URLs and signing secrets


_OWNER_ONLY = frozenset({Permission.ADMINS, Permission.BACKUP_RESTORE, Permission.SETTINGS_SECRETS})

ROLE_PERMISSIONS: dict[AdminRole, frozenset[Permission]] = {
    AdminRole.OWNER: frozenset(Permission),
    AdminRole.ADMIN: frozenset(Permission) - _OWNER_ONLY,
    AdminRole.SUPPORT: frozenset(
        {
            Permission.VIEW_DASHBOARD,
            Permission.USERS_VIEW,
            Permission.USERS_EDIT,
            Permission.PAYMENTS_VIEW,
            Permission.TICKETS,
        }
    ),
    AdminRole.VIEWER: frozenset({Permission.VIEW_DASHBOARD, Permission.USERS_VIEW, Permission.PAYMENTS_VIEW}),
}

# Higher number = more privileges; used to stop admins from managing someone above them.
ROLE_RANK: dict[AdminRole, int] = {AdminRole.VIEWER: 1, AdminRole.SUPPORT: 2, AdminRole.ADMIN: 3, AdminRole.OWNER: 4}


def has_permission(role: AdminRole | None, permission: Permission) -> bool:
    """True if ``role`` grants ``permission`` (``None`` = not an admin)."""
    return role is not None and permission in ROLE_PERMISSIONS[role]


def permissions_of(role: AdminRole | None) -> frozenset[Permission]:
    return ROLE_PERMISSIONS[role] if role is not None else frozenset()


async def resolve_role(session: AsyncSession, settings: Settings, tg_id: int) -> AdminRole | None:
    """The user's admin role: ``ADMIN_IDS`` are always owners, others come from the ``admins`` table."""
    if tg_id in settings.admin_ids:
        return AdminRole.OWNER
    return await session.scalar(select(Admin.role).where(Admin.tg_id == tg_id))


class HasPermission(Filter):
    """aiogram filter: passes when the ``admin_role`` injected by the user middleware grants every permission."""

    def __init__(self, *permissions: Permission) -> None:
        if not permissions:
            raise ValueError("HasPermission needs at least one permission")
        self.permissions = permissions

    async def __call__(self, event: Any, admin_role: AdminRole | None = None) -> bool:
        return all(has_permission(admin_role, p) for p in self.permissions)

    def __str__(self) -> str:
        return f"HasPermission({', '.join(self.permissions)})"


class IsAdmin(Filter):
    """aiogram filter: passes for any admin role."""

    async def __call__(self, event: Any, admin_role: AdminRole | None = None) -> bool:
        return admin_role is not None
