"""Referral program: totals per level, top partners, partner statistics and the payout queue."""

from __future__ import annotations

import html
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.core.permissions import Permission
from app.db.models import ReferralReward, User, Withdrawal, WithdrawalStatus
from app.modules.admin_api import notify
from app.modules.admin_api.deps import Actor, bad_request, conflict, not_found, require
from app.modules.admin_api.schemas.common import Page
from app.modules.admin_api.schemas.referral import (
    DecisionIn,
    FlagOut,
    LevelStatsOut,
    LevelTotal,
    PartnerBrief,
    PartnerStatsOut,
    ReferralOverview,
    WithdrawalItem,
    WithdrawalOut,
)
from app.services import partner, referrals
from app.web.deps import PageParams, ReadDb

router = APIRouter(tags=["admin: referral"])

Referral = Annotated[Actor, Depends(require(Permission.REFERRAL))]
Payouts = Annotated[Actor, Depends(require(Permission.WITHDRAWALS))]


@router.get("/referral", response_model=ReferralOverview)
async def overview(actor: Referral, session: ReadDb) -> ReferralOverview:
    """How the program performs: rewards per level, partner balances, payouts and the top inviters.

    The program itself (mode, levels, bonuses, payouts) is configured in the settings, group ``referral``.
    """
    settings = actor.ctx.settings
    rows = (
        await session.execute(
            select(
                ReferralReward.level,
                func.count(ReferralReward.id),
                func.coalesce(func.sum(ReferralReward.amount_kop), 0),
                func.coalesce(func.sum(ReferralReward.days), 0),
            )
            .group_by(ReferralReward.level)
            .order_by(ReferralReward.level)
        )
    ).all()
    by_status = {
        WithdrawalStatus(status): (int(n), int(kop))
        for status, n, kop in (
            await session.execute(
                select(
                    Withdrawal.status, func.count(Withdrawal.id), func.coalesce(func.sum(Withdrawal.amount_kop), 0)
                ).group_by(Withdrawal.status)
            )
        ).all()
    }
    pending = [by_status.get(s, (0, 0)) for s in partner.OPEN_STATUSES]
    top = await referrals.top(session, 10)
    return ReferralOverview(
        enabled=settings.referral_enabled,
        mode=str(settings.referral_reward_mode),
        levels_percent=[float(p) for p in settings.referral_levels_percent],
        withdrawals_enabled=settings.withdrawals_enabled,
        referred_users=int(
            await session.scalar(select(func.count(User.tg_id)).where(User.referred_by.is_not(None))) or 0
        ),
        levels=[
            LevelTotal(level=int(lvl), rewards=int(n), amount_kop=int(kop), days=int(days))
            for lvl, n, kop, days in rows
        ],
        partner_balance_total_kop=int(
            await session.scalar(select(func.coalesce(func.sum(User.referral_balance_kop), 0))) or 0
        ),
        pending_withdrawals=sum(n for n, _ in pending),
        pending_withdrawal_kop=sum(kop for _, kop in pending),
        paid_out_kop=by_status.get(WithdrawalStatus.PAID, (0, 0))[1],
        top_partners=[
            PartnerBrief(tg_id=u.tg_id, username=u.username, first_name=u.first_name, referrals=n) for u, n in top
        ],
    )


@router.get("/referral/partners/{user_id}", response_model=PartnerStatsOut)
async def partner_stats(user_id: int, actor: Referral, session: ReadDb) -> PartnerStatsOut:
    """A partner's tree and earnings per level."""
    if await session.get(User, user_id) is None:
        raise not_found("User not found")
    stats = await partner.partner_stats(session, actor.ctx.settings, user_id)
    return PartnerStatsOut(
        user_id=user_id,
        levels=[
            LevelStatsOut(
                level=lvl.level,
                users=lvl.users,
                paid_users=lvl.paid_users,
                earned_kop=lvl.earned_kop,
                earned_days=lvl.earned_days,
                rewards=lvl.rewards,
            )
            for lvl in stats.levels
        ],
        total_users=stats.total_users,
        total_paid_users=stats.total_paid_users,
        total_earned_kop=stats.total_earned_kop,
        total_earned_days=stats.total_earned_days,
        bonus_kop=stats.bonus_kop,
        bonus_days=stats.bonus_days,
        referral_balance_kop=stats.referral_balance_kop,
        pending_withdrawal_kop=stats.pending_withdrawal_kop,
        withdrawn_kop=stats.withdrawn_kop,
    )


# ---------------------------------------------------------------------------------------------
# Withdrawals
# ---------------------------------------------------------------------------------------------
async def _items(session: Any, rows: list[Withdrawal], with_flags: bool, settings: Any) -> list[WithdrawalItem]:
    users = {
        u.tg_id: u for u in (await session.scalars(select(User).where(User.tg_id.in_({w.user_id for w in rows})))).all()
    }
    items = []
    for w in rows:
        user = users.get(w.user_id)
        flags = await partner.fraud_flags(session, w.user_id, settings=settings) if with_flags else []
        items.append(
            WithdrawalItem(
                withdrawal=WithdrawalOut.model_validate(w),
                username=user.username if user else None,
                first_name=user.first_name if user else None,
                flags=[FlagOut(code=str(f.code), detail=dict(f.detail)) for f in flags],
            )
        )
    return items


@router.get("/withdrawals", response_model=Page[WithdrawalItem])
async def list_withdrawals(
    actor: Payouts,
    session: ReadDb,
    page: PageParams,
    status: WithdrawalStatus | None = None,
    open_only: bool = True,
    user_id: int | None = None,
) -> Page[WithdrawalItem]:
    """Payout requests: the open queue (oldest first, with anti-fraud flags) or the history."""
    stmt = select(Withdrawal)
    if status is not None:
        stmt = stmt.where(Withdrawal.status == status)
    elif open_only:
        stmt = stmt.where(Withdrawal.status.in_(partner.OPEN_STATUSES))
    if user_id is not None:
        stmt = stmt.where(Withdrawal.user_id == user_id)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    order = Withdrawal.id.asc() if open_only and status is None else Withdrawal.id.desc()
    rows = list((await session.scalars(stmt.order_by(order).limit(page.limit).offset(page.offset))).all())
    with_flags = open_only or status in partner.OPEN_STATUSES
    items = await _items(session, rows, with_flags, actor.ctx.settings)
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


async def _decide(actor: Actor, withdrawal_id: int, body: DecisionIn, action: str) -> WithdrawalItem:
    ctx = actor.ctx
    operations = {"approve": partner.approve, "paid": partner.mark_paid, "reject": partner.reject}
    async with ctx.session_factory.begin() as session:
        try:
            withdrawal = await operations[action](
                session, withdrawal_id, actor.tg_id, comment=body.comment, source="web", ip=actor.ip
            )
        except partner.PartnerError as exc:
            if exc.code == partner.PartnerErrorCode.NOT_FOUND:
                raise not_found("Withdrawal not found") from None
            if exc.code == partner.PartnerErrorCode.BAD_STATUS:
                raise conflict("bad_status", str(exc)) from None
            raise bad_request(str(exc.code), str(exc)) from None
        item = (await _items(session, [withdrawal], False, ctx.settings))[0]
    if body.notify:
        w = item.withdrawal
        comment = html.escape(w.admin_comment) if w.admin_comment else ""

        def build(lang: str) -> str:
            amount = notify.money(ctx, w.amount_kop, lang)
            extra = notify.t(ctx, "admin_api.notify.withdrawal_comment", lang, comment=comment) if comment else ""
            key = {"approve": "approved", "paid": "paid", "reject": "rejected"}[action]
            return notify.t(ctx, f"admin_api.notify.withdrawal_{key}", lang, id=w.id, amount=amount, comment=extra)

        notify.notify_user(ctx, w.user_id, build)
    return item


@router.post("/withdrawals/{withdrawal_id}/approve", response_model=WithdrawalItem)
async def approve(withdrawal_id: int, body: DecisionIn, actor: Payouts) -> WithdrawalItem:
    """Pending -> approved (you are making the payout)."""
    return await _decide(actor, withdrawal_id, body, "approve")


@router.post("/withdrawals/{withdrawal_id}/paid", response_model=WithdrawalItem)
async def mark_paid(withdrawal_id: int, body: DecisionIn, actor: Payouts) -> WithdrawalItem:
    """The money was sent to the partner."""
    return await _decide(actor, withdrawal_id, body, "paid")


@router.post("/withdrawals/{withdrawal_id}/reject", response_model=WithdrawalItem)
async def reject(withdrawal_id: int, body: DecisionIn, actor: Payouts) -> WithdrawalItem:
    """Decline the request; the amount goes back to the partner balance."""
    return await _decide(actor, withdrawal_id, body, "reject")
