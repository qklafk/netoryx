"""System: audit log, jobs, version, logs, maintenance, feature modules, Telegram webhook."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class AuditOut(BaseModel):
    id: int
    actor_id: int
    action: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any] | None
    source: str
    ip: str | None
    created_at: datetime


class AuditPage(BaseModel):
    items: list[AuditOut]
    next_before_id: int | None


class JobOut(BaseModel):
    id: str
    module: str
    enabled: bool
    running: bool
    next_run_at: datetime | None
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_duration_ms: int | None
    last_ok: bool | None
    last_error: str | None
    last_error_at: datetime | None


class JobsOut(BaseModel):
    available: bool  # the scheduler runs in this process
    jobs: list[JobOut]


class RunJobOut(BaseModel):
    started: bool


class VersionOut(BaseModel):
    version: str
    python: str
    started_at: datetime | None
    uptime_sec: int | None
    role: str | None
    mode: str
    database: str | None
    migration: str | None
    latest_version: str | None = None
    update_available: bool | None = None
    release_url: str | None = None
    checked_at: datetime | None = None
    check_error: str | None = None


class LogsOut(BaseModel):
    available: bool
    lines: list[str]


class WindowOut(BaseModel):
    id: str
    starts_at: datetime
    ends_at: datetime
    announce_minutes: int
    message_ru: str | None
    message_en: str | None
    announced: bool
    started: bool


class MaintenanceOut(BaseModel):
    active: bool
    enabled: bool
    message_ru: str | None
    message_en: str | None
    eta: datetime | None
    allowlist: list[int]
    disabled_modules: list[str]
    started_at: datetime | None
    started_by: int | None
    windows: list[WindowOut]
    pending_notices: int


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("the time needs a timezone")
    return value


class MaintenanceEnableIn(BaseModel):
    message_ru: str | None = Field(default=None, max_length=1000)
    message_en: str | None = Field(default=None, max_length=1000)
    eta: datetime | None = None

    @field_validator("eta")
    @classmethod
    def _check_eta(cls, value: datetime | None) -> datetime | None:
        return _aware(value)


class AllowlistIn(BaseModel):
    user_ids: list[int] = Field(default_factory=list, max_length=1000)


class ModulesIn(BaseModel):
    modules: list[str] = Field(default_factory=list, max_length=100)


class WindowIn(BaseModel):
    starts_at: datetime
    ends_at: datetime
    announce_minutes: int = Field(default=60, ge=0, le=7 * 24 * 60)
    message_ru: str | None = Field(default=None, max_length=1000)
    message_en: str | None = Field(default=None, max_length=1000)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _check(cls, value: datetime) -> datetime:
        _aware(value)
        return value


class ModuleOut(BaseModel):
    name: str
    title: str
    enabled: bool
    required: bool
    shop_only: bool
    switched_off: bool  # in the disabled modules setting
    maintenance: bool  # paused by a partial maintenance
    admin_menu: list[dict[str, Any]]


class ModuleToggleIn(BaseModel):
    enabled: bool


class WebhookOut(BaseModel):
    mode: str
    url: str | None = None
    pending_update_count: int | None = None
    last_error_date: datetime | None = None
    last_error_message: str | None = None
    max_connections: int | None = None
    ip_address: str | None = None
    error: str | None = None
