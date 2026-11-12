"""Admin audit log: every admin mutation (bot or web) records who did what to whom."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminAuditLog

REDACTED = "***"
# Detail keys whose values are never stored: matched as substrings of the key lower-cased with "_" and "-"
# removed, so "api_key", "X-Api-Key", "apiKey" and "privateKey" (3x-ui Reality settings) all match.
_SECRET_MARKERS = (
    "password",
    "passwd",
    "passphrase",
    "pwd",
    "secret",
    "token",
    "apikey",
    "cookie",
    "authorization",
    "credential",
    "privatekey",
)


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("_", "").replace("-", "")
    return any(marker in normalized for marker in _SECRET_MARKERS)


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return redact(value)
    if isinstance(value, list | tuple):
        return [_redact_value(item) for item in value]
    return value


def redact(details: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of ``details`` with secret-looking values replaced by ``"***"`` (nested mappings and lists included)."""
    clean: dict[str, Any] = {}
    for key, value in details.items():
        if _is_secret_key(str(key)):
            clean[key] = REDACTED if value not in (None, "") else value
        else:
            clean[key] = _redact_value(value)
    return clean


async def log(
    session: AsyncSession,
    actor_id: int,
    action: str,
    target_type: str | None = None,
    target_id: int | str | None = None,
    source: str = "bot",
    ip: str | None = None,
    **details: Any,
) -> AdminAuditLog:
    """Write an audit record in the caller's transaction.

    ``action`` is a dotted verb such as ``"user.balance_adjust"``; ``details`` are stored as JSON with
    secrets redacted.
    """
    if not action:
        raise ValueError("audit action is required")
    entry = AdminAuditLog(
        actor_id=actor_id,
        action=action[:64],
        target_type=target_type[:32] if target_type else None,
        target_id=None if target_id is None else str(target_id)[:64],
        details=json.dumps(redact(details), ensure_ascii=False, sort_keys=True, default=str) if details else None,
        source=source[:16],
        ip=ip[:64] if ip else None,
    )
    session.add(entry)
    return entry


async def recent(
    session: AsyncSession,
    *,
    limit: int = 50,
    before_id: int | None = None,
    actor_id: int | None = None,
    target_type: str | None = None,
    target_id: int | str | None = None,
) -> list[AdminAuditLog]:
    """Newest-first page of audit records, optionally filtered. Paginate with ``before_id``."""
    stmt = select(AdminAuditLog).order_by(AdminAuditLog.id.desc()).limit(max(1, min(limit, 500)))
    if before_id is not None:
        stmt = stmt.where(AdminAuditLog.id < before_id)
    if actor_id is not None:
        stmt = stmt.where(AdminAuditLog.actor_id == actor_id)
    if target_type is not None:
        stmt = stmt.where(AdminAuditLog.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(AdminAuditLog.target_id == str(target_id))
    return list((await session.scalars(stmt)).all())
