"""Promo codes, gift codes and ad campaigns."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.db.models import PromoKind
from app.modules.admin_api.schemas.common import OrmModel
from app.utils.time import MAX_PERIOD_DAYS


class PromoOut(OrmModel):
    id: int
    code: str
    kind: PromoKind
    bonus_kop: int
    bonus_days: int
    plan_id: int | None
    max_uses: int
    uses: int
    only_new_users: bool
    created_by: int | None
    expires_at: datetime | None
    is_active: bool
    created_at: datetime


class PromoItem(BaseModel):
    promo: PromoOut
    activations: int
    deep_link: str | None
    redeemed_by: int | None = None  # gifts: who redeemed it


class PromoIn(BaseModel):
    code: str | None = Field(default=None, min_length=3, max_length=64, description="Random when empty")
    bonus_kop: int = Field(default=0, ge=0, le=100_000_000_00)
    bonus_days: int = Field(default=0, ge=0, le=MAX_PERIOD_DAYS)
    max_uses: int = Field(default=1, ge=0, le=10_000_000, description="0 = unlimited")
    only_new_users: bool = False
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at needs a timezone")
        return value


class ActivationOut(BaseModel):
    user_id: int
    username: str | None
    created_at: datetime


class CampaignOut(BaseModel):
    code: str
    name: str
    is_active: bool
    created_at: datetime
    created_by: int | None
    deep_link: str | None
    registrations: int
    trials: int
    paying_users: int
    purchases: int
    revenue_kop: int
    conversion: float


class CampaignIn(BaseModel):
    code: str = Field(min_length=2, max_length=32)
    name: str = Field(default="", max_length=64)


class ActiveIn(BaseModel):
    is_active: bool


class PromoGroupOut(OrmModel):
    id: int
    title: str
    discount_percent: int
    expires_at: datetime | None
    is_active: bool
    created_by: int | None
    created_at: datetime
    members: int = 0


class PromoGroupIn(BaseModel):
    title: str = Field(min_length=1, max_length=64)
    discount_percent: int = Field(ge=1, le=99)
    expires_at: datetime | None = None
    user_ids: list[int] = Field(default_factory=list, max_length=10_000)

    @field_validator("expires_at")
    @classmethod
    def _group_expiry_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at needs a timezone")
        return value

    @field_validator("user_ids")
    @classmethod
    def _unique_users(cls, value: list[int]) -> list[int]:
        if any(isinstance(item, bool) or item <= 0 for item in value) or len(set(value)) != len(value):
            raise ValueError("user_ids must contain unique positive Telegram IDs")
        return value


class PromoGroupPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=64)
    discount_percent: int | None = Field(default=None, ge=1, le=99)
    expires_at: datetime | None = None
    is_active: bool | None = None
    user_ids: list[int] | None = Field(default=None, max_length=10_000)

    @field_validator("expires_at")
    @classmethod
    def _group_patch_expiry_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at needs a timezone")
        return value

    @field_validator("user_ids")
    @classmethod
    def _patch_unique_users(cls, value: list[int] | None) -> list[int] | None:
        invalid = value is not None and (
            any(isinstance(item, bool) or item <= 0 for item in value) or len(set(value)) != len(value)
        )
        if invalid:
            raise ValueError("user_ids must contain unique positive Telegram IDs")
        return value
