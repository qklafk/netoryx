"""Web admin API of invitations (mounted under ``/api/admin/invites``)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.permissions import Permission
from app.db.models import Invite, User
from app.db.session import read_session
from app.modules.invites.views import invite_link, invite_state
from app.services import invites
from app.utils.time import MAX_PERIOD_DAYS, utcnow
from app.web.auth import AdminPrincipal, admin_user
from app.web.deps import Ctx, PageParams
from app.web.errors import ApiError

router = APIRouter(tags=["invites"])
Editor = Annotated[AdminPrincipal, Depends(admin_user(Permission.USERS_EDIT))]
SOURCE = "web"


class InviteOut(BaseModel):
    id: int
    code: str
    note: str | None
    days: int | None
    device_limit: int
    traffic_limit_gb: int
    max_uses: int
    uses: int
    expires_at: datetime | None
    is_active: bool
    state: str
    link: str
    created_by: int
    created_at: datetime


class InvitePage(BaseModel):
    items: list[InviteOut]
    total: int
    limit: int
    offset: int


class InviteIn(BaseModel):
    days: int | None = Field(default=None, ge=1, le=MAX_PERIOD_DAYS, description="null = no end date")
    device_limit: int = Field(default=3, ge=0, le=1000)
    traffic_limit_gb: int = Field(default=0, ge=0, le=1_000_000)
    max_uses: int = Field(default=1, ge=0, le=100_000, description="0 = unlimited")
    expires_at: datetime | None = None
    note: str | None = Field(default=None, max_length=invites.MAX_NOTE_LENGTH)
    code: str | None = Field(default=None, max_length=32)


class JoinedOut(BaseModel):
    tg_id: int
    username: str | None
    first_name: str | None


class Ok(BaseModel):
    ok: bool = True


def _out(ctx: Any, invite: Invite) -> InviteOut:
    return InviteOut(
        id=invite.id,
        code=invite.code,
        note=invite.note,
        days=invite.days,
        device_limit=invite.device_limit,
        traffic_limit_gb=invite.traffic_limit_gb,
        max_uses=invite.max_uses,
        uses=invite.uses,
        expires_at=invite.expires_at,
        is_active=invite.is_active,
        state=invite_state(invite, utcnow()).value,
        link=invite_link(ctx.bot_username, invite),
        created_by=invite.created_by,
        created_at=invite.created_at,
    )


@router.get("", response_model=InvitePage)
async def list_invites(admin: Editor, ctx: Ctx, page: PageParams, active_only: bool = False) -> InvitePage:
    async with read_session(ctx.session_factory) as session:
        items = await invites.list_invites(session, active_only=active_only, limit=page.limit, offset=page.offset)
        count = select(func.count()).select_from(Invite)
        if active_only:
            count = count.where(Invite.is_active.is_(True))
        total = int(await session.scalar(count) or 0)
    return InvitePage(items=[_out(ctx, i) for i in items], total=total, limit=page.limit, offset=page.offset)


@router.post("", response_model=InviteOut, status_code=201)
async def create_invite(body: InviteIn, admin: Editor, ctx: Ctx) -> InviteOut:
    try:
        async with ctx.session_factory.begin() as session:
            invite = await invites.create(
                session,
                admin.tg_id,
                days=body.days,
                device_limit=body.device_limit,
                traffic_limit_gb=body.traffic_limit_gb,
                max_uses=body.max_uses,
                expires_at=body.expires_at,
                note=body.note,
                code=body.code,
                source=SOURCE,
                ip=admin.ip,
            )
    except invites.InviteCodeExists:
        raise ApiError(409, "code_taken", "This invitation code is already used") from None
    except ValueError as exc:
        raise ApiError(400, "invalid", str(exc)) from None
    return _out(ctx, invite)


@router.post("/{invite_id}/revoke", response_model=Ok)
async def revoke_invite(invite_id: int, admin: Editor, ctx: Ctx) -> Ok:
    async with ctx.session_factory.begin() as session:
        if await session.get(Invite, invite_id) is None:
            raise ApiError(404, "not_found", "Invitation not found")
        await invites.revoke(session, invite_id, admin.tg_id, source=SOURCE, ip=admin.ip)
    return Ok()


@router.get("/{invite_id}/users", response_model=list[JoinedOut])
async def invite_users(invite_id: int, admin: Editor, ctx: Ctx) -> list[JoinedOut]:
    async with read_session(ctx.session_factory) as session:
        if await session.get(Invite, invite_id) is None:
            raise ApiError(404, "not_found", "Invitation not found")
        ids = await invites.redeemed_by(session, invite_id)
        rows = list(await session.scalars(select(User).where(User.tg_id.in_(ids)))) if ids else []
    return [JoinedOut(tg_id=u.tg_id, username=u.username, first_name=u.first_name) for u in rows]
