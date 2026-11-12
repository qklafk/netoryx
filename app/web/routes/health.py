"""``GET /healthz``: liveness/readiness for Docker and uptime monitors (no secrets, counts only)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import case, func, select, text

from app.db.models import Panel
from app.db.session import read_session
from app.web.deps import Ctx

log = logging.getLogger(__name__)

DB_TIMEOUT_SEC = 3.0

router = APIRouter(include_in_schema=False)


async def _check_db(ctx: Any) -> tuple[bool, dict[str, int] | None]:
    try:
        async with asyncio.timeout(DB_TIMEOUT_SEC), read_session(ctx.session_factory) as session:
            await session.execute(text("SELECT 1"))
            healthy = case((Panel.consecutive_failures == 0, 1), else_=0)
            row = (
                await session.execute(
                    select(func.count(), func.coalesce(func.sum(healthy), 0)).where(Panel.is_active.is_(True))
                )
            ).one()
    except Exception as exc:
        log.warning("healthz: database check failed: %s", type(exc).__name__)
        return False, None
    return True, {"total": int(row[0]), "healthy": int(row[1])}


@router.get("/healthz")
async def healthz(ctx: Ctx) -> JSONResponse:
    db_ok, panels = await _check_db(ctx)
    bot_ok = bool(getattr(ctx, "bot_username", None))
    started_at: datetime | None = getattr(ctx, "started_at", None)
    uptime = int((datetime.now(UTC) - started_at).total_seconds()) if started_at else None
    if not db_ok:
        status = "error"
    elif not bot_ok or (panels and panels["healthy"] < panels["total"]):
        status = "degraded"
    else:
        status = "ok"
    body = {"status": status, "db": db_ok, "bot": bot_ok, "panels": panels, "uptime_sec": uptime}
    return JSONResponse(body, status_code=200 if db_ok else 503, headers={"Cache-Control": "no-store"})
