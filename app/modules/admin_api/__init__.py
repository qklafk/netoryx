"""Web admin API: every section of the web admin under ``/api/admin/*`` (session cookie + CSRF, per-endpoint
permissions, audit with the client IP)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.core.modules import FeatureModule
from app.modules.admin_api.deps import drain_background
from app.modules.admin_api.routers import SECTION_ROUTERS
from app.web.auth import admin_user

API_PREFIX = "/api/admin"


def build_router() -> APIRouter:
    """All sections under ``/api/admin``; every route needs a logged-in admin (and CSRF for mutations)."""
    router = APIRouter(prefix=API_PREFIX, dependencies=[Depends(admin_user())])
    for section in SECTION_ROUTERS:
        router.include_router(section)
    return router


async def _on_shutdown(_: Any) -> None:
    await drain_background(5.0)


module = FeatureModule(
    name="admin_api",
    title_ru="Веб-админка",
    title_en="Web admin",
    enabled=lambda s: s.web_admin_enabled,
    web_routers=[build_router()],
    on_shutdown=[_on_shutdown],
    order=5,
)
