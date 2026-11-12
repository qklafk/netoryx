"""Profile, subscription card, connect data and devices."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.config import Settings
from app.db.models import Plan, Subscription, SubscriptionStatus, User
from app.db.session import read_session
from app.modules.miniapp_api import deps
from app.modules.miniapp_api.deps import PLANS, SUBSCRIPTION, WALLET, error, needs
from app.modules.miniapp_api.schemas import (
    AppOut,
    AutoRenewIn,
    AutoRenewOut,
    ConnectOut,
    DeviceOut,
    DeviceResetIn,
    DeviceResetOut,
    DeviceServerOut,
    DevicesOut,
    DownloadOut,
    LanguageIn,
    LanguageOut,
    MeOut,
    PlanBrief,
    RotateLinkOut,
    SelectionIn,
    SubscriptionLabelIn,
    SubscriptionOut,
    SubscriptionsOut,
    UsageDay,
    UsageOut,
)
from app.modules.subscription.stars_renewal import cancel_stars_renewal
from app.services import devices, invites, provisioning, subscriptions, tickets, usage, users
from app.services.connection_links import connection_groups
from app.services.deeplinks import APPS, Platform, happ_crypt_link, incy_crypt_link
from app.services.subscription_page import access_state
from app.services.subscription_page import usage as traffic_usage
from app.services.subscriptions import SubErrorCode, SubscriptionError
from app.web.auth import MiniAppUser
from app.web.deps import Ctx, DbSession, ReadDb

log = logging.getLogger(__name__)

router = APIRouter(tags=["Mini App: account"])

ROTATE_LINK_LIMIT = (3, 3600)  # rotations per user per hour
DEVICE_RESET_LIMIT = (10, 3600)  # attempts per user per hour (the service also enforces its cooldown)
TRIAL_LIMIT = (5, 600)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def trial_available(settings: Settings, user: User, sub: Subscription | None) -> bool:
    return settings.app_mode == "shop" and settings.trial_days > 0 and not user.trial_used and sub is None


def days_left(expires_at: datetime, now: datetime) -> int:
    seconds = (expires_at - now).total_seconds()
    return max(0, math.ceil(seconds / 86400))


def subscription_card(
    settings: Settings,
    user: User,
    sub: Subscription | None,
    plan: Plan | None,
    now: datetime,
    *,
    used_bytes: int | None = None,
    wallet_on: bool = False,
) -> SubscriptionOut:
    state = access_state(user, sub, now)
    if sub is None:
        return SubscriptionOut(exists=False, state=state.value, trial_available=trial_available(settings, user, None))
    unlimited = invites.is_unlimited(sub)
    return SubscriptionOut(
        id=sub.id,
        label=sub.label,
        exists=True,
        state=state.value,
        expires_at=None if unlimited else sub.expires_at,
        days_left=0 if unlimited else days_left(sub.expires_at, now),
        unlimited=unlimited,
        plan=PlanBrief(id=plan.id, title=plan.title, duration_days=plan.duration_days) if plan else None,
        device_limit=sub.device_limit,
        traffic_limit_gb=sub.traffic_limit_gb,
        traffic_used_bytes=used_bytes,
        auto_renew=sub.auto_renew,
        auto_renew_available=wallet_on and not unlimited and not sub.is_complimentary,
        is_trial=sub.is_trial,
        is_complimentary=sub.is_complimentary,
        stars_recurring=sub.stars_charge_id is not None,
        trial_available=False,
    )


async def load_subscription(
    ctx: Any, user_id: int, subscription_id: int | None = None
) -> tuple[Subscription | None, Plan | None]:
    async with read_session(ctx.session_factory) as session:
        sub = await subscriptions.get_for_user(session, user_id, subscription_id)
        if subscription_id is not None and sub is None:
            raise deps.ApiError(404, "no_subscription", "Subscription not found")
        plan = await session.get(Plan, sub.plan_id) if sub is not None and sub.plan_id is not None else None
    return sub, plan


async def require_subscription(ctx: Any, user: User, subscription_id: int | None = None) -> Subscription:
    sub, _ = await load_subscription(ctx, user.tg_id, subscription_id)
    if sub is None:
        raise error(ctx, user, 404, "no_subscription")
    return sub


async def sync_subscription(ctx: Any, sub_id: int) -> bool:
    """Push the subscription to the panels now (the reconciler retries failures). Never raises."""
    try:
        result = await provisioning.sync_subscription(ctx.session_factory, ctx.panels, ctx.settings, sub_id)
    except Exception:
        log.exception("mini app: syncing subscription %s failed; the reconciler will retry", sub_id)
        return False
    return result.failed == 0


# ---------------------------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------------------------
@router.get("/me", response_model=MeOut, summary="Profile, balances, language and available sections")
async def me(ctx: Ctx, user: MiniAppUser, db: ReadDb) -> MeOut:
    settings: Settings = ctx.settings
    sub = await subscriptions.get_for_user(db, user.tg_id)
    unread = await tickets.unread_for_user(db, user.tg_id)
    features = await deps.features_for(ctx, user.tg_id)
    return MeOut(
        id=user.tg_id,
        username=user.username,
        first_name=user.first_name,
        language=user.language,
        languages=list(users.enabled_languages(settings)),
        mode=settings.app_mode,
        brand=settings.brand_name,
        brand_logo_url=settings.brand_logo_url,
        bot_username=getattr(ctx, "bot_username", None) or None,
        support_url=settings.support_url,
        balance_kop=user.balance_kop,
        partner_balance_kop=user.referral_balance_kop,
        has_subscription=sub is not None,
        trial_available=features[PLANS] and trial_available(settings, user, sub),
        trial_days=settings.trial_days,
        unread_tickets=unread if features[deps.TICKETS] else 0,
        features=features,
    )


@router.post("/me/language", response_model=LanguageOut, summary="Switch the language")
async def set_language(ctx: Ctx, body: LanguageIn, user: MiniAppUser, db: DbSession) -> LanguageOut:
    if body.language not in users.enabled_languages(ctx.settings):
        raise deps.ApiError(422, "unsupported_language", "This language is not available")
    row = await db.get(User, user.tg_id)
    assert row is not None  # the dependency has just loaded it
    await users.set_language(db, row, body.language)
    return LanguageOut(language=row.language)


# ---------------------------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------------------------
@router.get(
    "/subscription",
    response_model=SubscriptionOut,
    dependencies=[needs(SUBSCRIPTION)],
    summary="Subscription card (status, expiry, plan, limits, traffic)",
)
async def subscription(
    ctx: Ctx,
    user: MiniAppUser,
    traffic: Annotated[bool, Query(description="Ask the servers for traffic")] = True,
    subscription_id: int | None = None,
) -> SubscriptionOut:
    sub, plan = await load_subscription(ctx, user.tg_id, subscription_id)
    used: int | None = None
    if sub is not None and traffic:
        try:
            up, down = await traffic_usage(ctx.session_factory, ctx.panels, sub, ctx.settings)
            used = up + down
        except Exception:
            log.warning("mini app: traffic of subscription %s is unavailable", sub.id, exc_info=True)
    wallet_on = await deps.feature_on(ctx, WALLET, user.tg_id)
    return subscription_card(ctx.settings, user, sub, plan, deps.now_utc(), used_bytes=used, wallet_on=wallet_on)


@router.get(
    "/subscription/usage",
    response_model=UsageOut,
    dependencies=[needs(SUBSCRIPTION)],
    summary="Traffic per day",
)
async def subscription_usage(
    ctx: Ctx,
    user: MiniAppUser,
    db: ReadDb,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
    subscription_id: int | None = None,
) -> UsageOut:
    sub = await subscriptions.get_for_user(db, user.tg_id, subscription_id)
    if sub is None:
        raise error(ctx, user, 404, "no_subscription")
    rows = await usage.daily_usage(db, sub.id, ctx.settings, days=days)
    return UsageOut(days=[UsageDay(day=d, bytes=b) for d, b in rows], total_bytes=sum(b for _, b in rows))


@router.post(
    "/subscription/auto-renew",
    response_model=AutoRenewOut,
    dependencies=[needs(SUBSCRIPTION, WALLET)],
    summary="Turn renewal from the balance on or off",
)
async def auto_renew(body: AutoRenewIn, ctx: Ctx, user: MiniAppUser) -> AutoRenewOut:
    selected = await require_subscription(ctx, user, body.subscription_id)
    if not body.enabled and not await cancel_stars_renewal(ctx.bot, ctx.session_factory, user.tg_id, selected.id):
        # Telegram would keep charging Stars: auto-renewal stays on until the cancellation goes through.
        raise error(ctx, user, 503, "stars_cancel_failed")
    async with ctx.session_factory.begin() as db:
        sub = await subscriptions.lock_for_user(db, user.tg_id, body.subscription_id)
        if sub is None:
            raise error(ctx, user, 404, "no_subscription")
        if invites.is_unlimited(sub) or sub.is_complimentary:
            raise error(ctx, user, 409, "auto_renew_unavailable")
        await subscriptions.set_auto_renew(db, sub, body.enabled)
        return AutoRenewOut(auto_renew=sub.auto_renew)


@router.post(
    "/subscription/trial",
    response_model=SubscriptionOut,
    dependencies=[needs(SUBSCRIPTION, PLANS)],
    summary="Start the free trial",
)
async def start_trial(request: Request, ctx: Ctx, user: MiniAppUser) -> SubscriptionOut:
    settings: Settings = ctx.settings
    await deps.rate_limit(request, ctx, user, "trial", *TRIAL_LIMIT)
    if settings.app_mode != "shop":
        raise error(ctx, user, 409, "trial_disabled")
    try:
        async with ctx.session_factory.begin() as session:
            row = await session.get(User, user.tg_id)
            assert row is not None
            sub = await subscriptions.start_trial(session, settings, row)
            sub_id = sub.id
    except SubscriptionError as exc:
        raise _sub_error(ctx, user, exc) from None
    await sync_subscription(ctx, sub_id)
    sub_now, plan = await load_subscription(ctx, user.tg_id)
    async with read_session(ctx.session_factory) as session:
        fresh = await session.get(User, user.tg_id)
    wallet_on = await deps.feature_on(ctx, WALLET, user.tg_id)
    return subscription_card(settings, fresh or user, sub_now, plan, deps.now_utc(), wallet_on=wallet_on)


@router.post(
    "/subscription/rotate-link",
    response_model=RotateLinkOut,
    dependencies=[needs(SUBSCRIPTION)],
    summary="Issue a new subscription link (the old one stops working)",
)
async def rotate_link(request: Request, ctx: Ctx, user: MiniAppUser, body: SelectionIn | None = None) -> RotateLinkOut:
    body = body or SelectionIn()
    base = deps.public_base(ctx, user)
    await deps.rate_limit(request, ctx, user, "rotate_link", *ROTATE_LINK_LIMIT)
    async with ctx.session_factory.begin() as session:
        row = await session.get(User, user.tg_id)
        assert row is not None
        sub = await subscriptions.get_for_user(session, row.tg_id, body.subscription_id)
        if sub is None:
            if body.subscription_id is not None:
                raise error(ctx, user, 404, "no_subscription")
            token = await users.rotate_sub_token(session, row)
        else:
            token = await subscriptions.rotate_token(session, row, sub)
    log.info("mini app: user %s rotated the subscription link", user.tg_id)
    return RotateLinkOut(sub_url=deps.subscription_url(base, token))


def _sub_error(ctx: Any, user: User, exc: SubscriptionError) -> Exception:
    status = {
        SubErrorCode.INSUFFICIENT_FUNDS: 402,
        SubErrorCode.NO_PLAN: 404,
        SubErrorCode.BANNED: 403,
    }.get(exc.code, 409)
    return error(ctx, user, status, exc.code.value)


# ---------------------------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------------------------
@router.get(
    "/connect",
    response_model=ConnectOut,
    dependencies=[needs(SUBSCRIPTION)],
    summary="Subscription URL, one-tap app links, encrypted links and QR code",
)
async def connect(
    request: Request,
    ctx: Ctx,
    user: MiniAppUser,
    platform: Annotated[Platform | None, Query()] = None,
    subscription_id: int | None = None,
) -> ConnectOut:
    settings: Settings = ctx.settings
    base = deps.public_base(ctx, user)
    sub, _ = await load_subscription(ctx, user.tg_id, subscription_id)
    token = subscriptions.token_for(sub, user)
    sub_url = deps.subscription_url(base, token)
    name = settings.profile_title
    apps = [app for app in APPS.values() if platform is None or platform in app.platforms]
    happ = None
    http = getattr(ctx, "http", None)
    if settings.sub_happ_crypto_links and http is not None:
        happ = await happ_crypt_link(ctx.session_factory, http, sub_url)
    incy = incy_crypt_link(sub_url, name) if settings.sub_incy_crypto_links else None
    all_platforms = list(dict.fromkeys(p.value for app in APPS.values() for p in app.platforms))
    return ConnectOut(
        state=access_state(user, sub, deps.now_utc()).value,
        sub_url=sub_url,
        whitelist_url=f"{sub_url}/wl" if settings.sub_whitelist_separate_url else None,
        qr=await deps.qr_data_url(sub_url),
        happ_crypt_link=happ,
        incy_crypt_link=incy,
        platforms=all_platforms,
        platform=platform.value if platform else None,
        servers=await connection_groups(ctx, user, sub, dict(request.headers)),
        apps=[
            AppOut(
                key=app.key,
                title=app.title,
                platforms=[p.value for p in app.platforms],
                downloads=[
                    DownloadOut(platform=d.platform.value, label=d.label, url=d.url)
                    for d in app.downloads
                    if platform is None or d.platform == platform
                ],
                deeplink=app.deeplink(sub_url, name),
                open_url=f"{base}/open/{app.key}/{token}",
                supports_crypto_link=app.supports_crypto_link,
            )
            for app in apps
        ],
    )


# ---------------------------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------------------------
@router.get(
    "/devices",
    response_model=DevicesOut,
    dependencies=[needs(SUBSCRIPTION)],
    summary="Devices connected to the subscription",
)
async def list_devices(ctx: Ctx, user: MiniAppUser, subscription_id: int | None = None) -> DevicesOut:
    sub = await require_subscription(ctx, user, subscription_id)
    per_panel = await devices.list_devices(ctx.session_factory, ctx.panels, sub.id)
    last = await devices.last_reset(ctx.session_factory, sub.id)
    return DevicesOut(
        device_limit=sub.device_limit,
        servers=[
            DeviceServerOut(
                panel_id=entry.panel_id,
                server=entry.panel_name,
                supported=entry.supported,
                can_delete_one=entry.can_delete_one,
                unavailable=entry.error is not None,
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
                    for d in entry.devices
                ],
            )
            for entry in per_panel
        ],
        last_reset_at=last,
        next_reset_at=_next_reset(ctx.settings, last),
    )


def _next_reset(settings: Settings, last: datetime | None) -> datetime | None:
    cooldown = devices.cooldown_from(settings)
    if last is None or cooldown <= timedelta(0):
        return None
    ready = last + cooldown
    return ready if ready > deps.now_utc() else None


@router.post(
    "/devices/reset",
    response_model=DeviceResetOut,
    dependencies=[needs(SUBSCRIPTION)],
    summary="Unlink all devices or one device",
)
async def reset_devices(body: DeviceResetIn, request: Request, ctx: Ctx, user: MiniAppUser) -> DeviceResetOut:
    if body.device_id is not None and body.panel_id is None:
        raise error(ctx, user, 422, "device_needs_server")
    await deps.rate_limit(request, ctx, user, "devices_reset", *DEVICE_RESET_LIMIT)
    sub = await require_subscription(ctx, user, body.subscription_id)
    if sub.status == SubscriptionStatus.DISABLED:
        raise error(ctx, user, 409, "subscription_disabled")
    try:
        result = await devices.reset(
            ctx.session_factory,
            ctx.panels,
            sub.id,
            device_id=body.device_id,
            panel_id=body.panel_id,
            cooldown=devices.cooldown_from(ctx.settings),
        )
    except devices.ResetCooldown as exc:
        retry = max(1, math.ceil((exc.retry_after - deps.now_utc()).total_seconds()))
        raise error(
            ctx,
            user,
            429,
            "devices_cooldown",
            headers={"Retry-After": str(retry)},
            extra={"retry_at": exc.retry_after.isoformat()},
            time=deps.translator(ctx, user).datetime(exc.retry_after),
        ) from None
    except devices.DeviceNotSupported:
        raise error(ctx, user, 409, "device_delete_unsupported") from None
    if result.reset == 0:
        raise error(ctx, user, 502 if result.failed else 404, "servers_unavailable" if result.failed else "no_devices")
    return DeviceResetOut(reset=result.reset, failed=result.failed)


@router.get("/subscriptions", response_model=SubscriptionsOut, dependencies=[needs(SUBSCRIPTION)])
async def list_subscriptions(ctx: Ctx, user: MiniAppUser, db: ReadDb) -> SubscriptionsOut:
    rows = await subscriptions.list_for_user(db, user.tg_id)
    plans = {p.id: p for p in await db.scalars(select(Plan))}
    wallet_on = await deps.feature_on(ctx, WALLET, user.tg_id)
    return SubscriptionsOut(
        items=[
            subscription_card(
                ctx.settings,
                user,
                sub,
                plans.get(sub.plan_id) if sub.plan_id is not None else None,
                deps.now_utc(),
                wallet_on=wallet_on,
            )
            for sub in rows
        ]
    )


@router.patch("/subscriptions/{subscription_id}", response_model=SubscriptionOut, dependencies=[needs(SUBSCRIPTION)])
async def rename_subscription(
    subscription_id: int, body: SubscriptionLabelIn, ctx: Ctx, user: MiniAppUser, db: DbSession
) -> SubscriptionOut:
    sub = await subscriptions.lock_for_user(db, user.tg_id, subscription_id)
    if sub is None:
        raise error(ctx, user, 404, "no_subscription")
    sub.label = body.label
    plan = await db.get(Plan, sub.plan_id) if sub.plan_id is not None else None
    return subscription_card(ctx.settings, user, sub, plan, deps.now_utc())
