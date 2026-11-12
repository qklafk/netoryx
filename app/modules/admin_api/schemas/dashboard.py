"""Dashboard: KPIs, chart series and recent events."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class ProviderKpi(BaseModel):
    provider: str
    payments: int
    amount_kop: int


class TopReferrer(BaseModel):
    tg_id: int
    username: str | None
    first_name: str | None
    referrals: int


class PanelHealth(BaseModel):
    total: int
    active: int
    healthy: int
    down: list[str] = Field(default_factory=list)


class DashboardOut(BaseModel):
    generated_at: datetime
    users_total: int
    users_new_today: int
    users_new_7d: int
    users_new_30d: int
    users_blocked_bot: int
    subscriptions_active: int
    subscriptions_trial: int
    subscriptions_paid: int
    paying_users: int
    revenue_today_kop: int
    revenue_7d_kop: int
    revenue_30d_kop: int
    revenue_total_kop: int
    donations_30d_kop: int
    donations_total_kop: int
    arppu_kop: int
    mrr_kop: int
    churned_30d: int
    churn_rate_30d: float
    trial_users: int
    trial_converted: int
    trial_conversion: float
    wallet_total_kop: int
    partner_balance_total_kop: int
    pending_payments: int
    open_tickets: int
    pending_withdrawals: int
    panels: PanelHealth
    providers_30d: list[ProviderKpi]
    top_referrers: list[TopReferrer]


class SeriesPoint(BaseModel):
    day: date
    registrations: int
    payments: int
    revenue_kop: int
    active_subscriptions: int
    traffic_bytes: int


class SeriesOut(BaseModel):
    days: int
    points: list[SeriesPoint]


class EventOut(BaseModel):
    id: int
    type: str
    user_id: int | None
    amount_kop: int | None
    data: dict[str, Any] | None
    created_at: datetime
