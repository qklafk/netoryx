"""FastAPI dependencies shared by every router: application context, settings, DB sessions, pagination."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.session import read_session
from app.web.errors import ApiError
from app.web.security import client_ip

if TYPE_CHECKING:
    from app.core.context import AppContext


def get_ctx(request: Request) -> AppContext:
    """The application context the web app was created with."""
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:  # pragma: no cover - create_web_app always sets it
        raise ApiError(503, "unavailable", "Application is starting")
    return ctx


def settings_dep(ctx: Annotated[Any, Depends(get_ctx)]) -> Settings:
    """Current runtime settings (re-read on every request, so admin changes apply immediately)."""
    return ctx.settings


async def db_session(ctx: Annotated[Any, Depends(get_ctx)]) -> AsyncIterator[AsyncSession]:
    """A session in a transaction, committed when the endpoint returns (rolled back on error).

    Never do network I/O (Telegram, panels, payment APIs) while holding it; use ``ctx.session_factory`` for
    short transactions around such calls instead.
    """
    async with ctx.session_factory.begin() as session:
        yield session


async def read_db(ctx: Annotated[Any, Depends(get_ctx)]) -> AsyncIterator[AsyncSession]:
    """A read-only session (does not take the SQLite write lock). Never write through it."""
    async with read_session(ctx.session_factory) as session:
        yield session


def request_ip(request: Request, settings: Annotated[Settings, Depends(settings_dep)]) -> str:
    """Client IP honouring ``TRUSTED_PROXIES``."""
    return client_ip(request, settings)


Ctx = Annotated[Any, Depends(get_ctx)]  # AppContext (typed as Any to avoid an import cycle at runtime)
CurrentSettings = Annotated[Settings, Depends(settings_dep)]
# scope="function": the transaction is committed before the response is sent, so a failed commit is a 500.
DbSession = Annotated[AsyncSession, Depends(db_session, scope="function")]
ReadDb = Annotated[AsyncSession, Depends(read_db, scope="function")]
ClientIp = Annotated[str, Depends(request_ip)]


# ---------------------------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------------------------
MAX_PAGE_SIZE = 200


class Pagination(BaseModel):
    limit: int
    offset: int


def pagination(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
) -> Pagination:
    """``?limit=&offset=`` query parameters."""
    return Pagination(limit=limit, offset=offset)


PageParams = Annotated[Pagination, Depends(pagination)]


class Page[T](BaseModel):
    """A page of results: ``{"items": [...], "total": N, "limit": L, "offset": O}``."""

    items: list[T]
    total: int
    limit: int
    offset: int


async def fetch_page(session: AsyncSession, stmt: Select[Any], page: Pagination) -> tuple[list[Any], int]:
    """Run ``stmt`` (an ORM select of one entity or column) for one page; returns ``(rows, total)``."""
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await session.scalars(stmt.limit(page.limit).offset(page.offset))).all()
    return list(rows), int(total or 0)
