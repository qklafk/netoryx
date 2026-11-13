"""Users: search and filters, the user card and every admin action on a user."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated, Any

from aiogram.types import LinkPreviewOptions
from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import TextValidationError, check_html
from app.core.permissions import Permission, resolve_role
from app.db.models import (
    BalanceTransaction,
    Inbound,
    LedgerAccount,
    Payment,
    Plan,
    Provision,
    ProvisionState,
    Purchase,
    PurchaseKind,
    Subscription,
    SubscriptionStatus,
    Ticket,
    TxKind,
    User,
)
from app.db.session import read_session
from app.modules.admin_api import notify
from app.modules.admin_api.deps import Actor, bad_request, conflict, kick_reconcile, require, schedule_sync
from app.modules.admin_api.queries import (
    get_subscription,
    get_user,
    list_item,
    require_subscription,
    search_condition,
    subscription_url,
    subscription_view,
    user_filter,
)
from app.modules.admin_api.schemas.common import Page
from app.modules.admin_api.schemas.payments import LedgerOut, PaymentOut, PurchaseOut
from app.modules.admin_api.schemas.users import (
    BalanceAdjustIn,
    BalanceOut,
    BulkBanIn,
    DaysIn,
    DeleteOut,
    DeviceOut,
    DevicesResetIn,
    ExpiryIn,
    FlagIn,
    LimitsIn,
    MessageIn,
    MessageOut,
    PanelDevicesOut,
    ReferralInfo,
    RemoveDaysIn,
    ResetOut,
    RotateOut,
    SubscriptionOut,
    SubStatusIn,
    SyncOut,
    TicketBrief,
    UserCard,
    UserFilter,
    UserListItem,
    UserOut,
    UserSort,
)
from app.modules.subscription.stars_renewal import cancel_stars_renewal
from app.services import bans, campaigns, devices, partner, provisioning, referrals, subscriptions, wallet
from app.services.notify import safe_send
from app.services.panels.base import PanelError
from app.utils.time import utcnow
from app.web.deps import PageParams, ReadDb

log = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["admin: users"])

UserId = Annotated[int, Path(ge=1, le=2**63 - 1, description="Telegram user id")]

Viewer = Annotated[Actor, Depends(require(Permission.USERS_VIEW))]
Editor = Annotated[Actor, Depends(require(Permission.USERS_VIEW, Permission.USERS_EDIT))]
Cashier = Annotated[Actor, Depends(require(Permission.USERS_VIEW, Permission.USERS_MONEY))]
Manager = Annotated[Actor, Depends(require(Permission.USERS_EDIT, Permission.USERS_MONEY))]
# Free service (days, a later expiry, higher limits) is not a support task: it needs its own permission.
Granter = Annotated[Actor, Depends(require(Permission.USERS_VIEW, Permission.USERS_EDIT, Permission.SUBS_GRANT))]
Exporter = Annotated[Actor, Depends(require(Permission.USERS_VIEW, Permission.SUBS_BULK))]
BulkEditor = Annotated[Actor, Depends(require(Permission.USERS_EDIT, Permission.SUBS_BULK))]

CARD_ROWS = 20
EXPORT_BATCH = 1000
DELETE_CONCURRENCY = 8
CSV_COLUMNS = (
    "tg_id",
    "username",
    "first_name",
    "language",
    "created_at",
    "last_seen_at",
    "balance_rub",
    "partner_balance_rub",
    "banned",
    "blocked_bot",
    "subscription_status",
    "expires_at",
    "plan_id",
    "trial",
    "complimentary",
)
_SORT_COLUMNS: dict[UserSort, Any] = {
    UserSort.CREATED: User.created_at,
    UserSort.LAST_SEEN: User.last_seen_at,
    UserSort.BALANCE: User.balance_kop,
    UserSort.EXPIRES: Subscription.expires_at,
}


# ---------------------------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------------------------
def _list_stmt(status: UserFilter, q: str | None, plan_id: int | None, referred_by: int | None) -> Any:
    primary = (
        select(func.min(Subscription.id)).where(Subscription.user_id == User.tg_id).correlate(User).scalar_subquery()
    )
    stmt = select(User, Subscription).outerjoin(Subscription, Subscription.id == primary)
    condition = user_filter(status, utcnow())
    if condition is not None:
        stmt = stmt.where(condition)
    if q:
        search = search_condition(q)
        if search is None:
            return None
        stmt = stmt.where(search)
    if plan_id is not None:
        stmt = stmt.where(
            select(Subscription.id)
            .where(Subscription.user_id == User.tg_id, Subscription.plan_id == plan_id)
            .correlate(User)
            .exists()
        )
    if referred_by is not None:
        stmt = stmt.where(User.referred_by == referred_by)
    return stmt


@router.get("", response_model=Page[UserListItem])
async def list_users(
    actor: Viewer,
    session: ReadDb,
    page: PageParams,
    status: UserFilter = UserFilter.ALL,
    q: Annotated[str | None, Query(max_length=128, description="Telegram id, @username or a part of the name")] = None,
    plan_id: int | None = None,
    referred_by: int | None = None,
    sort: UserSort = UserSort.CREATED,
    desc: bool = True,
) -> Page[UserListItem]:
    """Users with their subscription, filtered and paginated."""
    stmt = _list_stmt(status, q, plan_id, referred_by)
    if stmt is None:
        return Page(items=[], total=0, limit=page.limit, offset=page.offset)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    column = _SORT_COLUMNS[sort]
    order = column.desc() if desc else column.asc()
    # NULLs last on every database (MySQL has no NULLS LAST): order by "is null" first.
    stmt = stmt.order_by(column.is_(None), order, User.tg_id.desc())
    rows = (await session.execute(stmt.limit(page.limit).offset(page.offset))).all()
    return Page(items=[list_item(u, s) for u, s in rows], total=total, limit=page.limit, offset=page.offset)


def _csv_safe(value: Any) -> str:
    """A cell value that spreadsheet programs will not run as a formula."""
    if value is None:
        return ""
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


async def _export_rows(ctx: Any, status: UserFilter, q: str | None) -> AsyncIterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    yield "﻿" + buffer.getvalue()  # BOM: Excel opens UTF-8 correctly
    last_id: int | None = None
    while True:
        stmt = _list_stmt(status, q, None, None)
        if stmt is None:
            return
        if last_id is not None:
            stmt = stmt.where(User.tg_id > last_id)
        async with read_session(ctx.session_factory) as session:
            rows = (await session.execute(stmt.order_by(User.tg_id).limit(EXPORT_BATCH))).all()
        if not rows:
            return
        buffer.seek(0)
        buffer.truncate()
        for user, sub in rows:
            writer.writerow(
                [
                    _csv_safe(v)
                    for v in (
                        user.tg_id,
                        user.username,
                        user.first_name,
                        user.language,
                        user.created_at,
                        user.last_seen_at,
                        f"{user.balance_kop / 100:.2f}",
                        f"{user.referral_balance_kop / 100:.2f}",
                        int(user.is_banned),
                        int(user.bot_blocked),
                        sub.status.value if sub else None,
                        sub.expires_at if sub else None,
                        sub.plan_id if sub else None,
                        int(bool(sub and sub.is_trial)),
                        int(bool(sub and sub.is_complimentary)),
                    )
                ]
            )
        last_id = rows[-1][0].tg_id
        yield buffer.getvalue()


@router.get("/export.csv", response_class=StreamingResponse)
async def export_csv(
    actor: Exporter,
    status: UserFilter = UserFilter.ALL,
    q: Annotated[str | None, Query(max_length=128)] = None,
) -> StreamingResponse:
    """Download the (filtered) user list as CSV."""
    async with actor.ctx.session_factory.begin() as session:
        await actor.audit(session, "user.export", "user", None, status=status.value, q=q)
    filename = f"users-{utcnow():%Y%m%d-%H%M}.csv"
    return StreamingResponse(
        _export_rows(actor.ctx, status, q),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------------------------
# Card
# ---------------------------------------------------------------------------------------------
async def _rows(session: AsyncSession, stmt: Any) -> list[Any]:
    return list((await session.scalars(stmt.limit(CARD_ROWS))).all())


@router.get("/{user_id}", response_model=UserCard)
async def user_card(user_id: UserId, actor: Viewer, session: ReadDb) -> UserCard:
    """Everything about one user."""
    now = utcnow()
    settings = actor.ctx.settings
    user = await get_user(session, user_id)
    sub = await get_subscription(session, user_id)
    invited, earned = await referrals.stats(session, user_id)
    levels = await partner.tree_counts(session, user_id, max(1, len(settings.referral_levels_percent)))
    inviter = None
    if user.referred_by is not None:
        parent = await session.get(User, user.referred_by)
        if parent is not None:
            inviter = list_item(parent, await get_subscription(session, parent.tg_id))
    return UserCard(
        user=UserOut.model_validate(user),
        subscription_url=subscription_url(settings, user),
        subscription=await subscription_view(session, sub, now) if sub else None,
        subscriptions=[
            await subscription_view(session, item, now) for item in await subscriptions.list_for_user(session, user_id)
        ],
        payments=[
            PaymentOut.model_validate(p)
            for p in await _rows(
                session, select(Payment).where(Payment.user_id == user_id).order_by(Payment.created_at.desc())
            )
        ],
        purchases=[
            PurchaseOut.model_validate(p)
            for p in await _rows(
                session, select(Purchase).where(Purchase.user_id == user_id).order_by(Purchase.id.desc())
            )
        ],
        ledger=[LedgerOut.model_validate(r) for r in await wallet.history(session, user_id, limit=CARD_ROWS)],
        tickets=[
            TicketBrief.model_validate(t)
            for t in await _rows(
                session, select(Ticket).where(Ticket.user_id == user_id).order_by(Ticket.updated_at.desc())
            )
        ],
        referrals=ReferralInfo(invited=invited, earned_kop=earned, levels=levels, inviter=inviter),
        campaign=await campaigns.campaign_of(session, user_id),
        is_admin=await resolve_role(session, settings, user_id) is not None,
    )


@router.get("/{user_id}/payments", response_model=Page[PaymentOut])
async def user_payments(user_id: UserId, actor: Viewer, session: ReadDb, page: PageParams) -> Page[PaymentOut]:
    stmt = select(Payment).where(Payment.user_id == user_id)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (await session.scalars(stmt.order_by(Payment.created_at.desc()).limit(page.limit).offset(page.offset))).all()
    return Page(items=[PaymentOut.model_validate(p) for p in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/{user_id}/purchases", response_model=Page[PurchaseOut])
async def user_purchases(user_id: UserId, actor: Viewer, session: ReadDb, page: PageParams) -> Page[PurchaseOut]:
    stmt = select(Purchase).where(Purchase.user_id == user_id)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (await session.scalars(stmt.order_by(Purchase.id.desc()).limit(page.limit).offset(page.offset))).all()
    return Page(items=[PurchaseOut.model_validate(p) for p in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/{user_id}/ledger", response_model=Page[LedgerOut])
async def user_ledger(
    user_id: UserId, actor: Viewer, session: ReadDb, page: PageParams, account: LedgerAccount | None = None
) -> Page[LedgerOut]:
    stmt = select(BalanceTransaction).where(BalanceTransaction.user_id == user_id)
    if account is not None:
        stmt = stmt.where(BalanceTransaction.account == account)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        await session.scalars(stmt.order_by(BalanceTransaction.id.desc()).limit(page.limit).offset(page.offset))
    ).all()
    return Page(items=[LedgerOut.model_validate(r) for r in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/{user_id}/referrals", response_model=Page[UserListItem])
async def user_referrals(user_id: UserId, actor: Viewer, session: ReadDb, page: PageParams) -> Page[UserListItem]:
    """Users invited directly by this user."""
    stmt = _list_stmt(UserFilter.ALL, None, None, user_id)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (await session.execute(stmt.order_by(User.created_at.desc()).limit(page.limit).offset(page.offset))).all()
    return Page(items=[list_item(u, s) for u, s in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/{user_id}/devices", response_model=list[PanelDevicesOut])
async def user_devices(user_id: UserId, actor: Viewer, subscription_id: int | None = None) -> list[PanelDevicesOut]:
    """Devices connected to the user's panel accounts (as far as each panel reports them)."""
    ctx = actor.ctx
    async with read_session(ctx.session_factory) as session:
        sub = await require_subscription(session, user_id, subscription_id=subscription_id)
    result = await devices.list_devices(ctx.session_factory, ctx.panels, sub.id)
    return [
        PanelDevicesOut(
            panel_id=p.panel_id,
            panel_name=p.panel_name,
            kind=str(p.kind),
            supported=p.supported,
            can_delete_one=p.can_delete_one,
            devices=[
                DeviceOut(
                    id=d.id,
                    platform=d.platform,
                    model=d.model,
                    os_version=d.os_version,
                    user_agent=d.user_agent,
                    ip=d.ip,
                    created_at=d.created_at,
                    last_seen_at=d.last_seen_at,
                )
                for d in p.devices
            ],
            error=p.error,
        )
        for p in result
    ]


# ---------------------------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------------------------
@router.post("/{user_id}/balance", response_model=BalanceOut)
async def adjust_balance(user_id: UserId, body: BalanceAdjustIn, actor: Cashier) -> BalanceOut:
    """Credit or charge the main or the partner balance (ledger-backed; idempotent with ``request_id``)."""
    ctx = actor.ctx
    account = LedgerAccount(body.account)
    ref = f"admin:{body.request_id or uuid.uuid4().hex}"
    async with ctx.session_factory.begin() as session:
        user = await get_user(session, user_id)
        try:
            applied = await wallet.apply(
                session, user_id, body.amount_kop, TxKind.ADMIN_ADJUST, ref, actor_id=actor.tg_id, account=account
            )
        except wallet.InsufficientFunds:
            raise conflict("insufficient_funds", "The balance is lower than the amount to charge") from None
        if applied is not None:
            await actor.audit(
                session,
                "user.balance_adjust",
                "user",
                user_id,
                amount_kop=body.amount_kop,
                account=account.value,
                reason=body.reason,
                ref=ref,
            )
        await session.refresh(user)
        out = BalanceOut(
            applied=applied is not None, balance_kop=user.balance_kop, referral_balance_kop=user.referral_balance_kop
        )
    if applied is not None and body.notify and body.amount_kop > 0:
        amount = body.amount_kop
        notify.notify_user(
            ctx,
            user_id,
            lambda lang: notify.t(
                ctx, "admin_api.notify.balance_credited", lang, amount=notify.money(ctx, amount, lang)
            ),
        )
    return out


# ---------------------------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------------------------
async def _view(ctx: Any, user_id: int, subscription_id: int | None = None) -> SubscriptionOut:
    async with read_session(ctx.session_factory) as session:
        sub = await require_subscription(session, user_id, subscription_id=subscription_id)
        return await subscription_view(session, sub, utcnow())


async def _stop_stars_renewal(ctx: Any, user_id: int, subscription_id: int | None = None) -> None:
    """Cancel the user's native Stars subscription: Telegram keeps charging it until the bot cancels it."""
    async with read_session(ctx.session_factory) as session:
        await require_subscription(session, user_id, subscription_id=subscription_id)
    if not await cancel_stars_renewal(ctx.bot, ctx.session_factory, user_id, subscription_id):
        raise conflict("stars_cancel_failed", "Could not cancel the user's Telegram Stars subscription, try again")


@router.post("/{user_id}/days", response_model=SubscriptionOut)
async def grant_days(
    user_id: UserId, body: DaysIn, actor: Granter, subscription_id: int | None = None
) -> SubscriptionOut:
    """Add free days (creates the subscription if needed); optionally with a plan's limits."""
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        user = await get_user(session, user_id)
        plan = None
        if body.plan_id is not None:
            plan = await session.get(Plan, body.plan_id)
            if plan is None:
                raise bad_request("unknown_plan", "No such plan")
        try:
            purchase = await subscriptions.grant_days(
                session,
                ctx.settings,
                user,
                body.days,
                kind=PurchaseKind.ADMIN_GRANT,
                plan=plan,
                subscription_id=subscription_id,
            )
        except ValueError as exc:
            raise bad_request("bad_days", str(exc)) from None
        purchase.actor_id = actor.tg_id
        await actor.audit(
            session,
            "subscription.grant_days",
            "user",
            user_id,
            days=body.days,
            plan_id=body.plan_id,
            expires_at=purchase.expires_after.isoformat(),
        )
        sub = await require_subscription(session, user_id, subscription_id=subscription_id)
        expires = purchase.expires_after
    schedule_sync(ctx, sub.id)
    if body.notify:
        days = body.days
        notify.notify_user(
            ctx,
            user_id,
            lambda lang: notify.t(
                ctx, "admin_api.notify.days_granted", lang, days=days, expires=notify.date(ctx, expires, lang)
            ),
        )
    return await _view(ctx, user_id, subscription_id)


async def _set_expiry(
    actor: Actor, user_id: int, action: str, compute: Any, subscription_id: int | None = None, **details: Any
) -> SubscriptionOut:
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        sub = await require_subscription(session, user_id, lock=True, subscription_id=subscription_id)
        before = sub.expires_at
        sub.expires_at = compute(sub)
        now = utcnow()
        if sub.status == SubscriptionStatus.EXPIRED and sub.expires_at > now:
            sub.status = SubscriptionStatus.ACTIVE
        sub.last_reminder_days = None
        sub.revision += 1
        await provisioning.ensure_provisions(session, sub)
        await actor.audit(
            session,
            action,
            "user",
            user_id,
            expires_before=before.isoformat(),
            expires_after=sub.expires_at.isoformat(),
            **details,
        )
        sub_id = sub.id
    schedule_sync(ctx, sub_id)
    return await _view(ctx, user_id, subscription_id)


@router.post("/{user_id}/days/remove", response_model=SubscriptionOut)
async def remove_days(
    user_id: UserId, body: RemoveDaysIn, actor: Editor, subscription_id: int | None = None
) -> SubscriptionOut:
    """Take days away (the subscription ends earlier; it may end right away)."""
    return await _set_expiry(
        actor,
        user_id,
        "subscription.remove_days",
        lambda sub: sub.expires_at - timedelta(days=body.days),
        days=body.days,
        subscription_id=subscription_id,
    )


@router.put("/{user_id}/expiry", response_model=SubscriptionOut)
async def set_expiry(
    user_id: UserId, body: ExpiryIn, actor: Granter, subscription_id: int | None = None
) -> SubscriptionOut:
    """Set the exact end of the subscription."""
    return await _set_expiry(
        actor, user_id, "subscription.set_expiry", lambda sub: body.expires_at, subscription_id=subscription_id
    )


@router.put("/{user_id}/limits", response_model=SubscriptionOut)
async def set_limits(
    user_id: UserId, body: LimitsIn, actor: Granter, subscription_id: int | None = None
) -> SubscriptionOut:
    """Change the plan and/or device and traffic limits of the subscription."""
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        sub = await require_subscription(session, user_id, lock=True, subscription_id=subscription_id)
        changes: dict[str, Any] = {}
        if body.plan_id is not None:
            plan = await session.get(Plan, body.plan_id)
            if plan is None:
                raise bad_request("unknown_plan", "No such plan")
            changes["plan_id"] = plan.id
            if body.apply_plan_limits:
                changes["device_limit"] = plan.device_limit
                changes["traffic_limit_gb"] = plan.traffic_limit_gb
        if body.device_limit is not None:
            changes["device_limit"] = body.device_limit
        if body.traffic_limit_gb is not None:
            changes["traffic_limit_gb"] = body.traffic_limit_gb
        if not changes:
            raise bad_request("nothing_to_change", "Nothing to change")
        if "traffic_limit_gb" in changes:
            sub.traffic_limit_bytes = None
        previous = {key: getattr(sub, key) for key in changes}
        for key, value in changes.items():
            setattr(sub, key, value)
        sub.revision += 1
        await actor.audit(session, "subscription.limits", "user", user_id, previous=previous, new=changes)
        sub_id = sub.id
    schedule_sync(ctx, sub_id)
    return await _view(ctx, user_id, subscription_id)


@router.put("/{user_id}/auto-renew", response_model=SubscriptionOut)
async def set_auto_renew(
    user_id: UserId, body: FlagIn, actor: Editor, subscription_id: int | None = None
) -> SubscriptionOut:
    ctx = actor.ctx
    if not body.value:
        await _stop_stars_renewal(ctx, user_id, subscription_id)
    async with ctx.session_factory.begin() as session:
        sub = await require_subscription(session, user_id, lock=True, subscription_id=subscription_id)
        await subscriptions.set_auto_renew(session, sub, body.value)
        await actor.audit(session, "subscription.auto_renew", "user", user_id, value=body.value)
    return await _view(ctx, user_id, subscription_id)


@router.put("/{user_id}/complimentary", response_model=SubscriptionOut)
async def set_complimentary(
    user_id: UserId, body: FlagIn, actor: Manager, subscription_id: int | None = None
) -> SubscriptionOut:
    """Free access: no payment reminders, no auto-renewal, excluded from sales statistics."""
    ctx = actor.ctx
    if body.value:
        await _stop_stars_renewal(ctx, user_id, subscription_id)
    async with ctx.session_factory.begin() as session:
        sub = await require_subscription(session, user_id, lock=True, subscription_id=subscription_id)
        sub.is_complimentary = body.value
        if body.value:
            sub.auto_renew = False
        await actor.audit(session, "subscription.complimentary", "user", user_id, value=body.value)
    return await _view(ctx, user_id, subscription_id)


@router.put("/{user_id}/status", response_model=SubscriptionOut)
async def set_status(
    user_id: UserId, body: SubStatusIn, actor: Editor, subscription_id: int | None = None
) -> SubscriptionOut:
    """Block the subscription (panel accounts are disabled) or unblock it."""
    ctx = actor.ctx
    target = SubscriptionStatus.DISABLED if body.status == "disabled" else SubscriptionStatus.ACTIVE
    async with ctx.session_factory.begin() as session:
        sub = await require_subscription(session, user_id, lock=True, subscription_id=subscription_id)
        if target == SubscriptionStatus.ACTIVE and sub.expires_at <= utcnow():
            target = SubscriptionStatus.EXPIRED
        old = sub.status
        if old != target:
            sub.status = target
            sub.revision += 1
            await provisioning.ensure_provisions(session, sub)
            await actor.audit(session, "subscription.set_status", "user", user_id, old=old.value, new=target.value)
        sub_id = sub.id
    schedule_sync(ctx, sub_id)
    return await _view(ctx, user_id, subscription_id)


@router.post("/{user_id}/devices/reset", response_model=ResetOut)
async def reset_devices(
    user_id: UserId, body: DevicesResetIn, actor: Editor, subscription_id: int | None = None
) -> ResetOut:
    """Unlink all devices of the user (or one device on one panel)."""
    ctx = actor.ctx
    async with read_session(ctx.session_factory) as session:
        sub = await require_subscription(session, user_id, subscription_id=subscription_id)
    try:
        result = await devices.reset(
            ctx.session_factory,
            ctx.panels,
            sub.id,
            device_id=body.device_id,
            panel_id=body.panel_id,
            cooldown=timedelta(0),
            actor_id=actor.tg_id,
            source="web",
            ip=actor.ip,
        )
    except devices.DeviceNotSupported as exc:
        raise bad_request("not_supported", str(exc)) from None
    except devices.DeviceError as exc:
        raise bad_request("devices_error", str(exc)) from None
    return ResetOut(reset=result.reset, failed=result.failed, errors=result.errors)


@router.post("/{user_id}/rotate-link", response_model=RotateOut)
async def rotate_link(user_id: UserId, actor: Editor, subscription_id: int | None = None) -> RotateOut:
    """Issue a new subscription link; the old one stops working immediately."""
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        user = await get_user(session, user_id, lock=True)
        sub = await require_subscription(session, user_id, subscription_id=subscription_id)
        await subscriptions.rotate_token(session, user, sub)
        await actor.audit(session, "user.rotate_link", "user", user_id)
        url = subscription_url(ctx.settings, user, sub)
    return RotateOut(subscription_url=url)


@router.post("/{user_id}/resync", response_model=SyncOut)
async def resync(user_id: UserId, actor: Editor, subscription_id: int | None = None) -> SyncOut:
    """Push the subscription to every panel right now."""
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        sub = await require_subscription(session, user_id, lock=True, subscription_id=subscription_id)
        sub.revision += 1
        await actor.audit(session, "subscription.resync", "user", user_id)
        sub_id = sub.id
    result = await provisioning.sync_subscription(ctx.session_factory, ctx.panels, ctx.settings, sub_id)
    return SyncOut(
        found=result.found, synced=result.synced, failed=result.failed, skipped=result.skipped, errors=result.errors
    )


@router.put("/{user_id}/ban", response_model=UserOut)
async def set_banned(user_id: UserId, body: FlagIn, actor: Editor) -> UserOut:
    """Ban (no bot access, panel accounts disabled) or unban a user."""
    ctx = actor.ctx
    if body.value and await _is_staff(ctx, user_id):
        raise conflict("is_admin", "Admins cannot be banned; remove the admin role first")
    async with ctx.session_factory.begin() as session:
        user = await get_user(session, user_id, lock=True)
        if await bans.set_banned_manually(session, user, body.value):
            await actor.audit(session, "user.ban" if body.value else "user.unban", "user", user_id)
        out = UserOut.model_validate(user)
    kick_reconcile(ctx)
    return out


@router.post("/bulk-ban")
async def bulk_ban(body: BulkBanIn, actor: BulkEditor) -> dict[str, int]:
    """Ban or unban up to 10,000 known customers, skipping staff accounts safely."""
    ctx = actor.ctx
    changed = skipped = 0
    async with ctx.session_factory.begin() as session:
        rows = list((await session.scalars(select(User).where(User.tg_id.in_(body.user_ids)).with_for_update())).all())
        staff = {user.tg_id for user in rows if await resolve_role(session, ctx.settings, user.tg_id) is not None}
        for user in rows:
            if body.value and user.tg_id in staff:
                skipped += 1
                continue
            if not await bans.set_banned_manually(session, user, body.value):
                continue
            changed += 1
        await actor.audit(
            session,
            "user.bulk_ban" if body.value else "user.bulk_unban",
            "user",
            None,
            requested=len(body.user_ids),
            changed=changed,
            skipped_staff=skipped,
            reason=body.reason,
        )
    if changed:
        kick_reconcile(ctx)
    return {"changed": changed, "unknown": len(body.user_ids) - len(rows), "skipped_staff": skipped}


async def _is_staff(ctx: Any, user_id: int) -> bool:
    async with read_session(ctx.session_factory) as session:
        return await resolve_role(session, ctx.settings, user_id) is not None


@router.post("/{user_id}/message", response_model=MessageOut)
async def message_user(user_id: UserId, body: MessageIn, actor: Editor) -> MessageOut:
    """Send the user a message from the bot (Telegram HTML)."""
    ctx = actor.ctx
    try:
        check_html(body.text)
    except TextValidationError as exc:
        raise bad_request("bad_html", str(exc)) from None
    async with read_session(ctx.session_factory) as session:
        await get_user(session, user_id)
    delivered = await safe_send(
        ctx.bot,
        user_id,
        body.text,
        session_factory=ctx.session_factory,
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=body.disable_preview),
    )
    async with ctx.session_factory.begin() as session:
        await actor.audit(session, "user.message", "user", user_id, length=len(body.text), delivered=delivered)
    return MessageOut(delivered=delivered)


# ---------------------------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------------------------
async def _remove_accounts(ctx: Any, sub: Subscription) -> tuple[int, list[str]]:
    """Delete the subscription's accounts from every panel (network I/O, no transaction open)."""
    async with read_session(ctx.session_factory) as session:
        rows = (
            await session.execute(
                select(Provision.panel_id, Provision.inbound_id, Provision.remote_email, Provision.actual_state).where(
                    Provision.subscription_id == sub.id
                )
            )
        ).all()
        remote_ids = await _inbound_remote_ids(session, [r[1] for r in rows if r[1] is not None])
    semaphore = asyncio.Semaphore(DELETE_CONCURRENCY)
    errors: list[str] = []
    removed = 0

    async def remove(panel_id: int, inbound_id: int | None, email: str | None) -> None:
        nonlocal removed
        if not email:
            return
        target = remote_ids.get(inbound_id) if inbound_id is not None else None
        async with semaphore:
            try:
                backend = await ctx.panels.get(panel_id)
                await backend.delete_account(target, email, sub.client_uuid)
                removed += 1
            except (PanelError, LookupError, OSError, TimeoutError) as exc:
                errors.append(f"panel {panel_id}: {str(exc)[:200] or type(exc).__name__}")

    await asyncio.gather(
        *(
            remove(panel_id, inbound_id, email)
            for panel_id, inbound_id, email, state in rows
            if state != ProvisionState.ABSENT
        )
    )
    return removed, errors


async def _inbound_remote_ids(session: AsyncSession, inbound_ids: list[int]) -> dict[int, int]:
    if not inbound_ids:
        return {}
    rows = await session.execute(select(Inbound.id, Inbound.remote_id).where(Inbound.id.in_(inbound_ids)))
    return {int(i): int(r) for i, r in rows}


@router.delete("/{user_id}", response_model=DeleteOut)
async def delete_user(
    user_id: UserId,
    actor: Manager,
    remove_accounts: bool = True,
    force: bool = False,
) -> DeleteOut:
    """Delete a user with all their data. Their panel accounts are removed first (``force`` deletes even if a
    panel cannot be reached; the leftovers are then cleaned up as orphans)."""
    ctx = actor.ctx
    if await _is_staff(ctx, user_id):
        raise conflict("is_admin", "Remove the admin role before deleting this user")
    async with read_session(ctx.session_factory) as session:
        await get_user(session, user_id)
        owned = await subscriptions.list_for_user(session, user_id)
    removed, errors = 0, list[str]()
    if remove_accounts:
        for sub in owned:
            count, failures = await _remove_accounts(ctx, sub)
            removed += count
            errors.extend(failures)
        if errors and not force:
            raise conflict("panels_failed", "Some panel accounts could not be removed: " + "; ".join(errors[:5]))
    # Telegram keeps charging a Stars subscription after the user is gone; those charges would match no payment.
    linked = any(sub.stars_charge_id is not None for sub in owned)
    if linked and not await cancel_stars_renewal(ctx.bot, ctx.session_factory, user_id):
        if not force:
            raise conflict("stars_cancel_failed", "Could not cancel the user's Telegram Stars subscription, try again")
        errors.append("Telegram Stars subscription could not be canceled")
    async with ctx.session_factory.begin() as session:
        result = await session.execute(
            sa_delete(User).where(User.tg_id == user_id).execution_options(synchronize_session=False)
        )
        if not getattr(result, "rowcount", 0):
            return DeleteOut(deleted=False, accounts_removed=removed, errors=errors)
        await actor.audit(
            session, "user.delete", "user", user_id, accounts_removed=removed, panel_errors=len(errors), forced=force
        )
    if errors:
        kick_reconcile(ctx)
    return DeleteOut(deleted=True, accounts_removed=removed, errors=errors)


__all__ = ["router"]


@router.get("/{user_id}/subscriptions", response_model=list[SubscriptionOut])
async def user_subscriptions(user_id: UserId, actor: Viewer, session: ReadDb) -> list[SubscriptionOut]:
    await get_user(session, user_id)
    return [
        await subscription_view(session, sub, utcnow()) for sub in await subscriptions.list_for_user(session, user_id)
    ]
