"""Administrators (owner only) and active web sessions (own sessions for everyone, all for the owner)."""

from __future__ import annotations

from collections import Counter
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.permissions import ROLE_PERMISSIONS, Permission
from app.db.models import AdminSession, User
from app.modules.admin_api.deps import Actor, AnyAdmin, bad_request, conflict, not_found, require
from app.modules.admin_api.schemas.admins import AdminIn, AdminOut, AdminUpdate, RoleInfo, SessionOut
from app.modules.admin_api.schemas.common import Count, Ok
from app.services import admins
from app.web import auth
from app.web.deps import ReadDb
from app.web.errors import ApiError

router = APIRouter(tags=["admin: admins"])

Owner = Annotated[Actor, Depends(require(Permission.ADMINS))]

_ERROR_STATUS = {
    admins.AdminErrorCode.FORBIDDEN: 403,
    admins.AdminErrorCode.NOT_FOUND: 404,
    admins.AdminErrorCode.EXISTS: 409,
    admins.AdminErrorCode.LAST_OWNER: 409,
    admins.AdminErrorCode.ENV_OWNER: 409,
}


def _error(exc: admins.AdminError) -> Exception:
    return ApiError(_ERROR_STATUS.get(exc.code, 400), str(exc.code), str(exc))


async def _list(session: Any, settings: Any) -> list[AdminOut]:
    entries = await admins.list_admins(session, settings)
    ids = [e.tg_id for e in entries]
    users = {u.tg_id: u for u in (await session.scalars(select(User).where(User.tg_id.in_(ids)))).all()} if ids else {}
    sessions = Counter(s.admin_id for s in await auth.list_sessions(session))
    return [
        AdminOut(
            tg_id=e.tg_id,
            role=e.role,
            note=e.note,
            added_by=e.added_by,
            created_at=e.created_at,
            from_env=e.from_env,
            username=users[e.tg_id].username if e.tg_id in users else None,
            first_name=users[e.tg_id].first_name if e.tg_id in users else None,
            active_sessions=sessions.get(e.tg_id, 0),
        )
        for e in entries
    ]


@router.get("/admins/roles", response_model=list[RoleInfo])
async def roles(actor: AnyAdmin) -> list[RoleInfo]:
    """What every role may do."""
    return [RoleInfo(role=role, permissions=sorted(str(p) for p in perms)) for role, perms in ROLE_PERMISSIONS.items()]


@router.get("/admins", response_model=list[AdminOut])
async def list_admins(actor: Owner, session: ReadDb) -> list[AdminOut]:
    """Owners from the environment and administrators added here."""
    return await _list(session, actor.ctx.settings)


@router.post("/admins", response_model=list[AdminOut], status_code=201)
async def add_admin(body: AdminIn, actor: Owner) -> list[AdminOut]:
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        try:
            await admins.add(
                session, ctx.settings, actor.tg_id, body.tg_id, body.role, note=body.note, source="web", ip=actor.ip
            )
        except admins.AdminError as exc:
            raise _error(exc) from None
        return await _list(session, ctx.settings)


@router.patch("/admins/{tg_id}", response_model=list[AdminOut])
async def update_admin(tg_id: int, body: AdminUpdate, actor: Owner) -> list[AdminOut]:
    ctx = actor.ctx
    kwargs: dict[str, Any] = {"role": body.role}
    if body.clear_note:
        kwargs["note"] = None
    elif body.note is not None:
        kwargs["note"] = body.note
    if body.role is None and "note" not in kwargs:
        raise bad_request("nothing_to_change", "Nothing to change")
    async with ctx.session_factory.begin() as session:
        try:
            await admins.update(session, ctx.settings, actor.tg_id, tg_id, source="web", ip=actor.ip, **kwargs)
        except admins.AdminError as exc:
            raise _error(exc) from None
        return await _list(session, ctx.settings)


@router.delete("/admins/{tg_id}", response_model=Ok)
async def remove_admin(tg_id: int, actor: Owner) -> Ok:
    """Remove an administrator; their web sessions are revoked."""
    ctx = actor.ctx
    if tg_id == actor.tg_id:
        raise conflict("self", "You cannot remove yourself")
    async with ctx.session_factory.begin() as session:
        try:
            removed = await admins.remove(session, ctx.settings, actor.tg_id, tg_id, source="web", ip=actor.ip)
        except admins.AdminError as exc:
            raise _error(exc) from None
    if not removed:
        raise not_found("Not an administrator")
    return Ok()


# ---------------------------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------------------------
def _session_out(row: AdminSession, current_id: int) -> SessionOut:
    return SessionOut(
        id=row.id,
        admin_id=row.admin_id,
        ip=row.ip,
        user_agent=row.user_agent,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        current=row.id == current_id,
    )


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(actor: AnyAdmin, session: ReadDb, all_admins: bool = False) -> list[SessionOut]:
    """Active web sessions: your own, or everyone's (owner)."""
    if all_admins:
        actor.require(Permission.ADMINS)
    rows = await auth.list_sessions(session, admin_id=None if all_admins else actor.tg_id)
    return [_session_out(r, actor.principal.session_id) for r in rows]


@router.delete("/sessions/{session_id}", response_model=Ok)
async def revoke_session(session_id: int, actor: AnyAdmin) -> Ok:
    """Log a session out (your own, or anyone's for the owner)."""
    ctx = actor.ctx
    owner_scope = actor.can(Permission.ADMINS)
    async with ctx.session_factory.begin() as session:
        revoked = await auth.revoke_session(session, session_id, admin_id=None if owner_scope else actor.tg_id)
        if revoked:
            await actor.audit(session, "web.session_revoke", "admin_session", session_id)
    if not revoked:
        raise not_found("No such active session")
    return Ok()


@router.post("/sessions/revoke-others", response_model=Count)
async def revoke_other_sessions(actor: AnyAdmin) -> Count:
    """Log out all your other sessions (other browsers and devices)."""
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        rows = await auth.list_sessions(session, admin_id=actor.tg_id)
        count = 0
        for row in rows:
            if row.id != actor.principal.session_id and await auth.revoke_session(session, row.id):
                count += 1
        await actor.audit(session, "web.session_revoke_others", "admin_session", None, count=count)
    return Count(count=count)
