"""Bot administrators: owners from ``ADMIN_IDS`` plus admins with roles stored in the ``admins`` table.

* ``ADMIN_IDS`` (environment) are always :attr:`AdminRole.OWNER` and cannot be changed or removed from the bot
  or the web admin - this is the recovery path if the table is misconfigured.
* Only owners manage admins. The last owner can never be removed or demoted.
* Removing an admin revokes their web admin sessions.

Functions taking ``session`` run in the caller's transaction and never commit.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, AdminRole, AdminSession
from app.services import audit

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)

MAX_NOTE_LENGTH = 128
# Highest first: a role "outranks" every role after it.
ROLE_ORDER: tuple[AdminRole, ...] = (AdminRole.OWNER, AdminRole.ADMIN, AdminRole.SUPPORT, AdminRole.VIEWER)
_RANK = {role: index for index, role in enumerate(ROLE_ORDER)}
_KEEP = object()  # "leave unchanged" marker for optional update arguments


class AdminErrorCode(enum.StrEnum):
    FORBIDDEN = "forbidden"  # the actor is not an owner
    ENV_OWNER = "env_owner"  # owners from ADMIN_IDS are managed in the environment only
    EXISTS = "exists"
    NOT_FOUND = "not_found"
    LAST_OWNER = "last_owner"  # the change would leave nobody with the owner role
    INVALID = "invalid"


class AdminError(Exception):
    """An admin management rule refused the operation; nothing was written."""

    def __init__(self, code: AdminErrorCode, message: str | None = None) -> None:
        super().__init__(message or code.value)
        self.code = code


@dataclass(frozen=True, slots=True)
class AdminEntry:
    """One administrator as shown in the admin UI."""

    tg_id: int
    role: AdminRole
    note: str | None = None
    added_by: int | None = None
    created_at: datetime | None = None
    from_env: bool = False  # listed in ADMIN_IDS: read-only


def is_env_owner(settings: Settings, tg_id: int) -> bool:
    """True for owners configured in ``ADMIN_IDS``."""
    return tg_id in settings.admin_ids


def outranks(role: AdminRole, other: AdminRole) -> bool:
    """True if ``role`` is strictly higher than ``other`` (OWNER > ADMIN > SUPPORT > VIEWER)."""
    return _RANK[role] < _RANK[other]


def at_least(role: AdminRole | None, minimum: AdminRole) -> bool:
    """True if ``role`` is ``minimum`` or higher; ``None`` (not an admin) never is."""
    return role is not None and _RANK[role] <= _RANK[minimum]


def parse_role(value: str | AdminRole) -> AdminRole:
    """Role from its value (``"admin"``); raises :class:`AdminError` (INVALID) for unknown roles."""
    try:
        return AdminRole(str(value).strip().lower())
    except ValueError:
        raise AdminError(AdminErrorCode.INVALID, f"unknown role: {value!r}") from None


async def resolve_role(session: AsyncSession, settings: Settings, tg_id: int) -> AdminRole | None:
    """The user's admin role: OWNER for ``ADMIN_IDS``, else the ``admins`` row, else ``None``."""
    if is_env_owner(settings, tg_id):
        return AdminRole.OWNER
    return await session.scalar(select(Admin.role).where(Admin.tg_id == tg_id))


async def get(session: AsyncSession, tg_id: int) -> Admin | None:
    """The stored admin row (owners from ``ADMIN_IDS`` have none unless also added to the table)."""
    return await session.get(Admin, tg_id)


async def list_admins(session: AsyncSession, settings: Settings) -> list[AdminEntry]:
    """Every administrator: ``ADMIN_IDS`` owners first, then stored admins by role and date."""
    entries = [
        AdminEntry(tg_id=tg_id, role=AdminRole.OWNER, from_env=True) for tg_id in dict.fromkeys(settings.admin_ids)
    ]
    env = set(settings.admin_ids)
    rows = (await session.scalars(select(Admin).order_by(Admin.created_at, Admin.tg_id))).all()
    stored = [
        AdminEntry(tg_id=a.tg_id, role=a.role, note=a.note, added_by=a.added_by, created_at=a.created_at)
        for a in rows
        if a.tg_id not in env
    ]
    stored.sort(key=lambda e: _RANK[e.role])  # stable: keeps the date order within a role
    return entries + stored


async def ids_with_roles(session: AsyncSession, settings: Settings, roles: Iterable[AdminRole]) -> list[int]:
    """Telegram ids of administrators holding any of ``roles`` (env owners included for OWNER), unique."""
    wanted = set(roles)
    result: dict[int, None] = {}
    if AdminRole.OWNER in wanted:
        result.update(dict.fromkeys(settings.admin_ids))
    if wanted:
        rows = await session.scalars(select(Admin.tg_id).where(Admin.role.in_(wanted)).order_by(Admin.tg_id))
        result.update(dict.fromkeys(rows.all()))
    return list(result)


async def _require_owner(session: AsyncSession, settings: Settings, actor_id: int) -> None:
    if await resolve_role(session, settings, actor_id) is not AdminRole.OWNER:
        raise AdminError(AdminErrorCode.FORBIDDEN, "only owners manage administrators")


async def _owner_count(session: AsyncSession, settings: Settings) -> int:
    env = set(settings.admin_ids)
    stmt = select(func.count()).select_from(Admin).where(Admin.role == AdminRole.OWNER)
    if env:
        stmt = stmt.where(Admin.tg_id.not_in(env))
    return len(env) + int(await session.scalar(stmt) or 0)


def _clean_note(note: str | None) -> str | None:
    if note is None:
        return None
    cleaned = note.strip()[:MAX_NOTE_LENGTH]
    return cleaned or None


async def add(
    session: AsyncSession,
    settings: Settings,
    actor_id: int,
    tg_id: int,
    role: AdminRole | str,
    *,
    note: str | None = None,
    source: str = "bot",
    ip: str | None = None,
) -> Admin:
    """Make ``tg_id`` an administrator with ``role`` (owners only); audited.

    Raises :class:`AdminError`: FORBIDDEN, ENV_OWNER, EXISTS, INVALID.
    """
    parsed = parse_role(role)
    await _require_owner(session, settings, actor_id)
    if tg_id <= 0:
        raise AdminError(AdminErrorCode.INVALID, "administrators must be Telegram users")
    if is_env_owner(settings, tg_id):
        raise AdminError(AdminErrorCode.ENV_OWNER)
    admin = Admin(tg_id=tg_id, role=parsed, note=_clean_note(note), added_by=actor_id)
    try:
        async with session.begin_nested():
            session.add(admin)
            await session.flush()
    except IntegrityError:
        raise AdminError(AdminErrorCode.EXISTS) from None
    await audit.log(session, actor_id, "admin.add", "admin", tg_id, source=source, ip=ip, role=parsed.value)
    log.info("admin %s added %s as %s", actor_id, tg_id, parsed)
    return admin


async def update(
    session: AsyncSession,
    settings: Settings,
    actor_id: int,
    tg_id: int,
    *,
    role: AdminRole | str | None = None,
    note: str | object | None = _KEEP,
    source: str = "bot",
    ip: str | None = None,
) -> Admin:
    """Change an administrator's role and/or note (owners only); audited.

    Raises :class:`AdminError`: FORBIDDEN, ENV_OWNER, NOT_FOUND, LAST_OWNER, INVALID.
    """
    parsed = parse_role(role) if role is not None else None
    await _require_owner(session, settings, actor_id)
    if is_env_owner(settings, tg_id):
        raise AdminError(AdminErrorCode.ENV_OWNER)
    admin = await session.get(Admin, tg_id, with_for_update=True)
    if admin is None:
        raise AdminError(AdminErrorCode.NOT_FOUND)
    changes: dict[str, object] = {}
    if parsed is not None and parsed is not admin.role:
        if admin.role is AdminRole.OWNER and await _owner_count(session, settings) <= 1:
            raise AdminError(AdminErrorCode.LAST_OWNER)
        changes["role"] = parsed.value
        changes["old_role"] = admin.role.value
        admin.role = parsed
    if note is not _KEEP:
        new_note = _clean_note(note if isinstance(note, str) else None)
        if new_note != admin.note:
            changes["note"] = new_note
            admin.note = new_note
    if changes:
        await session.flush()
        await audit.log(session, actor_id, "admin.update", "admin", tg_id, source=source, ip=ip, **changes)
    return admin


async def remove(
    session: AsyncSession,
    settings: Settings,
    actor_id: int,
    tg_id: int,
    *,
    source: str = "bot",
    ip: str | None = None,
) -> bool:
    """Remove an administrator (owners only) and revoke their web sessions; False if they were not one.

    Raises :class:`AdminError`: FORBIDDEN, ENV_OWNER, LAST_OWNER.
    """
    await _require_owner(session, settings, actor_id)
    if is_env_owner(settings, tg_id):
        raise AdminError(AdminErrorCode.ENV_OWNER)
    admin = await session.get(Admin, tg_id, with_for_update=True)
    if admin is None:
        return False
    if admin.role is AdminRole.OWNER and await _owner_count(session, settings) <= 1:
        raise AdminError(AdminErrorCode.LAST_OWNER)
    role = admin.role
    await session.delete(admin)
    revoked = await revoke_sessions(session, tg_id)
    await audit.log(
        session, actor_id, "admin.remove", "admin", tg_id, source=source, ip=ip, role=role.value, sessions=revoked
    )
    log.info("admin %s removed %s (%s)", actor_id, tg_id, role)
    return True


async def revoke_sessions(session: AsyncSession, tg_id: int) -> int:
    """Revoke every active web admin session of ``tg_id``; returns how many were revoked."""
    result = await session.execute(
        sa_update(AdminSession)
        .where(AdminSession.admin_id == tg_id, AdminSession.revoked.is_(False))
        .values(revoked=True)
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)
