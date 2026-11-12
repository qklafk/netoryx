"""Owner-controlled import of existing accounts from a Marzban panel."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.permissions import Permission
from app.modules.admin_api.deps import Actor, bad_request, require
from app.modules.admin_api.schemas.imports import ImportApplyIn, ImportApplyOut, ImportPreviewOut
from app.services import imports
from app.services.panels.base import PanelError
from app.web.deps import Ctx
from app.web.errors import ApiError

router = APIRouter(prefix="/panels", tags=["Panel import"])
Owner = Annotated[Actor, Depends(require(Permission.PANELS, Permission.SETTINGS_SECRETS))]


@router.post("/{panel_id}/import/preview", response_model=ImportPreviewOut)
async def preview(panel_id: int, ctx: Ctx, actor: Owner) -> ImportPreviewOut:
    try:
        result = await imports.preview(ctx.session_factory, ctx.panels, panel_id)
    except imports.ImportError as exc:
        raise bad_request("import_unavailable", str(exc)) from None
    except PanelError:
        raise ApiError(502, "panel_unavailable", "Could not read the source panel") from None
    return ImportPreviewOut.model_validate(result)


@router.post("/{panel_id}/import/apply", response_model=ImportApplyOut)
async def apply(panel_id: int, body: ImportApplyIn, ctx: Ctx, actor: Owner) -> ImportApplyOut:
    if len({item.username for item in body.items}) != len(body.items):
        raise bad_request("duplicate_accounts", "Choose each source account only once")
    try:
        result = await imports.apply(
            ctx.session_factory,
            ctx.panels,
            ctx.settings,
            ctx.secret_box,
            panel_id,
            [imports.Selection(item.username, item.user_id, item.label) for item in body.items],
            body.request_id,
            bot_username=ctx.bot_username,
        )
    except imports.ImportError as exc:
        raise bad_request("import_unavailable", str(exc)) from None
    except PanelError:
        raise ApiError(502, "panel_unavailable", "Could not read the source panel") from None
    async with ctx.session_factory.begin() as session:
        await actor.audit(
            session,
            "panel.import",
            "panel",
            panel_id,
            imported=result["imported"],
            staged=result["staged"],
            failed=len(result["errors"]),
        )
    return ImportApplyOut.model_validate(result)
