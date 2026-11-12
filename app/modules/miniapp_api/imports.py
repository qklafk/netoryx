"""Bind an operator-issued one-time claim link to the authenticated Telegram account."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.modules.miniapp_api import deps
from app.services import imports
from app.services.panels.base import PanelError
from app.web.auth import MiniAppUser
from app.web.deps import Ctx
from app.web.errors import ApiError

router = APIRouter(tags=["Mini App: import"])


class ClaimIn(BaseModel):
    token: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class ClaimOut(BaseModel):
    subscription_id: int


@router.post("/import/claim", response_model=ClaimOut)
async def claim(body: ClaimIn, request: Request, ctx: Ctx, user: MiniAppUser) -> ClaimOut:
    await deps.rate_limit(request, ctx, user, "import_claim", 10, 600)
    try:
        sub_id = await imports.claim(
            ctx.session_factory, ctx.settings, ctx.secret_box, user.tg_id, body.token, registry=ctx.panels
        )
    except imports.ImportError as exc:
        raise ApiError(409, "claim_unavailable", str(exc)) from None
    except PanelError:
        # The panel could not be reached; the link stays valid and can be tried again later.
        raise ApiError(503, "panel_unavailable", "the server is temporarily unavailable") from None
    return ClaimOut(subscription_id=sub_id)
