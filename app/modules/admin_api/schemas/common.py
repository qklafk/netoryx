"""Schemas shared by several sections of the web admin API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.web.deps import Page


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Ok(BaseModel):
    ok: bool = True


class Count(BaseModel):
    count: int


class JobKick(BaseModel):
    """Background jobs started right away to apply a change (empty when the scheduler runs elsewhere)."""

    started_jobs: list[str] = Field(default_factory=list)


class Message(BaseModel):
    ok: bool = True
    detail: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


__all__ = ["Count", "JobKick", "Message", "Ok", "OrmModel", "Page"]
