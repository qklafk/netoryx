"""Users, their subscription and admin actions on them."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.db.models import ProvisionState, SubscriptionStatus, TicketStatus
from app.modules.admin_api.schemas.common import OrmModel
from app.modules.admin_api.schemas.payments import LedgerOut, PaymentOut, PurchaseOut
from app.utils.time import MAX_PERIOD_DAYS


class UserFilter(enum.StrEnum):
    ALL = "all"
    ACTIVE = "active"
    EXPIRED = "expired"
    TRIAL = "trial"
    DISABLED = "disabled"
    COMPLIMENTARY = "complimentary"
    NO_SUBSCRIPTION = "no_subscription"
    NEVER_PAID = "never_paid"
    PAID = "paid"
    BANNED = "banned"
    BLOCKED = "blocked"


class UserSort(enum.StrEnum):
    CREATED = "created_at"
    LAST_SEEN = "last_seen_at"
    BALANCE = "balance"
    EXPIRES = "expires_at"


class UserListItem(BaseModel):
    tg_id: int
    username: str | None
    first_name: str | None
    language: str
    balance_kop: int
    referral_balance_kop: int
    is_banned: bool
    bot_blocked: bool
    created_at: datetime
    last_seen_at: datetime | None
    sub_status: SubscriptionStatus | None = None
    sub_expires_at: datetime | None = None
    sub_is_trial: bool = False
    sub_is_complimentary: bool = False
    sub_plan_id: int | None = None


class UserOut(OrmModel):
    tg_id: int
    username: str | None
    first_name: str | None
    language: str
    balance_kop: int
    referral_balance_kop: int
    referral_code: str
    referred_by: int | None
    trial_used: bool
    receipt_email: str | None
    bot_blocked: bool
    is_banned: bool
    created_at: datetime
    last_seen_at: datetime | None


class ProvisionOut(BaseModel):
    id: int
    panel_id: int
    panel_name: str
    inbound_id: int | None
    inbound_name: str | None
    desired_state: ProvisionState
    actual_state: ProvisionState
    synced_revision: int
    attempts: int
    last_error: str | None
    updated_at: datetime


class SubscriptionOut(BaseModel):
    id: int
    label: str
    status: SubscriptionStatus
    active: bool
    plan_id: int | None
    plan_title: str | None
    expires_at: datetime
    days_left: int
    device_limit: int
    traffic_limit_gb: int
    auto_renew: bool
    is_trial: bool
    is_complimentary: bool
    stars_recurring: bool
    revision: int
    traffic_exhausted: bool
    created_at: datetime
    provisions: list[ProvisionOut] = Field(default_factory=list)


class TicketBrief(OrmModel):
    id: int
    subject: str
    status: TicketStatus
    created_at: datetime
    updated_at: datetime


class ReferralInfo(BaseModel):
    invited: int
    earned_kop: int
    levels: list[int]
    inviter: UserListItem | None


class UserCard(BaseModel):
    user: UserOut
    subscription_url: str | None
    subscription: SubscriptionOut | None
    subscriptions: list[SubscriptionOut] = Field(default_factory=list)
    payments: list[PaymentOut]
    purchases: list[PurchaseOut]
    ledger: list[LedgerOut]
    tickets: list[TicketBrief]
    referrals: ReferralInfo
    campaign: str | None
    is_admin: bool


class BalanceAdjustIn(BaseModel):
    amount_kop: int = Field(description="Positive to credit, negative to charge")
    account: Literal["main", "referral"] = "main"
    reason: str = Field(min_length=3, max_length=300)
    request_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_\-]{8,64}$",
        description="Client-generated id: repeating a request with the same id does not change the balance twice",
    )
    notify: bool = False

    @field_validator("amount_kop")
    @classmethod
    def _non_zero(cls, value: int) -> int:
        if value == 0 or abs(value) > 100_000_000_00:
            raise ValueError("amount must be non-zero and at most 100 000 000 ₽")
        return value


class BalanceOut(BaseModel):
    applied: bool
    balance_kop: int
    referral_balance_kop: int


class DaysIn(BaseModel):
    days: int = Field(ge=1, le=MAX_PERIOD_DAYS)
    plan_id: int | None = Field(default=None, description="Apply this plan's limits")
    notify: bool = False


class RemoveDaysIn(BaseModel):
    days: int = Field(ge=1, le=MAX_PERIOD_DAYS)


class ExpiryIn(BaseModel):
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expires_at needs a timezone")
        return value


class LimitsIn(BaseModel):
    plan_id: int | None = None
    apply_plan_limits: bool = Field(default=False, description="Copy device/traffic limits from the plan")
    device_limit: int | None = Field(default=None, ge=0, le=1000)
    traffic_limit_gb: int | None = Field(default=None, ge=0, le=1_000_000)


class FlagIn(BaseModel):
    value: bool


class BulkBanIn(BaseModel):
    user_ids: list[int] = Field(min_length=1, max_length=10_000)
    value: bool = True
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("user_ids")
    @classmethod
    def _unique_positive_ids(cls, value: list[int]) -> list[int]:
        if any(isinstance(item, bool) or item <= 0 for item in value) or len(set(value)) != len(value):
            raise ValueError("user_ids must contain unique positive Telegram IDs")
        return value


class SubStatusIn(BaseModel):
    status: Literal["active", "disabled"]


class DevicesResetIn(BaseModel):
    device_id: str | None = Field(default=None, max_length=256)
    panel_id: int | None = None


class DeviceOut(BaseModel):
    id: str
    platform: str | None
    model: str | None
    os_version: str | None
    user_agent: str | None
    ip: str | None
    created_at: datetime | None
    last_seen_at: datetime | None = None


class PanelDevicesOut(BaseModel):
    panel_id: int
    panel_name: str
    kind: str
    supported: bool
    can_delete_one: bool
    devices: list[DeviceOut]
    error: str | None


class ResetOut(BaseModel):
    reset: int
    failed: int
    errors: list[str]


class SyncOut(BaseModel):
    found: bool
    synced: int
    failed: int
    skipped: int
    errors: list[str]


class RotateOut(BaseModel):
    subscription_url: str | None


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096, description="Telegram HTML")
    disable_preview: bool = True


class MessageOut(BaseModel):
    delivered: bool


class DeleteOut(BaseModel):
    deleted: bool
    accounts_removed: int
    errors: list[str]
