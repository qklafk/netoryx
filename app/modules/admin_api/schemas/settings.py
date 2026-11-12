"""Runtime settings (registry-driven)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SettingOut(BaseModel):
    key: str
    group: str
    type: str
    label: str
    help: str
    choices: list[str] | None
    min: float | None
    max: float | None
    nullable: bool
    secret: bool
    owner_only: bool
    restart_required: bool
    editable: bool  # the current admin may change it
    value: Any = None  # never set for secrets
    is_set: bool  # secrets: a value exists
    origin: Literal["env", "db", "default"]


class GroupOut(BaseModel):
    key: str
    title: str
    settings: list[SettingOut]


class SettingIn(BaseModel):
    value: Any = Field(description="New value (JSON type of the setting); null for nullable settings")


class EnvValueOut(BaseModel):
    key: str
    value: Any = None
    is_set: bool
    hidden: bool
