"""Bot texts editor."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TextOut(BaseModel):
    key: str
    module: str
    placeholders: list[str]
    default: dict[str, str | None]  # language -> built-in text
    override: dict[str, str | None]  # language -> admin text
    effective: dict[str, str | None]


class TextIn(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


class PreviewIn(BaseModel):
    value: str | None = Field(
        default=None, max_length=4096, description="Unsaved text to check; the stored one if empty"
    )
    values: dict[str, str] = Field(default_factory=dict, description="Sample values for placeholders")


class PreviewOut(BaseModel):
    ok: bool
    rendered: str | None = None
    error: str | None = None
    placeholders: list[str] = Field(default_factory=list)


class ModuleTexts(BaseModel):
    module: str
    keys: int
    overridden: int
