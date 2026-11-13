"""Web admin API of the shared Telegram link (``/api/admin/tg_only``): the page, the link, a QR code, new link."""

from __future__ import annotations

import base64
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.permissions import Permission
from app.modules.admin_api.deps import Actor, require
from app.modules.admin_api.schemas.common import Ok
from app.services import tg_only
from app.services.qr import make_qr_png_async

router = APIRouter(tags=["admin: telegram only"])

Manager = Annotated[Actor, Depends(require(Permission.PANELS))]


class ServerOut(BaseModel):
    panel_id: int
    title: str
    country: str | None
    ready: bool
    error: str | None


class SkippedOut(BaseModel):
    panel_id: int
    name: str
    reason: str  # country | unknown_country | no_inbound | no_endpoint
    country: str | None


class TgOnlyOut(BaseModel):
    enabled: bool
    url: str  # the raw subscription link
    page: str  # the public landing page
    qr_png_base64: str
    ready: bool
    servers: list[ServerOut]
    skipped: list[SkippedOut]
    excluded_countries: list[str]


async def _view(actor: Actor) -> TgOnlyOut:
    ctx = actor.ctx
    view = await tg_only.admin_view(ctx.session_factory, ctx.settings)
    png = await make_qr_png_async(view.url) if view.enabled and view.url.startswith("http") else b""
    return TgOnlyOut(
        enabled=view.enabled,
        url=view.url,
        page=view.page,
        qr_png_base64=base64.b64encode(png).decode("ascii"),
        ready=view.ready,
        servers=[ServerOut.model_validate(s, from_attributes=True) for s in view.servers],
        skipped=[
            SkippedOut(panel_id=s.panel_id, name=s.panel_name, reason=s.reason, country=s.country) for s in view.skipped
        ],
        excluded_countries=sorted(ctx.settings.tg_only_excluded_countries),
    )


@router.get("", response_model=TgOnlyOut)
async def get_view(actor: Manager) -> TgOnlyOut:
    """The public page, the subscription link and the state of each server."""
    return await _view(actor)


@router.post("/rotate", response_model=TgOnlyOut)
async def rotate(actor: Manager) -> TgOnlyOut:
    """Issue a new link and new client ids; the old link and the old ids stop working."""
    ctx = actor.ctx
    await tg_only.rotate(ctx.session_factory, ctx.panels, ctx.settings)
    async with ctx.session_factory.begin() as session:
        await actor.audit(session, "tg_only.rotate")
    return await _view(actor)


@router.post("/sync", response_model=Ok)
async def sync(actor: Manager) -> Ok:
    """Apply the rules and the client on every eligible server now."""
    ctx = actor.ctx
    await tg_only.reconcile(ctx.session_factory, ctx.panels, ctx.settings)
    return Ok()
