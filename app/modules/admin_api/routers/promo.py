"""Promo codes, gift codes and ad campaigns (deep links with funnel statistics)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.core.permissions import Permission
from app.db.models import PromoActivation, PromoCode, PromoGroup, PromoGroupMember, PromoKind, User
from app.modules.admin_api.deps import Actor, bad_request, conflict, not_found, require
from app.modules.admin_api.schemas.common import Page
from app.modules.admin_api.schemas.promo import (
    ActivationOut,
    ActiveIn,
    CampaignIn,
    CampaignOut,
    PromoGroupIn,
    PromoGroupOut,
    PromoGroupPatch,
    PromoIn,
    PromoItem,
    PromoOut,
)
from app.services import campaigns, promo
from app.web.deps import DbSession, PageParams, ReadDb

router = APIRouter(tags=["admin: promo"])

Promo = Annotated[Actor, Depends(require(Permission.PROMO))]


async def _group_out(session: Any, group: PromoGroup) -> PromoGroupOut:
    members = int(
        await session.scalar(
            select(func.count()).select_from(PromoGroupMember).where(PromoGroupMember.group_id == group.id)
        )
        or 0
    )
    return PromoGroupOut.model_validate(group).model_copy(update={"members": members})


async def _require_group_users(session: Any, user_ids: list[int]) -> None:
    if not user_ids:
        return
    found = set(await session.scalars(select(User.tg_id).where(User.tg_id.in_(user_ids))))
    missing = sorted(set(user_ids) - found)
    if missing:
        raise bad_request("unknown_users", f"Unknown Telegram user IDs: {', '.join(map(str, missing[:10]))}")


async def _group(session: Any, group_id: int) -> PromoGroup:
    group = await session.get(PromoGroup, group_id)
    if group is None:
        raise not_found("Promo group not found")
    return group


# ---------------------------------------------------------------------------------------------
# Personal discount groups
# ---------------------------------------------------------------------------------------------
@router.get("/promo-groups", response_model=list[PromoGroupOut])
async def list_groups(actor: Promo, session: ReadDb) -> list[PromoGroupOut]:
    """Audience groups with their member count; a buyer receives their best active group discount."""
    groups = list((await session.scalars(select(PromoGroup).order_by(PromoGroup.id.desc()))).all())
    return [await _group_out(session, group) for group in groups]


@router.post("/promo-groups", response_model=PromoGroupOut, status_code=201)
async def create_group(body: PromoGroupIn, actor: Promo, session: DbSession) -> PromoGroupOut:
    await _require_group_users(session, body.user_ids)
    group = PromoGroup(
        title=body.title.strip(),
        discount_percent=body.discount_percent,
        expires_at=body.expires_at,
        created_by=actor.tg_id,
    )
    try:
        session.add(group)
        await session.flush()
        session.add_all(PromoGroupMember(group_id=group.id, user_id=user_id) for user_id in body.user_ids)
        await session.flush()
    except IntegrityError:
        raise conflict("group_exists", "A promo group with this title already exists") from None
    await actor.audit(
        session, "promo_group.create", "promo_group", group.id, title=group.title, members=len(body.user_ids)
    )
    return await _group_out(session, group)


@router.patch("/promo-groups/{group_id}", response_model=PromoGroupOut)
async def update_group(group_id: int, body: PromoGroupPatch, actor: Promo, session: DbSession) -> PromoGroupOut:
    group = await _group(session, group_id)
    fields = body.model_dump(exclude_unset=True, exclude={"user_ids"})
    if "title" in fields:
        fields["title"] = fields["title"].strip()
    try:
        for name, value in fields.items():
            setattr(group, name, value)
        if body.user_ids is not None:
            await _require_group_users(session, body.user_ids)
            await session.execute(delete(PromoGroupMember).where(PromoGroupMember.group_id == group.id))
            session.add_all(PromoGroupMember(group_id=group.id, user_id=user_id) for user_id in body.user_ids)
        await session.flush()
    except IntegrityError:
        raise conflict("group_exists", "A promo group with this title already exists") from None
    await actor.audit(session, "promo_group.update", "promo_group", group.id, **fields)
    return await _group_out(session, group)


@router.delete("/promo-groups/{group_id}", status_code=204)
async def delete_group(group_id: int, actor: Promo, session: DbSession) -> None:
    group = await _group(session, group_id)
    await actor.audit(session, "promo_group.delete", "promo_group", group.id, title=group.title)
    await session.delete(group)


def _link(actor: Actor, payload: str) -> str | None:
    username = getattr(actor.ctx, "bot_username", "")
    return f"https://t.me/{username}?start={payload}" if username else None


async def _activations(session: Any, ids: list[int]) -> dict[int, tuple[int, int | None]]:
    if not ids:
        return {}
    rows = await session.execute(
        select(PromoActivation.promo_id, func.count(PromoActivation.id), func.min(PromoActivation.user_id))
        .where(PromoActivation.promo_id.in_(ids))
        .group_by(PromoActivation.promo_id)
    )
    return {int(pid): (int(n), uid) for pid, n, uid in rows}


def _item(actor: Actor, code: PromoCode, stats: dict[int, tuple[int, int | None]]) -> PromoItem:
    count, first_user = stats.get(code.id, (0, None))
    prefix = "gift_" if code.kind == PromoKind.GIFT else "promo_"
    return PromoItem(
        promo=PromoOut.model_validate(code),
        activations=count,
        deep_link=_link(actor, prefix + code.code),
        redeemed_by=first_user if code.kind == PromoKind.GIFT else None,
    )


@router.get("/promo", response_model=Page[PromoItem])
async def list_codes(
    actor: Promo,
    session: ReadDb,
    page: PageParams,
    kind: PromoKind = PromoKind.PROMO,
    active: bool | None = None,
    q: Annotated[str | None, Query(max_length=64)] = None,
) -> Page[PromoItem]:
    """Promo codes (``kind=promo``) or gift codes bought by users (``kind=gift``), newest first."""
    stmt = select(PromoCode).where(PromoCode.kind == kind)
    if active is not None:
        stmt = stmt.where(PromoCode.is_active.is_(active))
    if q:
        stmt = stmt.where(PromoCode.code.contains(promo.normalize_code(q), autoescape=True))
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list((await session.scalars(stmt.order_by(PromoCode.id.desc()).limit(page.limit).offset(page.offset))).all())
    stats = await _activations(session, [r.id for r in rows])
    return Page(items=[_item(actor, r, stats) for r in rows], total=total, limit=page.limit, offset=page.offset)


@router.post("/promo", response_model=PromoItem, status_code=201)
async def create_code(body: PromoIn, actor: Promo, session: DbSession) -> PromoItem:
    """Create a promo code with a balance bonus and/or free days."""
    try:
        code = await promo.create_promo(
            session,
            code=body.code,
            bonus_kop=body.bonus_kop,
            bonus_days=body.bonus_days,
            max_uses=body.max_uses,
            only_new_users=body.only_new_users,
            expires_at=body.expires_at,
            created_by=actor.tg_id,
        )
    except promo.PromoCodeExists:
        raise conflict("code_exists", "This code already exists") from None
    except ValueError as exc:
        raise bad_request("invalid_promo", str(exc)) from None
    await actor.audit(
        session,
        "promo.create",
        "promo",
        code.id,
        code=code.code,
        bonus_kop=code.bonus_kop,
        bonus_days=code.bonus_days,
        max_uses=code.max_uses,
    )
    return _item(actor, code, {})


async def _code(session: Any, promo_id: int) -> PromoCode:
    code = await session.get(PromoCode, promo_id)
    if code is None:
        raise not_found("Code not found")
    return code


@router.get("/promo/{promo_id}/activations", response_model=Page[ActivationOut])
async def activations(promo_id: int, actor: Promo, session: ReadDb, page: PageParams) -> Page[ActivationOut]:
    """Who used the code."""
    await _code(session, promo_id)
    stmt = (
        select(PromoActivation.user_id, User.username, PromoActivation.created_at)
        .outerjoin(User, User.tg_id == PromoActivation.user_id)
        .where(PromoActivation.promo_id == promo_id)
    )
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (await session.execute(stmt.order_by(PromoActivation.id.desc()).limit(page.limit).offset(page.offset))).all()
    return Page(
        items=[ActivationOut(user_id=u, username=n, created_at=c) for u, n, c in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.put("/promo/{promo_id}/active", response_model=PromoItem)
async def set_active(promo_id: int, body: ActiveIn, actor: Promo, session: DbSession) -> PromoItem:
    """Switch a code (promo or gift) off or back on; activations made so far stay."""
    code = await _code(session, promo_id)
    if body.is_active:
        code.is_active = True
    else:
        await promo.deactivate(session, code)
    await actor.audit(session, "promo.set_active", "promo", promo_id, code=code.code, active=body.is_active)
    return _item(actor, code, await _activations(session, [code.id]))


# ---------------------------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------------------------
def _campaign_out(actor: Actor, item: campaigns.Campaign, stat: campaigns.CampaignStats | None) -> CampaignOut:
    return CampaignOut(
        code=item.code,
        name=item.name,
        is_active=item.is_active,
        created_at=item.created_at,
        created_by=item.created_by,
        deep_link=_link(actor, campaigns.deep_link_payload(item.code)),
        registrations=stat.registrations if stat else 0,
        trials=stat.trials if stat else 0,
        paying_users=stat.paying_users if stat else 0,
        purchases=stat.purchases if stat else 0,
        revenue_kop=stat.revenue_kop if stat else 0,
        conversion=stat.conversion if stat else 0.0,
    )


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(actor: Promo, session: ReadDb, since: datetime | None = None) -> list[CampaignOut]:
    """Ad campaigns with registrations, trials, paying users and revenue (optionally since a date)."""
    items = await campaigns.list_campaigns(session)
    stats = {s.code: s for s in await campaigns.stats(session, since=since)}
    return [_campaign_out(actor, item, stats.get(item.code)) for item in items]


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def create_campaign(body: CampaignIn, actor: Promo, session: DbSession) -> CampaignOut:
    try:
        item = await campaigns.create(session, body.code, body.name, actor.tg_id, source="web", ip=actor.ip)
    except campaigns.CampaignExists:
        raise conflict("campaign_exists", "A campaign with this code exists") from None
    except ValueError as exc:
        raise bad_request("invalid_campaign", str(exc)) from None
    return _campaign_out(actor, item, None)


@router.put("/campaigns/{code}/active", response_model=CampaignOut)
async def set_campaign_active(code: str, body: ActiveIn, actor: Promo, session: DbSession) -> CampaignOut:
    item = await campaigns.set_active(session, code, body.is_active, actor.tg_id, source="web", ip=actor.ip)
    if item is None:
        raise not_found("Campaign not found")
    stats = await campaigns.stats(session, code=item.code)
    return _campaign_out(actor, item, stats[0] if stats else None)
