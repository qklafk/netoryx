"""Dashboard: key figures, chart series and the latest events."""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.permissions import Permission
from app.db.models import Event, Panel, Payment, Ticket, TicketStatus, Withdrawal
from app.modules.admin_api.deps import Actor, require
from app.modules.admin_api.schemas.dashboard import (
    DashboardOut,
    EventOut,
    PanelHealth,
    ProviderKpi,
    SeriesOut,
    SeriesPoint,
    TopReferrer,
)
from app.services import stats, usage
from app.services.partner import OPEN_STATUSES as OPEN_WITHDRAWALS
from app.services.payments.service import OPEN_STATUSES as OPEN_PAYMENTS
from app.services.provisioning import is_panel_down
from app.web.deps import ReadDb

router = APIRouter(prefix="/dashboard", tags=["admin: dashboard"])
Viewer = Annotated[Actor, Depends(require(Permission.VIEW_DASHBOARD))]


async def _count(session: ReadDb, stmt: Any) -> int:
    return int(await session.scalar(stmt) or 0)


@router.get("", response_model=DashboardOut)
async def dashboard(actor: Viewer, session: ReadDb) -> DashboardOut:
    """Key figures for the start page."""
    settings = actor.ctx.settings
    data = await stats.dashboard(session, settings)
    panels = list((await session.scalars(select(Panel))).all())
    active = [p for p in panels if p.is_active]
    down = [p.name for p in active if is_panel_down(p, settings)]
    return DashboardOut(
        generated_at=data.generated_at,
        users_total=data.users_total,
        users_new_today=data.users_new_today,
        users_new_7d=data.users_new_7d,
        users_new_30d=data.users_new_30d,
        users_blocked_bot=data.users_blocked_bot,
        subscriptions_active=data.subscriptions_active,
        subscriptions_trial=data.subscriptions_trial,
        subscriptions_paid=data.subscriptions_paid,
        paying_users=data.paying_users,
        revenue_today_kop=data.revenue_today_kop,
        revenue_7d_kop=data.revenue_7d_kop,
        revenue_30d_kop=data.revenue_30d_kop,
        revenue_total_kop=data.revenue_total_kop,
        donations_30d_kop=data.donations_30d_kop,
        donations_total_kop=data.donations_total_kop,
        arppu_kop=data.arppu_kop,
        mrr_kop=data.mrr_kop,
        churned_30d=data.churned_30d,
        churn_rate_30d=data.churn_rate_30d,
        trial_users=data.trial_users,
        trial_converted=data.trial_converted,
        trial_conversion=data.trial_conversion,
        wallet_total_kop=data.wallet_total_kop,
        partner_balance_total_kop=data.partner_balance_total_kop,
        pending_payments=await _count(session, select(func.count(Payment.id)).where(Payment.status.in_(OPEN_PAYMENTS))),
        open_tickets=await _count(session, select(func.count(Ticket.id)).where(Ticket.status == TicketStatus.OPEN)),
        pending_withdrawals=await _count(
            session, select(func.count(Withdrawal.id)).where(Withdrawal.status.in_(OPEN_WITHDRAWALS))
        ),
        panels=PanelHealth(total=len(panels), active=len(active), healthy=len(active) - len(down), down=down),
        providers_30d=[
            ProviderKpi(provider=p.provider, payments=p.payments, amount_kop=p.amount_kop) for p in data.providers_30d
        ],
        top_referrers=[
            TopReferrer(tg_id=u.tg_id, username=u.username, first_name=u.first_name, referrals=n)
            for u, n in data.top_referrers
        ],
    )


@router.get("/series", response_model=SeriesOut)
async def series(
    actor: Viewer,
    session: ReadDb,
    days: Annotated[int, Query(ge=1, le=stats.MAX_SERIES_DAYS)] = 30,
) -> SeriesOut:
    """Per-day chart data: registrations, payments, revenue, active subscriptions and traffic."""
    settings = actor.ctx.settings
    daily = await stats.daily(session, settings, days)
    traffic = dict(await usage.total_daily_usage(session, settings, days=days))
    points = [
        SeriesPoint(
            day=item.day,
            registrations=item.registrations,
            payments=item.payments,
            revenue_kop=item.revenue_kop,
            active_subscriptions=item.active_subscriptions,
            traffic_bytes=traffic.get(item.day.isoformat(), 0),
        )
        for item in daily
    ]
    return SeriesOut(days=days, points=points)


def _event_data(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


@router.get("/events", response_model=list[EventOut])
async def recent_events(
    actor: Viewer,
    session: ReadDb,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    type_prefix: Annotated[str | None, Query(alias="type", max_length=48)] = None,
    before_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[EventOut]:
    """The latest analytics events (registrations, payments, trials...), newest first."""
    stmt = select(Event).order_by(Event.id.desc()).limit(limit)
    if type_prefix:
        stmt = stmt.where(Event.type.startswith(type_prefix, autoescape=True))
    if before_id is not None:
        stmt = stmt.where(Event.id < before_id)
    rows = (await session.scalars(stmt)).all()
    return [
        EventOut(
            id=e.id,
            type=e.type,
            user_id=e.user_id,
            amount_kop=e.amount_kop,
            data=_event_data(e.data),
            created_at=e.created_at,
        )
        for e in rows
    ]


__all__ = ["date", "router"]
