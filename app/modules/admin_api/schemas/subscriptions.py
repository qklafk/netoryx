"""Subscriptions list and bulk actions."""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models import SubscriptionStatus


class SubFilter(enum.StrEnum):
    ALL = "all"
    ACTIVE = "active"
    EXPIRED = "expired"
    DISABLED = "disabled"


class SubListItem(BaseModel):
    id: int
    user_id: int
    username: str | None
    first_name: str | None
    status: SubscriptionStatus
    active: bool
    plan_id: int | None
    expires_at: datetime
    days_left: int
    is_trial: bool
    is_complimentary: bool
    auto_renew: bool
    device_limit: int
    traffic_limit_gb: int


class CompensationIn(BaseModel):
    days: int = Field(ge=1, le=365)
    include_trials: bool = True
    include_complimentary: bool = True
    reason: str = Field(min_length=3, max_length=300)
    dry_run: bool = Field(default=False, description="Only count the subscriptions that would be extended")
    request_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_\-]{8,48}$",
        description="Client-generated id: a repeated request with the same id is refused instead of applied twice",
    )


class CompensationOut(BaseModel):
    affected: int
    dry_run: bool
    started_jobs: list[str] = Field(default_factory=list)


class BulkResyncIn(BaseModel):
    panel_id: int | None = Field(default=None, description="Only this panel (all active panels when empty)")


class BulkResyncOut(BaseModel):
    marked: int
    started_jobs: list[str] = Field(default_factory=list)
