"""Plans: list with per-plan statistics, create, edit, archive, reorder, delete unused."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.db.models import Payment, PaymentStatus, Plan, PromoCode, Purchase, Subscription, SubscriptionStatus
from app.modules.admin_api.deps import Actor, bad_request, conflict, not_found, require
from app.modules.admin_api.schemas.common import Ok
from app.modules.admin_api.schemas.plans import PlanIn, PlanItem, PlanOut, PlanStats, PlanUpdate, ReorderIn
from app.services import plans
from app.services.stats import PAID_PURCHASE_KINDS
from app.utils.time import utcnow
from app.web.deps import DbSession, ReadDb

router = APIRouter(prefix="/plans", tags=["admin: plans"])

Viewer = Annotated[Actor, Depends(require(Permission.USERS_VIEW))]
Editor = Annotated[Actor, Depends(require(Permission.PLANS))]

_SERVICE_FIELDS = (
    "title",
    "duration_days",
    "price_kop",
    "price_stars",
    "device_limit",
    "traffic_limit_gb",
    "availability_rule",
    "allowed_user_ids",
    "is_active",
    "sort_order",
)
_FLAG_FIELDS = ("includes_tg_proxy", "is_hidden")


async def _stats(session: AsyncSession) -> dict[int, PlanStats]:
    now = utcnow()
    active: dict[int, int] = {
        int(plan_id): int(n)
        for plan_id, n in (
            await session.execute(
                select(Subscription.plan_id, func.count(Subscription.id))
                .where(
                    Subscription.plan_id.is_not(None),
                    Subscription.status == SubscriptionStatus.ACTIVE,
                    Subscription.expires_at > now,
                )
                .group_by(Subscription.plan_id)
            )
        ).all()
        if plan_id is not None
    }
    sold = {
        int(plan_id): (int(n), int(kop))
        for plan_id, n, kop in (
            await session.execute(
                select(Purchase.plan_id, func.count(Purchase.id), func.coalesce(func.sum(Purchase.price_kop), 0))
                .where(Purchase.plan_id.is_not(None), Purchase.kind.in_(PAID_PURCHASE_KINDS))
                .group_by(Purchase.plan_id)
            )
        ).all()
        if plan_id is not None
    }
    ids = set(active) | set(sold)
    return {
        pid: PlanStats(
            active_subscriptions=active.get(pid, 0),
            purchases=sold.get(pid, (0, 0))[0],
            revenue_kop=sold.get(pid, (0, 0))[1],
        )
        for pid in ids
    }


def _item(actor: Actor, plan: Plan, stats: dict[int, PlanStats]) -> PlanItem:
    settings = actor.ctx.settings
    username = getattr(actor.ctx, "bot_username", "")
    return PlanItem(
        plan=PlanOut.model_validate(plan),
        effective_stars=plans.stars_price(plan, settings),
        monthly_price_kop=plans.monthly_price_kop(plan),
        deep_link=f"https://t.me/{username}?start={plans.link_payload(actor.ctx.secret_box, plan.id)}"
        if username
        else None,
        stats=stats.get(plan.id, PlanStats(active_subscriptions=0, purchases=0, revenue_kop=0)),
    )


@router.get("", response_model=list[PlanItem])
async def list_plans(actor: Viewer, session: ReadDb, include_archived: bool = True) -> list[PlanItem]:
    """All plans in display order (archived ones too unless ``include_archived=false``)."""
    rows = await (plans.list_all(session) if include_archived else plans.list_active(session))
    stats = await _stats(session)
    return [_item(actor, plan, stats) for plan in rows]


async def _get(session: AsyncSession, plan_id: int) -> Plan:
    plan = await plans.get(session, plan_id)
    if plan is None:
        raise not_found("Plan not found")
    return plan


@router.get("/{plan_id}", response_model=PlanItem)
async def get_plan(plan_id: int, actor: Viewer, session: ReadDb) -> PlanItem:
    return _item(actor, await _get(session, plan_id), await _stats(session))


@router.post("", response_model=PlanItem, status_code=201)
async def create_plan(body: PlanIn, actor: Editor, session: DbSession) -> PlanItem:
    try:
        plan = await plans.create(
            session,
            title=body.title,
            duration_days=body.duration_days,
            price_kop=body.price_kop,
            device_limit=body.device_limit,
            traffic_limit_gb=body.traffic_limit_gb,
            price_stars=body.price_stars,
            sort_order=body.sort_order,
            is_active=body.is_active,
            availability_rule=body.availability_rule,
            allowed_user_ids=body.allowed_user_ids,
        )
    except ValueError as exc:
        raise bad_request("invalid_plan", str(exc)) from None
    plan.includes_tg_proxy = body.includes_tg_proxy
    plan.is_hidden = body.is_hidden
    await session.flush()
    await actor.audit(session, "plan.create", "plan", plan.id, **body.model_dump())
    return _item(actor, plan, {})


@router.patch("/{plan_id}", response_model=PlanItem)
async def update_plan(plan_id: int, body: PlanUpdate, actor: Editor, session: DbSession) -> PlanItem:
    """Change plan fields. Existing subscriptions keep their limits; new purchases get the new ones."""
    plan = await _get(session, plan_id)
    data = body.model_dump(exclude_unset=True)
    clear_stars = data.pop("clear_price_stars", False)
    fields: dict[str, Any] = {k: v for k, v in data.items() if k in _SERVICE_FIELDS and v is not None}
    if clear_stars:
        fields["price_stars"] = None
    flags = {k: v for k, v in data.items() if k in _FLAG_FIELDS and v is not None}
    if not fields and not flags:
        raise bad_request("nothing_to_change", "Nothing to change")
    previous = {key: getattr(plan, key) for key in (*fields, *flags)}
    try:
        await plans.update(session, plan, **fields)
    except ValueError as exc:
        raise bad_request("invalid_plan", str(exc)) from None
    for key, value in flags.items():
        setattr(plan, key, value)
    await session.flush()
    await actor.audit(session, "plan.update", "plan", plan.id, previous=previous, new={**fields, **flags})
    return _item(actor, plan, await _stats(session))


@router.post("/{plan_id}/archive", response_model=PlanItem)
async def archive_plan(plan_id: int, actor: Editor, session: DbSession) -> PlanItem:
    """Hide the plan from users (kept for history and existing payments)."""
    plan = await plans.archive(session, await _get(session, plan_id))
    await actor.audit(session, "plan.archive", "plan", plan.id)
    return _item(actor, plan, await _stats(session))


@router.post("/{plan_id}/restore", response_model=PlanItem)
async def restore_plan(plan_id: int, actor: Editor, session: DbSession) -> PlanItem:
    plan = await plans.update(session, await _get(session, plan_id), is_active=True)
    await actor.audit(session, "plan.restore", "plan", plan.id)
    return _item(actor, plan, await _stats(session))


@router.post("/reorder", response_model=list[PlanItem])
async def reorder_plans(body: ReorderIn, actor: Editor, session: DbSession) -> list[PlanItem]:
    """Set the display order: plans listed first come first; unlisted plans keep their relative order after them."""
    if len(set(body.plan_ids)) != len(body.plan_ids):
        raise bad_request("duplicate_ids", "Plan ids must be unique")
    all_plans = await plans.list_all(session)
    by_id = {p.id: p for p in all_plans}
    unknown = [pid for pid in body.plan_ids if pid not in by_id]
    if unknown:
        raise bad_request("unknown_plan", f"Unknown plans: {unknown}")
    ordered = [by_id[pid] for pid in body.plan_ids] + [p for p in all_plans if p.id not in set(body.plan_ids)]
    for index, plan in enumerate(ordered):
        plan.sort_order = index
    await session.flush()
    await actor.audit(session, "plan.reorder", "plan", None, order=[p.id for p in ordered])
    stats = await _stats(session)
    return [_item(actor, plan, stats) for plan in ordered]


@router.delete("/{plan_id}", response_model=Ok)
async def delete_plan(plan_id: int, actor: Editor, session: DbSession) -> Ok:
    """Delete a plan that was never used (otherwise archive it)."""
    plan = await _get(session, plan_id)
    used = await session.scalar(
        select(func.count()).select_from(
            select(Subscription.id)
            .where(Subscription.plan_id == plan_id)
            .union_all(
                select(Purchase.id).where(Purchase.plan_id == plan_id),
                select(PromoCode.id).where(PromoCode.plan_id == plan_id),
                select(Payment.id).where(and_(Payment.plan_id == plan_id, Payment.status != PaymentStatus.CANCELED)),
            )
            .subquery()
        )
    )
    if used:
        raise conflict("plan_in_use", "The plan has subscriptions, purchases or payments; archive it instead")
    await session.delete(plan)
    await actor.audit(session, "plan.delete", "plan", plan_id, title=plan.title)
    return Ok()
