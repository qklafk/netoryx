"""Payments: list and details, manual crediting of stuck payments, Stars refunds, providers overview."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Annotated, Any

from aiogram.exceptions import TelegramAPIError
from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import func, or_, select

from app.core.permissions import Permission
from app.core.settings_registry import GROUPS
from app.db.models import (
    BalanceTransaction,
    LedgerAccount,
    Payment,
    PaymentProvider,
    PaymentStatus,
    Plan,
    Purchase,
    TxKind,
    User,
)
from app.db.session import read_session
from app.modules.admin_api.deps import Actor, bad_request, conflict, not_found, require
from app.modules.admin_api.schemas.common import Page
from app.modules.admin_api.schemas.payments import (
    CreditOut,
    LedgerOut,
    MarkPaidIn,
    PaymentDetail,
    PaymentOut,
    ProviderOut,
    ProviderTestOut,
    PurchaseOut,
    RefundIn,
    RefundOut,
    StatusIn,
)
from app.services import wallet
from app.services.payments import service as payments_service
from app.services.payments.service import CreditResult
from app.utils.time import utcnow
from app.web.deps import PageParams, ReadDb

log = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["admin: payments"])

Viewer = Annotated[Actor, Depends(require(Permission.PAYMENTS_VIEW))]
Editor = Annotated[Actor, Depends(require(Permission.PAYMENTS_VIEW, Permission.PAYMENTS_EDIT))]

PaymentId = Annotated[str, Path(pattern=r"^[0-9A-Za-z-]{1,36}$")]


@router.get("", response_model=Page[PaymentOut])
async def list_payments(
    actor: Viewer,
    session: ReadDb,
    page: PageParams,
    status: PaymentStatus | None = None,
    provider: Annotated[str | None, Query(max_length=32)] = None,
    user_id: int | None = None,
    donation: bool | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    q: Annotated[str | None, Query(max_length=128, description="Our or the provider's payment id")] = None,
) -> Page[PaymentOut]:
    """Payments, newest first."""
    stmt = select(Payment)
    if status is not None:
        stmt = stmt.where(Payment.status == status)
    if provider:
        stmt = stmt.where(Payment.provider == provider)
    if user_id is not None:
        stmt = stmt.where(Payment.user_id == user_id)
    if donation is not None:
        stmt = stmt.where(Payment.is_donation.is_(donation))
    if since is not None:
        stmt = stmt.where(Payment.created_at >= since)
    if until is not None:
        stmt = stmt.where(Payment.created_at < until)
    if q:
        stmt = stmt.where(or_(Payment.id == q.strip(), Payment.provider_payment_id == q.strip()))
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (await session.scalars(stmt.order_by(Payment.created_at.desc()).limit(page.limit).offset(page.offset))).all()
    return Page(items=[PaymentOut.model_validate(p) for p in rows], total=total, limit=page.limit, offset=page.offset)


async def _payment(session: Any, payment_id: str) -> Payment:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise not_found("Payment not found")
    return payment


# ---------------------------------------------------------------------------------------------
# Providers (static paths before /{payment_id})
# ---------------------------------------------------------------------------------------------
def _provider_keys(actor: Actor) -> list[str]:
    keys = [p.value for p in PaymentProvider]
    gateways = getattr(actor.ctx, "gateways", None)
    for key in gateways.as_dict() if gateways is not None else {}:
        if key not in keys:
            keys.append(key)
    return keys


def _webhook_url(settings: Any, key: str) -> str | None:
    if key == PaymentProvider.STARS.value or not settings.public_base_url:
        return None
    return f"{settings.public_base_url.rstrip('/')}/webhooks/{key}"


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(actor: Viewer, session: ReadDb) -> list[ProviderOut]:
    """Every payment provider: switched on, running, its webhook URL and last month's turnover.

    Credentials and options are edited in the settings section (group ``settings_group``).
    """
    settings = actor.ctx.settings
    gateways = getattr(actor.ctx, "gateways", None)
    since = utcnow() - timedelta(days=30)
    turnover = {
        provider: (int(n), int(kop))
        for provider, n, kop in (
            await session.execute(
                select(Payment.provider, func.count(Payment.id), func.coalesce(func.sum(Payment.amount_kop), 0))
                .where(Payment.status == PaymentStatus.SUCCEEDED, Payment.credited_at >= since)
                .group_by(Payment.provider)
            )
        ).all()
    }
    last = dict(
        (
            await session.execute(
                select(Payment.provider, func.max(Payment.credited_at))
                .where(Payment.status == PaymentStatus.SUCCEEDED)
                .group_by(Payment.provider)
            )
        ).all()
    )
    result = []
    for key in _provider_keys(actor):
        gateway = gateways.get(key) if gateways is not None else None
        group = f"payments_{key}"
        title = GROUPS[group][1] if group in GROUPS else (getattr(gateway, "title", None) or key)
        n, kop = turnover.get(key, (0, 0))
        result.append(
            ProviderOut(
                key=key,
                title=title,
                enabled=bool(getattr(settings, f"{key}_enabled", gateway is not None)),
                active=gateway is not None,
                settings_group=group if group in GROUPS else None,
                webhook_url=_webhook_url(settings, key),
                supports_polling=getattr(gateway, "supports_polling", None),
                min_amount_kop=getattr(gateway, "min_amount_kop", None),
                payments_30d=n,
                amount_30d_kop=kop,
                last_success_at=last.get(key),
            )
        )
    return result


@router.post("/providers/{key}/test", response_model=ProviderTestOut)
async def test_provider(key: str, actor: Editor) -> ProviderTestOut:
    """Check a running gateway's credentials with the provider, where its API allows it."""
    gateways = getattr(actor.ctx, "gateways", None)
    gateway = gateways.get(key) if gateways is not None else None
    if gateway is None:
        return ProviderTestOut(ok=False, supported=True, detail="The provider is switched off or misconfigured")
    check = getattr(gateway, "check", None)
    if not callable(check):
        return ProviderTestOut(
            ok=None, supported=False, detail="This provider has no check call; make a small test payment instead"
        )
    try:
        detail = await check()
    except Exception as exc:  # any provider failure is the answer to show
        return ProviderTestOut(ok=False, supported=True, detail=f"{type(exc).__name__}: {str(exc)[:300]}")
    async with actor.ctx.session_factory.begin() as s:
        await actor.audit(s, "payment.provider_test", "provider", key)
    return ProviderTestOut(ok=True, supported=True, detail=str(detail)[:300] if detail else None)


# ---------------------------------------------------------------------------------------------
# One payment
# ---------------------------------------------------------------------------------------------
@router.get("/{payment_id}", response_model=PaymentDetail)
async def payment_detail(payment_id: PaymentId, actor: Viewer, session: ReadDb) -> PaymentDetail:
    payment = await _payment(session, payment_id)
    username = await session.scalar(select(User.username).where(User.tg_id == payment.user_id))
    plan_title = await session.scalar(select(Plan.title).where(Plan.id == payment.plan_id)) if payment.plan_id else None
    ledger = (
        await session.scalars(
            select(BalanceTransaction)
            .where(BalanceTransaction.user_id == payment.user_id, BalanceTransaction.ref.contains(payment.id))
            .order_by(BalanceTransaction.id)
        )
    ).all()
    purchases = (await session.scalars(select(Purchase).where(Purchase.payment_id == payment.id))).all()
    return PaymentDetail(
        payment=PaymentOut.model_validate(payment),
        confirmation_url=payment.confirmation_url,
        username=username,
        plan_title=plan_title,
        ledger=[LedgerOut.model_validate(r) for r in ledger],
        purchases=[PurchaseOut.model_validate(p) for p in purchases],
    )


async def _run_hook(actor: Actor, result: CreditResult) -> bool:
    try:
        await actor.ctx.on_payment_credited(result)
    except Exception:
        log.exception("payment %s: post-credit processing failed", result.payment_id)
        return False
    return True


@router.post("/{payment_id}/mark-paid", response_model=CreditOut)
async def mark_paid(payment_id: PaymentId, body: MarkPaidIn, actor: Editor) -> CreditOut:
    """Credit a payment the provider confirmed but we missed (the plan is bought and rewards granted as usual)."""
    ctx = actor.ctx
    async with read_session(ctx.session_factory) as session:
        payment = await _payment(session, payment_id)
    if payment.status not in payments_service.CREDITABLE_STATUSES:
        raise conflict("not_creditable", f"The payment is {payment.status}")
    result = await payments_service.credit(
        ctx.session_factory, ctx.settings, payment_id, paid_amount_kop=body.paid_amount_kop
    )
    async with ctx.session_factory.begin() as session:
        await actor.audit(
            session,
            "payment.mark_paid",
            "payment",
            payment_id,
            reason=body.reason,
            credited=result is not None,
            amount_kop=result.amount_kop if result else None,
        )
    if result is None:
        return CreditOut(credited=False)
    hook_ran = await _run_hook(actor, result)
    return CreditOut(
        credited=True, amount_kop=result.amount_kop, new_balance_kop=result.new_balance_kop, hook_ran=hook_ran
    )


@router.post("/{payment_id}/rerun", response_model=CreditOut)
async def rerun_hook(payment_id: PaymentId, actor: Editor) -> CreditOut:
    """Run the after-payment processing again for a credited payment whose plan was not bought (idempotent)."""
    ctx = actor.ctx
    async with read_session(ctx.session_factory) as session:
        payment = await _payment(session, payment_id)
        balance = await wallet.balance(session, payment.user_id)
    if payment.status != PaymentStatus.SUCCEEDED:
        raise conflict("not_credited", "Only credited payments can be processed again")
    result = CreditResult(
        payment_id=payment.id,
        user_id=payment.user_id,
        amount_kop=payment.amount_kop,
        new_balance_kop=balance,
        plan_id=payment.plan_id,
        is_gift=payment.is_gift,
        is_donation=payment.is_donation,
        inviter=None,
        provider=payment.provider,
    )
    hook_ran = await _run_hook(actor, result)
    async with ctx.session_factory.begin() as session:
        await actor.audit(session, "payment.rerun_hook", "payment", payment_id, ok=hook_ran)
    return CreditOut(credited=True, amount_kop=payment.amount_kop, new_balance_kop=balance, hook_ran=hook_ran)


@router.post("/{payment_id}/status", response_model=PaymentOut)
async def set_status(payment_id: PaymentId, body: StatusIn, actor: Editor) -> PaymentOut:
    """Cancel or expire an open payment by hand."""
    ctx = actor.ctx
    status = PaymentStatus(body.status)
    if not await payments_service.mark_status(ctx.session_factory, payment_id, status):
        raise conflict("bad_status", "Only open payments can be canceled or expired")
    async with ctx.session_factory.begin() as session:
        await actor.audit(session, "payment.set_status", "payment", payment_id, status=status.value, reason=body.reason)
        return PaymentOut.model_validate(await _payment(session, payment_id))


@router.post("/{payment_id}/refund", response_model=RefundOut)
async def refund_stars(payment_id: PaymentId, body: RefundIn, actor: Editor) -> RefundOut:
    """Refund a Telegram Stars payment to the user (Telegram returns the Stars)."""
    ctx = actor.ctx
    async with read_session(ctx.session_factory) as session:
        payment = await _payment(session, payment_id)
    if payment.provider != PaymentProvider.STARS.value:
        raise bad_request("not_stars", "Only Telegram Stars payments can be refunded here")
    if payment.status != PaymentStatus.SUCCEEDED or not payment.provider_payment_id:
        raise conflict("not_refundable", "Only credited Stars payments can be refunded")
    try:
        await ctx.bot.refund_star_payment(
            user_id=payment.user_id, telegram_payment_charge_id=payment.provider_payment_id
        )
    except TelegramAPIError as exc:
        raise conflict("telegram_refused", str(exc)[:300]) from None
    await payments_service.mark_status(ctx.session_factory, payment_id, PaymentStatus.REFUNDED, claw_back=False)
    debited = 0
    async with ctx.session_factory.begin() as session:
        if body.debit_wallet and not payment.is_donation:
            user = await session.get(User, payment.user_id, with_for_update=True)
            available = user.balance_kop if user is not None else 0
            amount = min(available, payment.amount_kop)
            if amount > 0:
                applied = await wallet.apply(
                    session,
                    payment.user_id,
                    -amount,
                    TxKind.REFUND,
                    f"refund:{payment.id}",
                    actor_id=actor.tg_id,
                    account=LedgerAccount.MAIN,
                )
                debited = amount if applied is not None else 0
        await actor.audit(session, "payment.refund", "payment", payment_id, reason=body.reason, debited_kop=debited)
    return RefundOut(refunded=True, debited_kop=debited)
