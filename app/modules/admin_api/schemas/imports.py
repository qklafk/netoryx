"""Import preview, explicit ownership mapping and adoption results."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ImportPreviewItem(BaseModel):
    username: str
    status: str
    expires_at: datetime | None
    traffic_limit_bytes: int
    used_bytes: int
    subscription_url: str | None
    user_id: int | None
    subscription_id: int | None
    supported: bool
    reason: str | None


class ImportPreviewOut(BaseModel):
    items: list[ImportPreviewItem]
    total: int


class ImportSelection(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    user_id: int | None = Field(default=None, gt=0)
    label: str | None = Field(default=None, min_length=1, max_length=64)


class ImportApplyIn(BaseModel):
    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    items: list[ImportSelection] = Field(min_length=1, max_length=1000)


class ImportResultItem(BaseModel):
    username: str
    user_id: int | None
    subscription_id: int | None
    claim_url: str | None
    already_imported: bool


class ImportFailure(BaseModel):
    username: str
    message: str


class ImportApplyOut(BaseModel):
    imported: int
    staged: int
    items: list[ImportResultItem]
    errors: list[ImportFailure]
