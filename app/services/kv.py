"""Runtime key/value store (``kv`` table): maintenance flag, generated secrets, bot-editable settings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, insert, select, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KeyValue
from app.utils.security import random_token

if TYPE_CHECKING:
    from app.config import Settings
    from app.db.session import SessionFactory

SECRET_KEY = "secret_key"
MAINTENANCE_KEY = "maintenance"
MAX_KEY_LENGTH = 64


def _check_key(key: str) -> None:
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ValueError(f"kv key must be 1..{MAX_KEY_LENGTH} characters")


async def get(session: AsyncSession, key: str, *, fresh: bool = False) -> str | None:
    """Value stored under ``key`` or ``None``.

    ``fresh=True`` uses a shared locking read, which also sees rows committed by concurrent transactions after
    this transaction's snapshot (MySQL REPEATABLE READ) - use it after losing an insert race.
    """
    _check_key(key)
    stmt = select(KeyValue.value).where(KeyValue.key == key)
    if fresh:
        stmt = stmt.with_for_update(read=True)
    return await session.scalar(stmt)


async def set(session: AsyncSession, key: str, value: str) -> None:
    """Insert or overwrite ``key`` (portable upsert: UPDATE, else INSERT in a SAVEPOINT)."""
    _check_key(key)
    upd = update(KeyValue).where(KeyValue.key == key).values(value=value)
    upd = upd.execution_options(synchronize_session=False)
    if cast(CursorResult[Any], await session.execute(upd)).rowcount:
        return
    if await _insert_if_absent(session, key, value):
        return
    await session.execute(upd)  # a concurrent writer inserted it first: overwrite


async def add(session: AsyncSession, key: str, value: str) -> bool:
    """Insert ``key`` only if absent (SAVEPOINT + ``IntegrityError``); False if it already exists.

    Prefer this over :func:`set` for keys that are new by construction: ``set``'s UPDATE of a missing key takes
    an InnoDB gap lock, and two transactions inserting different new keys into one gap deadlock.
    """
    _check_key(key)
    return await _insert_if_absent(session, key, value)


async def delete(session: AsyncSession, key: str) -> bool:
    """Remove ``key``; True if it existed."""
    _check_key(key)
    stmt = sa_delete(KeyValue).where(KeyValue.key == key).execution_options(synchronize_session=False)
    return bool(cast(CursorResult[Any], await session.execute(stmt)).rowcount)


async def _insert_if_absent(session: AsyncSession, key: str, value: str) -> bool:
    try:
        async with session.begin_nested():
            await session.execute(insert(KeyValue).values(key=key, value=value))
    except IntegrityError:
        return False
    return True


async def get_or_create_secret(session_factory: SessionFactory, settings: Settings | None = None) -> str:
    """The application secret: ``SECRET_KEY`` if configured, else a random one persisted in the DB.

    Safe to call concurrently from several processes: the first insert wins and everyone returns it.
    """
    if settings is not None and settings.secret_key is not None:
        configured = settings.secret_key.get_secret_value()
        if configured:
            return configured
    async with session_factory.begin() as session:
        existing = await get(session, SECRET_KEY)
        if existing:
            return existing
        candidate = random_token(32)
        if await _insert_if_absent(session, SECRET_KEY, candidate):
            return candidate
        winner = await get(session, SECRET_KEY, fresh=True)
    if not winner:  # pragma: no cover - only if the row vanished between insert and read
        raise RuntimeError("failed to persist the generated secret key")
    return winner


async def is_maintenance(session: AsyncSession) -> bool:
    """True while maintenance mode is on (non-admins are blocked by middleware)."""
    return await get(session, MAINTENANCE_KEY) == "1"


async def set_maintenance(session: AsyncSession, on: bool) -> None:
    """Turn maintenance mode on or off."""
    await set(session, MAINTENANCE_KEY, "1" if on else "0")
