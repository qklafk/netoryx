"""Public user API key lifecycle; raw keys are returned once and never stored."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PublicApiKey
from app.utils.security import random_token, sha256_hex

PREFIX = "vpb_"


def _hash(token: str) -> str:
    return sha256_hex("public-api:" + token)


async def create_key(session: AsyncSession, user_id: int, name: str) -> tuple[str, PublicApiKey]:
    # A caller may have just registered the owner in this transaction; flush it before the FK row.
    await session.flush()
    token = PREFIX + random_token(32)
    row = PublicApiKey(user_id=user_id, name=name[:64] or "API key", token_hash=_hash(token), token_hint=token[-6:])
    session.add(row)
    await session.flush()
    return token, row


async def authenticate(session: AsyncSession, token: str) -> PublicApiKey | None:
    if not token.startswith(PREFIX) or len(token) > 256:
        return None
    row = await session.scalar(
        select(PublicApiKey).where(PublicApiKey.token_hash == _hash(token), PublicApiKey.revoked_at.is_(None))
    )
    if row is not None:
        row.last_used_at = datetime.now(UTC)
    return row


async def revoke_key(session: AsyncSession, row: PublicApiKey) -> None:
    row.revoked_at = datetime.now(UTC)
