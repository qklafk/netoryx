"""Administrators and web sessions."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models import AdminRole


class AdminOut(BaseModel):
    tg_id: int
    role: AdminRole
    note: str | None
    added_by: int | None
    created_at: datetime | None
    from_env: bool
    username: str | None
    first_name: str | None
    active_sessions: int


class AdminIn(BaseModel):
    tg_id: int = Field(gt=0)
    role: AdminRole
    note: str | None = Field(default=None, max_length=128)


class AdminUpdate(BaseModel):
    role: AdminRole | None = None
    note: str | None = Field(default=None, max_length=128)
    clear_note: bool = False


class SessionOut(BaseModel):
    id: int
    admin_id: int
    ip: str | None
    user_agent: str | None
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    current: bool


class RoleInfo(BaseModel):
    role: AdminRole
    permissions: list[str]
