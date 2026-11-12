"""Small, user-scoped public REST API (never an admin API)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header
from pydantic import BaseModel
from sqlalchemy import select

from app.db.models import User
from app.services import public_api
from app.web.deps import DbSession
from app.web.errors import ApiError

router = APIRouter(prefix="/api/public", tags=["public-api"])


class PublicMe(BaseModel):
    telegram_id: int
    username: str | None
    balance_kop: int
    language: str


def _token(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    scheme, _, value = (authorization or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


@router.get("/me", response_model=PublicMe)
async def me(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> PublicMe:
    key = await public_api.authenticate(session, _token(authorization, x_api_key) or "")
    if key is None:
        raise ApiError(401, "invalid_api_key", "Invalid API key")
    user = await session.scalar(select(User).where(User.tg_id == key.user_id))
    if user is None or user.is_banned:
        raise ApiError(401, "invalid_api_key", "Invalid API key")
    return PublicMe(
        telegram_id=user.tg_id, username=user.username, balance_kop=user.balance_kop, language=user.language
    )
