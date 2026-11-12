"""Subscriptions: filtered list, compensation days for everyone after an outage, bulk resync."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, or_, select

from app.core.permissions import Permission
from app.db.models import Panel, Purchase, PurchaseKind, Subscription, SubscriptionStatus, User
from app.db.session import read_session
from app.modules.admin_api.deps import Actor, conflict, kick_reconcile, require
from app.modules.admin_api.schemas.common import Page
from app.modules.admin_api.schemas.subscriptions import (
    BulkResyncIn,
    BulkResyncOut,
    CompensationIn,
    CompensationOut,
    SubFilter,
    SubListItem,
)
from app.services import kv, provisioning
from app.utils.time import days_left, utcnow
from app.web.deps import PageParams, ReadDb

router = APIRouter(prefix="/subscriptions", tags=["admin: subscriptions"])

Viewer = Annotated[Actor, Depends(require(Permission.USERS_VIEW))]
Bulk = Annotated[Actor, Depends(require(Permission.SUBS_BULK))]

BATCH = 500
_REQUEST_KEY = "admin:bulk:{id}"


def _filters(
    status: SubFilter,
    expiring_in_days: int | None,
    trial: bool | None,
    complimentary: bool | None,
    auto_renew: bool | None,
    plan_id: int | None,
) -> list[Any]:
    now = utcnow()
    active = and_(Subscription.status == SubscriptionStatus.ACTIVE, Subscription.expires_at > now)
    where: list[Any] = []
    if status == SubFilter.ACTIVE:
        where.append(active)
    elif status == SubFilter.EXPIRED:
        where.append(
            or_(
                Subscription.status == SubscriptionStatus.EXPIRED,
                and_(Subscription.status == SubscriptionStatus.ACTIVE, Subscription.expires_at <= now),
            )
        )
    elif status == SubFilter.DISABLED:
        where.append(Subscription.status == SubscriptionStatus.DISABLED)
    if expiring_in_days is not None:
        where += [active, Subscription.expires_at <= now + timedelta(days=expiring_in_days)]
    if trial is not None:
        where.append(Subscription.is_trial.is_(trial))
    if complimentary is not None:
        where.append(Subscription.is_complimentary.is_(complimentary))
    if auto_renew is not None:
        where.append(Subscription.auto_renew.is_(auto_renew))
    if plan_id is not None:
        where.append(Subscription.plan_id == plan_id)
    return where


@router.get("", response_model=Page[SubListItem])
async def list_subscriptions(
    actor: Viewer,
    session: ReadDb,
    page: PageParams,
    status: SubFilter = SubFilter.ALL,
    expiring_in_days: Annotated[int | None, Query(ge=0, le=3650)] = None,
    trial: bool | None = None,
    complimentary: bool | None = None,
    auto_renew: bool | None = None,
    plan_id: int | None = None,
) -> Page[SubListItem]:
    """Subscriptions, soonest expiry first."""
    where = _filters(status, expiring_in_days, trial, complimentary, auto_renew, plan_id)
    stmt = select(Subscription, User.username, User.first_name).join(User, User.tg_id == Subscription.user_id)
    if where:
        stmt = stmt.where(*where)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        await session.execute(
            stmt.order_by(Subscription.expires_at, Subscription.id).limit(page.limit).offset(page.offset)
        )
    ).all()
    now = utcnow()
    items = [
        SubListItem(
            id=sub.id,
            user_id=sub.user_id,
            username=username,
            first_name=first_name,
            status=sub.status,
            active=sub.status == SubscriptionStatus.ACTIVE and sub.expires_at > now,
            plan_id=sub.plan_id,
            expires_at=sub.expires_at,
            days_left=days_left(sub.expires_at, now) if sub.expires_at > now else 0,
            is_trial=sub.is_trial,
            is_complimentary=sub.is_complimentary,
            auto_renew=sub.auto_renew,
            device_limit=sub.device_limit,
            traffic_limit_gb=sub.traffic_limit_gb,
        )
        for sub, username, first_name in rows
    ]
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


def _compensation_where(body: CompensationIn, now: Any) -> list[Any]:
    where = [Subscription.status == SubscriptionStatus.ACTIVE, Subscription.expires_at > now]
    if not body.include_trials:
        where.append(Subscription.is_trial.is_(False))
    if not body.include_complimentary:
        where.append(Subscription.is_complimentary.is_(False))
    return where


@router.post("/compensation", response_model=CompensationOut)
async def compensation(body: CompensationIn, actor: Bulk) -> CompensationOut:
    """Add ``days`` to every active subscription (e.g. after an outage). Each one gets a purchase record."""
    ctx = actor.ctx
    now = utcnow()
    where = _compensation_where(body, now)
    if body.dry_run:
        async with read_session(ctx.session_factory) as session:
            count = int(await session.scalar(select(func.count(Subscription.id)).where(*where)) or 0)
        return CompensationOut(affected=count, dry_run=True)
    if body.request_id is not None:
        async with ctx.session_factory.begin() as session:
            if not await kv.add(session, _REQUEST_KEY.format(id=body.request_id), now.isoformat()):
                raise conflict("already_applied", "This compensation was already applied")
    affected = 0
    last_id = 0
    delta = timedelta(days=body.days)
    while True:
        async with ctx.session_factory.begin() as session:
            subs = list(
                (
                    await session.scalars(
                        select(Subscription)
                        .where(*where, Subscription.id > last_id)
                        .order_by(Subscription.id)
                        .limit(BATCH)
                        .with_for_update()
                    )
                ).all()
            )
            if not subs:
                break
            for sub in subs:
                before = sub.expires_at
                sub.expires_at = before + delta
                sub.last_reminder_days = None
                sub.revision += 1
                session.add(
                    Purchase(
                        user_id=sub.user_id,
                        plan_id=sub.plan_id,
                        kind=PurchaseKind.ADMIN_GRANT,
                        price_kop=0,
                        days=body.days,
                        expires_before=before,
                        expires_after=sub.expires_at,
                        actor_id=actor.tg_id,
                    )
                )
            affected += len(subs)
            last_id = subs[-1].id
    async with ctx.session_factory.begin() as session:
        await actor.audit(
            session,
            "subscription.compensation",
            "subscription",
            None,
            days=body.days,
            affected=affected,
            reason=body.reason,
        )
    return CompensationOut(affected=affected, dry_run=False, started_jobs=kick_reconcile(ctx))


@router.post("/resync", response_model=BulkResyncOut)
async def bulk_resync(body: BulkResyncIn, actor: Bulk) -> BulkResyncOut:
    """Re-push every account (of one panel or of all active panels) on the next reconcile run, started now."""
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        if body.panel_id is not None:
            if await session.get(Panel, body.panel_id) is None:
                raise conflict("unknown_panel", "No such panel")
            panel_ids = [body.panel_id]
        else:
            panel_ids = list((await session.scalars(select(Panel.id).where(Panel.is_active.is_(True)))).all())
        marked = 0
        for panel_id in panel_ids:
            marked += await provisioning.mark_dirty(session, panel_id=panel_id)
        await actor.audit(session, "subscription.bulk_resync", "panel", body.panel_id, marked=marked)
    return BulkResyncOut(marked=marked, started_jobs=kick_reconcile(ctx))
