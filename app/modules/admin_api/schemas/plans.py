"""Plans (tariffs)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.modules.admin_api.schemas.common import OrmModel
from app.utils.time import MAX_PERIOD_DAYS


class PlanOut(OrmModel):
    id: int
    title: str
    duration_days: int
    price_kop: int
    price_stars: int | None
    device_limit: int
    traffic_limit_gb: int
    includes_tg_proxy: bool
    is_hidden: bool
    availability_rule: Literal["all", "new", "existing", "invited", "allowlist"]
    allowed_user_ids: list[int]
    is_active: bool
    sort_order: int
    created_at: datetime


class PlanStats(BaseModel):
    active_subscriptions: int
    purchases: int
    revenue_kop: int


class PlanItem(BaseModel):
    plan: PlanOut
    effective_stars: int
    monthly_price_kop: int
    deep_link: str | None
    stats: PlanStats


class PlanIn(BaseModel):
    title: str = Field(min_length=1, max_length=64)
    duration_days: int = Field(ge=1, le=MAX_PERIOD_DAYS)
    price_kop: int = Field(ge=0)
    price_stars: int | None = Field(default=None, ge=1)
    device_limit: int = Field(default=3, ge=0, le=1000)
    traffic_limit_gb: int = Field(default=0, ge=0, le=1_000_000)
    includes_tg_proxy: bool = True
    is_hidden: bool = False
    availability_rule: Literal["all", "new", "existing", "invited", "allowlist"] = "all"
    allowed_user_ids: list[int] = Field(default_factory=list, max_length=10_000)
    is_active: bool = True
    sort_order: int | None = None


class PlanUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=64)
    duration_days: int | None = Field(default=None, ge=1, le=MAX_PERIOD_DAYS)
    price_kop: int | None = Field(default=None, ge=0)
    price_stars: int | None = Field(default=None, ge=1)
    clear_price_stars: bool = Field(default=False, description="Derive the Stars price from the ruble price again")
    device_limit: int | None = Field(default=None, ge=0, le=1000)
    traffic_limit_gb: int | None = Field(default=None, ge=0, le=1_000_000)
    includes_tg_proxy: bool | None = None
    is_hidden: bool | None = None
    availability_rule: Literal["all", "new", "existing", "invited", "allowlist"] | None = None
    allowed_user_ids: list[int] | None = Field(default=None, max_length=10_000)
    is_active: bool | None = None
    sort_order: int | None = None


class ReorderIn(BaseModel):
    plan_ids: list[int] = Field(min_length=1, max_length=500, description="Plan ids in the new display order")
