"""Referral program, partner balance and payouts, promo and gift codes."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request

from app.config import Settings
from app.db.models import LedgerAccount, Plan, PromoKind, PurchaseKind, User
from app.db.session import read_session
from app.modules.miniapp_api import deps
from app.modules.miniapp_api.account import sync_subscription
from app.modules.miniapp_api.deps import GIFTS, PROMO, REFERRAL, error, needs
from app.modules.miniapp_api.schemas import (
    LevelOut,
    PartnerOut,
    RedeemIn,
    RedeemOut,
    ReferralOut,
    TransferIn,
    TransferOut,
    WithdrawalIn,
    WithdrawalOut,
)
from app.services import partner, promo, referrals, subscriptions, wallet
from app.services.notifications import AdminEvent
from app.services.partner import PartnerError, PartnerErrorCode
from app.services.promo import PromoOutcome
from app.web.auth import MiniAppUser
from app.web.deps import Ctx, ReadDb

log = logging.getLogger(__name__)

router = APIRouter(tags=["Mini App: rewards"])

REDEEM_LIMIT = (10, 600)  # code attempts per user per 10 minutes (guessing codes is pointless)
TRANSFER_LIMIT = (10, 600)
WITHDRAWAL_LIMIT = (5, 3600)
WITHDRAWALS_SHOWN = 20

_PARTNER_STATUS = {
    PartnerErrorCode.DISABLED: 404,
    PartnerErrorCode.NOT_FOUND: 404,
    PartnerErrorCode.PENDING_EXISTS: 409,
    PartnerErrorCode.BAD_STATUS: 409,
    PartnerErrorCode.INSUFFICIENT_FUNDS: 402,
}
_PROMO_STATUS = {
    PromoOutcome.NOT_FOUND: 404,
    PromoOutcome.EXPIRED: 410,
    PromoOutcome.EXHAUSTED: 410,
    PromoOutcome.ALREADY_USED: 409,
    PromoOutcome.ONLY_NEW: 403,
}


def referral_link(ctx: Any, code: str) -> str | None:
    bot_username = getattr(ctx, "bot_username", None)
    return f"https://t.me/{bot_username}?start=ref_{code}" if bot_username else None


def _partner_error(ctx: Any, user: User, exc: PartnerError) -> Exception:
    settings: Settings = ctx.settings
    _ = deps.translator(ctx, user)
    return error(
        ctx,
        user,
        _PARTNER_STATUS.get(exc.code, 400),
        exc.code.value,
        f"partner.{exc.code.value}",
        min=_.money(settings.min_withdrawal_kop),
    )


# ---------------------------------------------------------------------------------------------
# Referral
# ---------------------------------------------------------------------------------------------
@router.get("/referral", response_model=ReferralOut, dependencies=[needs(REFERRAL)], summary="Referral program")
async def referral(
    ctx: Ctx, user: MiniAppUser, db: ReadDb, qr: Annotated[bool, Query(description="Include a QR code")] = False
) -> ReferralOut:
    settings: Settings = ctx.settings
    invited, earned_kop = await referrals.stats(db, user.tg_id)
    stats = await partner.partner_stats(db, settings, user.tg_id)
    percents = settings.referral_levels_percent
    mode = settings.referral_reward_mode
    link = referral_link(ctx, user.referral_code)
    share = deps.text(ctx, user, f"referral.share_{mode}", brand=settings.brand_name, link=link or user.referral_code)
    partner_out: PartnerOut | None = None
    if mode == "partner":
        rows = await partner.list_for_user(db, user.tg_id, limit=WITHDRAWALS_SHOWN)
        partner_out = PartnerOut(
            balance_kop=stats.referral_balance_kop,
            pending_withdrawal_kop=stats.pending_withdrawal_kop,
            withdrawn_kop=stats.withdrawn_kop,
            withdrawals_available=partner.withdrawals_available(settings),
            min_withdrawal_kop=settings.min_withdrawal_kop,
            methods=list(settings.withdrawal_methods),
            withdrawals=[
                WithdrawalOut(
                    id=w.id,
                    amount_kop=w.amount_kop,
                    method=w.method,
                    status=w.status.value,
                    created_at=w.created_at,
                    processed_at=w.processed_at,
                    comment=w.admin_comment,
                )
                for w in rows
            ],
        )
    days_mode = mode == "days"
    return ReferralOut(
        mode=mode,
        code=user.referral_code,
        link=link,
        share_text=share,
        qr=await deps.qr_data_url(link) if qr and link else None,
        invited=invited,
        paid_invited=stats.levels[0].paid_users if stats.levels else 0,
        earned_kop=stats.total_earned_kop if mode == "partner" else earned_kop,
        earned_days=stats.total_earned_days,
        invitee_bonus_kop=0 if days_mode else settings.referral_invitee_bonus_kop,
        inviter_bonus_kop=0 if days_mode else settings.referral_inviter_bonus_kop,
        invitee_bonus_days=settings.referral_invitee_bonus_days if days_mode else 0,
        inviter_bonus_days=settings.referral_inviter_bonus_days if days_mode else 0,
        levels=[
            LevelOut(
                level=level.level,
                percent=percents[level.level - 1] if level.level <= len(percents) else 0,
                users=level.users,
                paid_users=level.paid_users,
                earned_kop=level.earned_kop,
                earned_days=level.earned_days,
            )
            for level in stats.levels
        ],
        partner=partner_out,
    )


@router.post(
    "/referral/transfer",
    response_model=TransferOut,
    dependencies=[needs(REFERRAL)],
    summary="Move partner earnings to the main balance",
)
async def transfer(body: TransferIn, request: Request, ctx: Ctx, user: MiniAppUser) -> TransferOut:
    if ctx.settings.referral_reward_mode != "partner":
        raise error(ctx, user, 404, "feature_disabled")
    await deps.rate_limit(request, ctx, user, "partner_transfer", *TRANSFER_LIMIT)
    ref = f"transfer:app:{user.tg_id}:{body.request_id}" if body.request_id else None
    try:
        async with ctx.session_factory.begin() as session:
            result = await partner.transfer_to_main(session, user.tg_id, body.amount_kop, ref=ref)
            main = await wallet.balance(session, user.tg_id)
            rest = await wallet.balance(session, user.tg_id, LedgerAccount.REFERRAL)
    except PartnerError as exc:
        raise _partner_error(ctx, user, exc) from None
    return TransferOut(amount_kop=result.amount_kop, applied=result.applied, balance_kop=main, partner_balance_kop=rest)


@router.post(
    "/referral/withdrawals",
    response_model=WithdrawalOut,
    status_code=201,
    dependencies=[needs(REFERRAL)],
    summary="Request a payout of partner earnings",
)
async def request_withdrawal(body: WithdrawalIn, request: Request, ctx: Ctx, user: MiniAppUser) -> WithdrawalOut:
    settings: Settings = ctx.settings
    await deps.rate_limit(request, ctx, user, "withdrawal", *WITHDRAWAL_LIMIT)
    try:
        async with ctx.session_factory.begin() as session:
            row = await partner.request_withdrawal(
                session, settings, user.tg_id, body.amount_kop, body.method, body.details
            )
            out = WithdrawalOut(
                id=row.id,
                amount_kop=row.amount_kop,
                method=row.method,
                status=row.status.value,
                created_at=row.created_at,
                processed_at=row.processed_at,
                comment=row.admin_comment,
            )
    except PartnerError as exc:
        raise _partner_error(ctx, user, exc) from None
    deps.notify_admins(
        ctx,
        AdminEvent.WITHDRAWALS,
        "admin.withdrawal",
        id=out.id,
        amount=ctx.i18n.money(out.amount_kop, settings.default_language),
        method=ctx.i18n.esc(out.method),
        user_id=user.tg_id,
        username=ctx.i18n.esc(f"@{user.username}" if user.username else ""),
    )
    return out


# ---------------------------------------------------------------------------------------------
# Promo & gift codes
# ---------------------------------------------------------------------------------------------
@router.post("/promo/redeem", response_model=RedeemOut, summary="Redeem a promo code or a gift code")
async def redeem(body: RedeemIn, request: Request, ctx: Ctx, user: MiniAppUser) -> RedeemOut:
    settings: Settings = ctx.settings
    promo_on = await deps.feature_on(ctx, PROMO, user.tg_id)
    gifts_on = await deps.feature_on(ctx, GIFTS, user.tg_id)
    if not (promo_on or gifts_on):
        raise error(ctx, user, 404, "feature_disabled")
    await deps.rate_limit(request, ctx, user, "redeem", *REDEEM_LIMIT)
    _ = deps.translator(ctx, user)
    sub_id: int | None = None
    kind: Literal["promo", "gift"] = "promo"
    bonus_kop = bonus_days = 0
    async with ctx.session_factory.begin() as session:
        row = await session.get(User, user.tg_id)
        assert row is not None
        if (
            body.subscription_id is not None
            and await subscriptions.get_for_user(session, user.tg_id, body.subscription_id) is None
        ):
            raise error(ctx, user, 404, "no_subscription")
        found = await promo.get_by_code(session, body.code)
        allowed = found is not None and (gifts_on if found.kind is PromoKind.GIFT else promo_on)
        if not allowed:
            outcome, code = PromoOutcome.NOT_FOUND, None
        else:
            outcome, code = await promo.redeem(session, settings, row, body.code)
        if outcome is PromoOutcome.OK and code is not None and code.bonus_days > 0:
            is_gift = code.kind is PromoKind.GIFT
            plan = await session.get(Plan, code.plan_id) if is_gift and code.plan_id is not None else None
            await subscriptions.grant_days(
                session,
                settings,
                row,
                code.bonus_days,
                kind=PurchaseKind.GIFT_REDEEM if is_gift else PurchaseKind.PROMO,
                plan=plan,
                subscription_id=body.subscription_id,
            )
            sub = await subscriptions.get_for_user(session, user.tg_id, body.subscription_id)
            sub_id = sub.id if sub is not None else None
        if outcome is PromoOutcome.OK and code is not None:
            kind = "gift" if code.kind is PromoKind.GIFT else "promo"
            bonus_kop, bonus_days = code.bonus_kop, code.bonus_days
        balance = await wallet.balance(session, user.tg_id)
    if outcome is not PromoOutcome.OK or code is None:
        raise error(ctx, user, _PROMO_STATUS.get(outcome, 400), f"promo_{outcome.value}")
    if sub_id is not None:
        await sync_subscription(ctx, sub_id)
    expires_at = await _expires_at(ctx, user.tg_id, body.subscription_id) if bonus_days > 0 else None
    parts = []
    if bonus_kop > 0:
        parts.append(deps.text(ctx, user, "promo.money", amount=_.money(bonus_kop)))
    if bonus_days > 0:
        parts.append(deps.text(ctx, user, "promo.days", days=_.plural(bonus_days, "plural.days")))
    message = deps.text(ctx, user, f"promo.ok_{kind}", bonus=", ".join(parts))
    log.info("mini app: user %s redeemed a %s code", user.tg_id, kind)
    return RedeemOut(
        kind=kind,
        bonus_kop=bonus_kop,
        bonus_days=bonus_days,
        balance_kop=balance,
        expires_at=expires_at,
        message=message,
    )


async def _expires_at(ctx: Any, user_id: int, subscription_id: int | None = None) -> datetime | None:
    async with read_session(ctx.session_factory) as session:
        sub = await subscriptions.get_for_user(session, user_id, subscription_id)
    return sub.expires_at if sub is not None else None
