"""Web admin API of members (personal mode; mounted under ``/api/admin/members``)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.permissions import Permission
from app.modules.members import service
from app.services import devices
from app.utils.time import MAX_PERIOD_DAYS, utcnow
from app.web.auth import AdminPrincipal, admin_user
from app.web.deps import Ctx, PageParams
from app.web.errors import ApiError

router = APIRouter(tags=["members"])
Editor = Annotated[AdminPrincipal, Depends(admin_user(Permission.USERS_EDIT))]
SOURCE = "web"


class MemberOut(BaseModel):
    tg_id: int
    username: str | None
    first_name: str | None
    state: str  # active | paused | expired
    expires_at: datetime
    unlimited: bool
    device_limit: int
    traffic_limit_gb: int
    joined_at: datetime


class MemberPage(BaseModel):
    items: list[MemberOut]
    total: int
    limit: int
    offset: int


class ExtendIn(BaseModel):
    days: int | None = Field(default=None, ge=1, le=MAX_PERIOD_DAYS, description="null = no end date")


class LimitsIn(BaseModel):
    device_limit: int | None = Field(default=None, ge=0, le=1000)
    traffic_limit_gb: int | None = Field(default=None, ge=0, le=1_000_000)


class ResetOut(BaseModel):
    reset: int
    failed: int


class RemoveOut(BaseModel):
    complete: bool


def _out(member: service.Member) -> MemberOut:
    return MemberOut(
        tg_id=member.user.tg_id,
        username=member.user.username,
        first_name=member.user.first_name,
        state=member.state(utcnow()),
        expires_at=member.sub.expires_at,
        unlimited=member.unlimited,
        device_limit=member.sub.device_limit,
        traffic_limit_gb=member.sub.traffic_limit_gb,
        joined_at=member.sub.created_at,
    )


async def _member(ctx: Any, user_id: int) -> MemberOut:
    member = await service.get_member(ctx.session_factory, user_id)
    if member is None:
        raise ApiError(404, "not_found", "Member not found")
    return _out(member)


@router.get("", response_model=MemberPage)
async def list_members(admin: Editor, ctx: Ctx, page: PageParams) -> MemberPage:
    members, total = await service.list_members(ctx.session_factory, limit=page.limit, offset=page.offset)
    return MemberPage(items=[_out(m) for m in members], total=total, limit=page.limit, offset=page.offset)


@router.get("/{user_id}", response_model=MemberOut)
async def get_member(user_id: int, admin: Editor, ctx: Ctx) -> MemberOut:
    return await _member(ctx, user_id)


@router.post("/{user_id}/extend", response_model=MemberOut)
async def extend(user_id: int, body: ExtendIn, admin: Editor, ctx: Ctx) -> MemberOut:
    try:
        await service.extend(
            ctx.session_factory, ctx.panels, ctx.settings, user_id, body.days, admin.tg_id, source=SOURCE, ip=admin.ip
        )
    except service.MemberNotFound:
        raise ApiError(404, "not_found", "Member not found") from None
    return await _member(ctx, user_id)


async def _pause(ctx: Any, admin: AdminPrincipal, user_id: int, paused: bool) -> MemberOut:
    try:
        await service.set_paused(ctx.session_factory, ctx.panels, ctx.settings, user_id, paused, admin.tg_id)
    except service.MemberNotFound:
        raise ApiError(404, "not_found", "Member not found") from None
    return await _member(ctx, user_id)


@router.post("/{user_id}/pause", response_model=MemberOut)
async def pause(user_id: int, admin: Editor, ctx: Ctx) -> MemberOut:
    return await _pause(ctx, admin, user_id, True)


@router.post("/{user_id}/resume", response_model=MemberOut)
async def resume(user_id: int, admin: Editor, ctx: Ctx) -> MemberOut:
    return await _pause(ctx, admin, user_id, False)


@router.post("/{user_id}/limits", response_model=MemberOut)
async def limits(user_id: int, body: LimitsIn, admin: Editor, ctx: Ctx) -> MemberOut:
    try:
        await service.set_limits(
            ctx.session_factory,
            ctx.panels,
            ctx.settings,
            user_id,
            admin.tg_id,
            device_limit=body.device_limit,
            traffic_limit_gb=body.traffic_limit_gb,
            source=SOURCE,
            ip=admin.ip,
        )
    except service.MemberNotFound:
        raise ApiError(404, "not_found", "Member not found") from None
    return await _member(ctx, user_id)


@router.post("/{user_id}/reset-devices", response_model=ResetOut)
async def reset_devices(user_id: int, admin: Editor, ctx: Ctx) -> ResetOut:
    try:
        result = await service.reset_devices(
            ctx.session_factory, ctx.panels, user_id, admin.tg_id, source=SOURCE, ip=admin.ip
        )
    except service.MemberNotFound:
        raise ApiError(404, "not_found", "Member not found") from None
    except devices.DeviceError as exc:
        raise ApiError(409, "not_supported", str(exc)) from None
    return ResetOut(reset=result.reset, failed=result.failed)


@router.post("/{user_id}/rotate-link", response_model=MemberOut)
async def rotate_link(user_id: int, admin: Editor, ctx: Ctx) -> MemberOut:
    try:
        await service.rotate_link(ctx.session_factory, user_id, admin.tg_id, source=SOURCE, ip=admin.ip)
    except service.MemberNotFound:
        raise ApiError(404, "not_found", "Member not found") from None
    return await _member(ctx, user_id)


@router.delete("/{user_id}", response_model=RemoveOut)
async def remove(user_id: int, admin: Editor, ctx: Ctx) -> RemoveOut:
    if await service.get_member(ctx.session_factory, user_id) is None:
        raise ApiError(404, "not_found", "Member not found")
    try:
        complete = await service.remove(
            ctx.session_factory, ctx.panels, ctx.settings, user_id, admin.tg_id, source=SOURCE, ip=admin.ip
        )
    except service.MemberNotFound:
        complete = True
    return RemoveOut(complete=complete)
