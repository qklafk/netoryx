"""Background jobs of subscriptions and servers.

* ``subscriptions.expire`` (1 min) - mark ended subscriptions EXPIRED and push the change to the panels;
* ``subscriptions.auto_renew`` (10 min) - renew from the wallet, tell the user;
* ``subscriptions.reminders`` (15 min) - expiry reminders and the "expired" notice, never during quiet hours;
* ``provisioning.reconcile`` (``RECONCILE_INTERVAL_MIN``) - converge the panels to the database;
* ``usage.snapshot`` (daily) - traffic counters for charts;
* ``inventory.refresh_inbounds`` (hourly) - re-read inbounds of 3x-ui panels;
* ``panels.health`` (``HEALTH_CHECK_INTERVAL_MIN``) - ping panels, probe endpoints, alert admins on transitions.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.config import Settings
from app.core.modules import JobSpec
from app.core.ui import Screen
from app.db.models import Panel, PanelKind, Plan, Subscription, User
from app.db.session import read_session
from app.modules.subscription import notices
from app.services import notifications, provisioning, subscriptions, usage, winback
from app.services.notify import safe_send
from app.services.panels import inventory
from app.services.panels.base import PanelError
from app.services.payments import autopay
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.core.context import AppContext
    from app.core.i18n import Translator

log = logging.getLogger(__name__)

EXPIRE_JOB = "subscriptions.expire"
AUTO_RENEW_JOB = "subscriptions.auto_renew"
REMINDERS_JOB = "subscriptions.reminders"
RECONCILE_JOB = "provisioning.reconcile"
USAGE_JOB = "usage.snapshot"
INBOUNDS_JOB = "inventory.refresh_inbounds"
HEALTH_JOB = "panels.health"
WINBACK_JOB = "subscriptions.winback"

# Reminders are not sent at night (local time of TZ). Overridable by REMINDER_QUIET_START_HOUR /
# REMINDER_QUIET_END_HOUR; equal values disable the quiet hours.
SEND_INTERVAL_SEC = 0.04  # ~25 messages per second, under Telegram's limit
USAGE_KEEP_DAYS = usage.DEFAULT_KEEP_DAYS


# --- helpers ----------------------------------------------------------------------------------------------------------
def quiet_hours(settings: Settings) -> tuple[int, int]:
    return settings.reminder_quiet_start_hour % 24, settings.reminder_quiet_end_hour % 24


def in_quiet_hours(settings: Settings, now: datetime) -> bool:
    """Whether ``now`` (local time of TZ) falls into the quiet hours ``[start, end)`` (may wrap midnight)."""
    start, end = quiet_hours(settings)
    if start == end:
        return False
    hour = now.astimezone(settings.zone).hour
    return start <= hour < end if start < end else hour >= start or hour < end


def admin_translator(ctx: AppContext) -> Translator:
    return ctx.i18n.translator(ctx.i18n.default_lang, ctx.settings.zone)


async def send_screen(ctx: AppContext, chat_id: int, screen: Screen) -> bool:
    """Deliver a notification; marks users who blocked the bot; never raises."""
    return await safe_send(
        ctx.bot,
        chat_id,
        screen.text,
        session_factory=ctx.session_factory,
        reply_markup=screen.kb,
        parse_mode=screen.parse_mode,
    )


def trigger(ctx: AppContext, job_id: str) -> None:
    """Run another job of this process now (e.g. push expired subscriptions without waiting for its interval)."""
    scheduler = getattr(ctx, "scheduler", None)
    if scheduler is None or not scheduler.is_scheduled(job_id):
        return
    try:
        scheduler.run_now(job_id)
    except KeyError:
        log.debug("job %s is not registered", job_id)


async def _sync(ctx: AppContext, settings: Settings, sub_id: int) -> None:
    try:
        await provisioning.sync_subscription(ctx.session_factory, ctx.panels, settings, sub_id)
    except Exception:
        log.exception("sync of subscription %s failed; the reconciler will retry", sub_id)


# --- subscriptions ----------------------------------------------------------------------------------------------------
async def expire_job(ctx: AppContext) -> None:
    expired = await subscriptions.expire_due(ctx.session_factory, utcnow())
    if expired:
        log.info("%d subscriptions expired", len(expired))
        trigger(ctx, RECONCILE_JOB)


async def auto_renew_job(ctx: AppContext) -> None:
    settings = ctx.settings
    results = await subscriptions.run_auto_renew(ctx.session_factory, settings, utcnow())
    renewed = [
        (user_id, purchase.subscription_id)
        for user_id, purchase, outcome in results
        if outcome == subscriptions.RenewOutcome.RENEWED and purchase is not None
    ]
    for user_id, sub_id in renewed:
        await _after_renewal(ctx, settings, user_id, sub_id)
    if settings.saved_cards_enabled and ctx.modules.is_enabled("payments", settings):
        await _card_renewals(ctx, settings)


async def _card_renewals(ctx: AppContext, settings: Settings) -> None:
    """Charge saved cards for renewals the balance cannot cover; a paid charge renews through the payment hook."""
    charges = await autopay.charge_due(
        ctx.session_factory, settings, ctx.gateways.as_dict(), utcnow(), ctx.on_payment_credited
    )
    for charge in charges:
        if charge.outcome is not autopay.ChargeOutcome.DECLINED:
            continue
        async with read_session(ctx.session_factory) as session:
            lang = await session.scalar(select(User.language).where(User.tg_id == charge.user_id))
        if lang is None:
            continue
        _ = ctx.i18n.translator(lang, settings.zone)
        card = _.esc(charge.card_title)
        text = _("subscription.autopay.declined", amount=_.money(charge.amount_kop), card=card)
        if charge.card_removed:
            text += "\n\n" + _("subscription.autopay.card_removed", card=card)
        await send_screen(ctx, charge.user_id, Screen(text))


async def _after_renewal(ctx: AppContext, settings: Settings, user_id: int, subscription_id: int | None = None) -> None:
    async with read_session(ctx.session_factory) as session, session.begin():
        user = await session.get(User, user_id)
        sub = await subscriptions.get_for_user(session, user_id, subscription_id)
        plan = await session.get(Plan, sub.plan_id) if sub is not None and sub.plan_id is not None else None
    if user is None or sub is None or plan is None:
        return
    await _sync(ctx, settings, sub.id)
    _ = ctx.i18n.translator(user.language, settings.zone)
    await send_screen(ctx, user_id, notices.renewed(_, plan, sub, user.balance_kop))
    admin = admin_translator(ctx)
    who = f"@{user.username}" if user.username else (user.first_name or str(user_id))
    text = admin(
        "subscription.admin.renewed",
        user=admin.esc(who),
        user_id=user_id,
        plan=admin.esc(plan.title),
        price=admin.money(plan.price_kop),
    )
    notifications.notify(ctx, notifications.AdminEvent.PAYMENTS, text)


async def reminders_job(ctx: AppContext) -> None:
    settings = ctx.settings
    now = utcnow()
    if in_quiet_hours(settings, now):
        return
    async with read_session(ctx.session_factory) as session, session.begin():
        plans = {plan.id: plan for plan in await session.scalars(select(Plan))}
    wallet = ctx.modules.is_enabled("wallet", settings)

    async def send(sub: Subscription, days: int) -> bool:
        if sub.is_complimentary:
            return True  # free access: nothing to renew, recorded without a message
        user = sub.user
        _ = ctx.i18n.translator(user.language, settings.zone)
        plan = plans.get(sub.plan_id) if sub.plan_id is not None else None
        screen = notices.reminder(_, settings, sub, plan, user.balance_kop, days, wallet_enabled=wallet)
        delivered = await send_screen(ctx, user.tg_id, screen)
        await asyncio.sleep(SEND_INTERVAL_SEC)
        return delivered

    sent = await subscriptions.send_reminders(ctx.session_factory, settings, send, now)
    if sent:
        log.info("sent %d subscription reminders", sent)


async def winback_job(ctx: AppContext) -> None:
    """Offer a time-limited personal discount to eligible dormant users once per offer period."""
    offers = await winback.issue(ctx.session_factory, ctx.settings)
    for offer in offers:
        async with read_session(ctx.session_factory) as session:
            user = await session.get(User, offer.user_id)
        if user is None:
            continue
        _ = ctx.i18n.translator(user.language, ctx.settings.zone)
        await safe_send(
            ctx.bot,
            user.tg_id,
            _(
                "subscription.winback.offer",
                discount=offer.discount_percent,
                until=_.date(offer.expires_at),
            ),
            session_factory=ctx.session_factory,
        )


# --- panels -----------------------------------------------------------------------------------------------------------
async def reconcile_job(ctx: AppContext) -> None:
    report = await provisioning.reconcile(ctx.session_factory, ctx.panels, ctx.settings)
    if report.errors:
        log.warning("reconcile finished with %d errors, first: %s", len(report.errors), report.errors[0])


async def usage_job(ctx: AppContext) -> None:
    settings = ctx.settings
    report = await usage.snapshot(ctx.session_factory, ctx.panels, settings)
    async with ctx.session_factory.begin() as session:
        pruned = await usage.prune(session, settings, keep_days=USAGE_KEEP_DAYS)
    log.info(
        "usage snapshot %s: stored %d, skipped %d, panels failed %d, pruned %d",
        report.day,
        report.stored,
        report.skipped,
        report.panels_failed,
        pruned,
    )


async def refresh_inbounds_job(ctx: AppContext) -> None:
    async with read_session(ctx.session_factory) as session, session.begin():
        rows = (
            await session.execute(
                select(Panel.id, Panel.name).where(Panel.is_active.is_(True), Panel.kind == PanelKind.XUI)
            )
        ).all()
    _ = admin_translator(ctx)
    for panel_id, name in rows:
        try:
            result = await inventory.refresh_inbounds(ctx.session_factory, ctx.panels, panel_id)
        except (PanelError, LookupError) as exc:
            log.info("inbound refresh of panel %s failed: %s", panel_id, exc)
            continue
        if result.created:
            text = _("subscription.alert.inbounds_new", panel=_.esc(name), count=len(result.created))
            notifications.notify(ctx, notifications.AdminEvent.ALERTS, text)
        if result.deactivated:
            text = _("subscription.alert.inbounds_gone", panel=_.esc(name), count=len(result.deactivated))
            notifications.notify(ctx, notifications.AdminEvent.ALERTS, text)


async def health_job(ctx: AppContext) -> None:
    changes = await inventory.check_health(ctx.session_factory, ctx.panels, ctx.settings)
    if not changes:
        return
    _ = admin_translator(ctx)
    for change in changes:
        notifications.notify(ctx, notifications.AdminEvent.ALERTS, notices.health_alert(_, change))


def _minutes(value: int) -> dict[str, Any]:
    return {"minutes": max(1, value)}


JOBS = [
    JobSpec(id=EXPIRE_JOB, func=expire_job, kwargs={"minutes": 1}, lock_ttl_sec=300),
    JobSpec(id=AUTO_RENEW_JOB, func=auto_renew_job, kwargs={"minutes": 10}, lock_ttl_sec=900),
    JobSpec(id=REMINDERS_JOB, func=reminders_job, kwargs={"minutes": 15}, lock_ttl_sec=1800),
    JobSpec(id=WINBACK_JOB, func=winback_job, trigger="cron", kwargs={"hour": 12, "minute": 0}, lock_ttl_sec=3600),
    JobSpec(
        id=RECONCILE_JOB,
        func=reconcile_job,
        schedule=lambda s: _minutes(s.reconcile_interval_min),
        lock_ttl_sec=1800,
        run_on_start=True,
    ),
    JobSpec(id=USAGE_JOB, func=usage_job, trigger="cron", kwargs={"hour": 3, "minute": 30}, lock_ttl_sec=3600),
    JobSpec(id=INBOUNDS_JOB, func=refresh_inbounds_job, kwargs={"hours": 1}, lock_ttl_sec=900),
    JobSpec(
        id=HEALTH_JOB,
        func=health_job,
        schedule=lambda s: _minutes(s.health_check_interval_min),
        lock_ttl_sec=300,
    ),
]
