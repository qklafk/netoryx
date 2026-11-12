"""Backups."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class BackupOut(BaseModel):
    name: str
    size: int
    created_at: datetime
    kind: str  # "logical" | "sqlite"
    encrypted: bool


class BackupsOut(BaseModel):
    directory: str
    keep: int
    scheduled: bool
    schedule_time: str | None
    encrypted_by_default: bool
    items: list[BackupOut]


class RestoreIn(BaseModel):
    name: str = Field(min_length=8, max_length=128)
    password: str | None = Field(default=None, max_length=1024, description="For encrypted backups")
    confirm: Literal["RESTORE"] = Field(description='Type "RESTORE": all current data is replaced')


class RestoreOut(BaseModel):
    kind: str
    rows: int
    tables: dict[str, int]
    skipped_tables: list[str]
    wiped: bool
    relogin_required: bool = True
    safety_backup: str | None = None  # copy of the data as it was right before the restore
    restart_recommended: bool = False  # the backup uses another secret key: restart the bot's other processes
