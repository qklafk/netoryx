"""Broadcasts: compose, audience, schedule, progress."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.db.models import BroadcastStatus
from app.services.broadcasts import MAX_CUSTOM_IDS, Audience, MediaType


class AudienceIn(BaseModel):
    kind: Audience = Audience.ALL
    days: int | None = Field(default=None, ge=1, le=365, description="expiring_in_days: within this many days")
    language: str | None = Field(default=None, max_length=8)
    plan_id: int | None = None
    panel_id: int | None = None
    location_id: int | None = None
    user_ids: list[int] = Field(default_factory=list, max_length=MAX_CUSTOM_IDS)


class ButtonIn(BaseModel):
    text: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=2048)


class BroadcastIn(BaseModel):
    audience: AudienceIn = Field(default_factory=AudienceIn)
    text: str | None = Field(default=None, max_length=4096, description="Telegram HTML (caption when media is set)")
    buttons: list[list[ButtonIn]] = Field(default_factory=list)
    media_type: MediaType | None = None
    media: str | None = Field(
        default=None, max_length=2048, description="file_id from /broadcasts/media or an HTTPS URL"
    )
    disable_preview: bool = True
    scheduled_at: datetime | None = None

    @field_validator("scheduled_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("scheduled_at needs a timezone")
        return value


class BroadcastOut(BaseModel):
    id: int
    created_by: int
    status: BroadcastStatus
    audience: dict[str, Any]
    text: str | None
    buttons: list[list[dict[str, str]]]
    media_type: MediaType | None
    media: str | None
    copied_message: bool  # composed in the bot (a message is copied)
    disable_preview: bool
    scheduled_at: datetime | None
    total: int
    sent: int
    failed: int
    blocked: int
    progress: float
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class CountOut(BaseModel):
    recipients: int


class MediaOut(BaseModel):
    media_type: MediaType
    media: str


class ScheduleIn(BaseModel):
    scheduled_at: datetime | None = Field(description="Null clears the schedule")

    @field_validator("scheduled_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("scheduled_at needs a timezone")
        return value


class PreviewOut(BaseModel):
    delivered: bool
