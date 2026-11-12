"""Web admin API of the blacklist (mounted under ``/api/admin/blacklist``)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.permissions import Permission
from app.modules.admin_api.deps import Actor, bad_request, require
from app.modules.blacklist.service import refresh_and_apply
from app.services import blacklist

router = APIRouter(tags=["blacklist"])
Viewer = Annotated[Actor, Depends(require(Permission.USERS_VIEW))]
Manager = Annotated[Actor, Depends(require(Permission.USERS_VIEW, Permission.SETTINGS))]


class SourceOut(BaseModel):
    url: str
    configured: bool
    count: int
    skipped: int
    truncated: bool
    fetched_at: datetime | None
    checked_at: datetime | None
    error: str | None
    error_at: datetime | None


class StatusOut(BaseModel):
    enabled: bool
    action: str
    ban_paying: bool
    refresh_minutes: int
    refreshed_at: datetime | None
    total_ids: int
    manual_ids: int
    banned_by_list: int
    exempt: int
    flagged: int
    flagged_ids: list[int]
    sources: list[SourceOut]


class RefreshOut(BaseModel):
    updated: int
    unchanged: int
    failed: int
    total_ids: int
    banned: int
    unbanned: int
    flagged: int
    errors: dict[str, str]


@router.get("/status", response_model=StatusOut)
async def get_status(actor: Viewer) -> Any:
    """Sources with their size, last download and error, plus how many users the list affects."""
    ctx = actor.ctx
    return await blacklist.status(ctx.session_factory, ctx.settings)


@router.post("/refresh", response_model=RefreshOut)
async def refresh_now(actor: Manager) -> RefreshOut:
    """Download every list now and apply the policy."""
    ctx = actor.ctx
    if not ctx.settings.blacklist_enabled:
        raise bad_request("disabled", "The blacklist is switched off")
    refreshed, applied = await refresh_and_apply(ctx)
    async with ctx.session_factory.begin() as session:
        await actor.audit(session, "blacklist.refresh", None, None, updated=refreshed.updated, failed=refreshed.failed)
    return RefreshOut(
        updated=refreshed.updated,
        unchanged=refreshed.unchanged,
        failed=refreshed.failed,
        total_ids=refreshed.total_ids,
        banned=len(applied.banned),
        unbanned=len(applied.unbanned),
        flagged=len(applied.new_flagged),
        errors=refreshed.errors,
    )
