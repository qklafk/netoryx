"""Product mode switch (personal <-> shop)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.services.mode_switch import MemberPolicy, Mode


class CheckItemOut(BaseModel):
    key: str
    ok: bool
    required: bool
    detail: str


class ReadinessOut(BaseModel):
    current_mode: str
    ready: bool
    missing: list[str]
    items: list[CheckItemOut]


class SwitchIn(BaseModel):
    target: Mode
    default_policy: MemberPolicy = MemberPolicy.KEEP
    per_user: dict[int, MemberPolicy] = Field(default_factory=dict, description="Member id -> keep / convert")
    grace_until: datetime | None = Field(default=None, description="Converted members keep free access until then")
    notify_users: bool = True

    @field_validator("grace_until")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("grace_until needs a timezone")
        return value


class ApplyIn(SwitchIn):
    force: bool = Field(default=False, description="Switch to shop even if required checklist items are missing")
    confirm: Literal["SWITCH"] = Field(description='Type "SWITCH" to confirm')


class MemberChangeOut(BaseModel):
    user_id: int
    username: str | None
    subscription_id: int
    policy: MemberPolicy
    expires_before: datetime
    expires_after: datetime


class PreviewOut(BaseModel):
    target: Mode
    readiness: ReadinessOut
    settings_changes: dict[str, Any]
    members: list[MemberChangeOut]
    auto_renew_off: int
    notify_users: int


class ApplyOut(BaseModel):
    target: Mode
    converted: int
    auto_renew_disabled: int
    notified: int
    settings_failed: list[str]


class LastSwitchOut(BaseModel):
    available: bool
    target: str | None = None
    at: datetime | None = None
    actor_id: int | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    subscriptions: int = 0
