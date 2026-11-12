"""Custom main menu buttons."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.modules.admin_api.schemas.common import OrmModel
from app.services.menu_buttons import MAX_ROW, MAX_SORT_ORDER, MAX_TITLE

Kind = Literal["url", "webapp", "copy_text", "text_screen"]
Audience = Literal["all", "subscribers", "non_subscribers", "admins"]


class MenuButtonOut(OrmModel):
    id: int
    kind: Kind
    title: str
    titles: dict[str, str]
    payload: str
    row: int
    sort_order: int
    audience: Audience
    is_active: bool
    created_at: datetime
    updated_at: datetime


class MenuButtonIn(BaseModel):
    kind: Kind
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    titles: dict[str, str] = Field(default_factory=dict, max_length=32)
    payload: str = Field(min_length=1, max_length=4096)
    row: int = Field(default=0, ge=0, le=MAX_ROW)
    sort_order: int | None = Field(default=None, ge=0, le=MAX_SORT_ORDER)
    audience: Audience = "all"
    is_active: bool = True


class MenuButtonUpdate(BaseModel):
    kind: Kind | None = None
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)
    titles: dict[str, str] | None = Field(default=None, max_length=32)
    payload: str | None = Field(default=None, min_length=1, max_length=4096)
    row: int | None = Field(default=None, ge=0, le=MAX_ROW)
    sort_order: int | None = Field(default=None, ge=0, le=MAX_SORT_ORDER)
    audience: Audience | None = None
    is_active: bool | None = None


class Position(BaseModel):
    id: int
    row: int = Field(ge=0, le=MAX_ROW)
    sort_order: int = Field(ge=0, le=MAX_SORT_ORDER)


class ReorderIn(BaseModel):
    items: list[Position] = Field(min_length=1, max_length=200)


class MoveIn(BaseModel):
    direction: Literal["up", "down"]


class BuiltinButton(BaseModel):
    key: str
    hidden: bool


class BuiltinsIn(BaseModel):
    hidden: list[str] = Field(default_factory=list, max_length=32)
