"""Referral program overview, partners and payout requests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.db.models import WithdrawalStatus
from app.modules.admin_api.schemas.common import OrmModel


class LevelTotal(BaseModel):
    level: int
    rewards: int
    amount_kop: int
    days: int


class PartnerBrief(BaseModel):
    tg_id: int
    username: str | None
    first_name: str | None
    referrals: int


class ReferralOverview(BaseModel):
    enabled: bool
    mode: str
    levels_percent: list[float]
    withdrawals_enabled: bool
    referred_users: int
    levels: list[LevelTotal]
    partner_balance_total_kop: int
    pending_withdrawals: int
    pending_withdrawal_kop: int
    paid_out_kop: int
    top_partners: list[PartnerBrief]


class LevelStatsOut(BaseModel):
    level: int
    users: int
    paid_users: int
    earned_kop: int
    earned_days: int
    rewards: int


class PartnerStatsOut(BaseModel):
    user_id: int
    levels: list[LevelStatsOut]
    total_users: int
    total_paid_users: int
    total_earned_kop: int
    total_earned_days: int
    bonus_kop: int
    bonus_days: int
    referral_balance_kop: int
    pending_withdrawal_kop: int
    withdrawn_kop: int


class WithdrawalOut(OrmModel):
    id: int
    user_id: int
    amount_kop: int
    method: str
    details: str
    status: WithdrawalStatus
    admin_id: int | None
    admin_comment: str | None
    created_at: datetime
    processed_at: datetime | None


class FlagOut(BaseModel):
    code: str
    detail: dict[str, Any]


class WithdrawalItem(BaseModel):
    withdrawal: WithdrawalOut
    username: str | None
    first_name: str | None
    flags: list[FlagOut]


class DecisionIn(BaseModel):
    comment: str | None = Field(default=None, max_length=512)
    notify: bool = True
