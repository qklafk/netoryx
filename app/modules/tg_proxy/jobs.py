"""Telegram proxy jobs: converge accounts on the proxy servers, and scan them for usage, drift and shared links."""

from __future__ import annotations

import logging
import weakref
from typing import TYPE_CHECKING, Any

from app.core.modules import JobSpec
from app.core.ui import Screen, send_screen
from app.db.models import User
from app.db.session import read_session
from app.keyboards import kb, nav
from app.modules.tg_proxy.callbacks import NAV
from app.services import tg_proxy, tg_socks
from app.services.notifications import AdminEvent, notify
from app.services.tg_proxy import AbuseDetector, AbuseEvent

if TYPE_CHECKING:
    from app.config import Settings
    from app.core.context import AppContext

log = logging.getLogger(__name__)

RECONCILE_INTERVAL_SEC = 60
ALERT_THROTTLE_SEC = 6 * 3600.0

# One detector per application: it counts consecutive scans in memory, so it must outlive single job runs.
_detectors: weakref.WeakKeyDictionary[Any, AbuseDetector] = weakref.WeakKeyDictionary()


def detector_for(ctx: AppContext) -> AbuseDetector:
    """The application's long-lived detector, with the strike count of the current settings."""
    strikes = max(1, ctx.settings.tg_proxy_abuse_strikes)
    detector = _detectors.get(ctx)
    if detector is None:
        detector = _detectors[ctx] = AbuseDetector(strikes)
    detector.strikes = strikes
    return detector


async def reconcile_job(ctx: AppContext) -> None:
    await tg_proxy.reconcile(ctx.session_factory, ctx.panels, ctx.settings, ctx.secret_box)
    await tg_socks.reconcile(ctx.session_factory, ctx.panels, ctx.settings, ctx.secret_box)


async def scan_job(ctx: AppContext) -> None:
    async def on_abuse(event: AbuseEvent) -> None:
        await report_abuse(ctx, event)

    report = await tg_proxy.scan(
        ctx.session_factory, ctx.panels, ctx.settings, ctx.secret_box, detector=detector_for(ctx), on_abuse=on_abuse
    )
    if report.errors:
        log.info("tg proxy scan: %d errors: %s", len(report.errors), "; ".join(report.errors[:3]))
    socks = await tg_socks.scan(ctx.session_factory, ctx.panels, ctx.settings, ctx.secret_box)
    if socks.errors:
        log.info("tg socks scan: %d errors: %s", len(socks.errors), "; ".join(socks.errors[:3]))


async def report_abuse(ctx: AppContext, event: AbuseEvent) -> None:
    """Tell the user their link was replaced (if it was) and the admins what happened."""
    i18n = ctx.i18n
    settings = ctx.settings
    admin_lang = settings.default_language
    key = "tg_proxy.admin_abuse_rotated" if event.rotated else "tg_proxy.admin_abuse"
    text = i18n.t(key, admin_lang, user_id=event.user_id, server=i18n.esc(event.title), ips=event.unique_ips)
    notify(ctx, AdminEvent.ALERTS, text, throttle_key=f"tgp-abuse:{event.access_id}", throttle_sec=ALERT_THROTTLE_SEC)
    if not event.rotated:
        return
    async with read_session(ctx.session_factory) as session:
        user = await session.get(User, event.user_id)
        if user is None or user.bot_blocked or user.is_banned:
            return
        lang = i18n.normalize(user.language)
    screen = Screen(
        i18n.t("tg_proxy.abuse_notice", lang, ips=event.unique_ips),
        kb([[nav(i18n.t("tg_proxy.btn.open", lang), NAV)]]),
    )
    try:
        await send_screen(ctx.bot, event.user_id, screen)
    except Exception as exc:  # the user may have blocked the bot; the admin alert is already queued
        log.info("tg proxy: could not notify user %s about a new link: %s", event.user_id, exc)


def _scan_schedule(settings: Settings) -> dict[str, Any]:
    return {"minutes": max(1, settings.tg_proxy_scan_interval_min)}


JOBS = [
    JobSpec(
        id="tg_proxy.reconcile", func=reconcile_job, trigger="interval", kwargs={"seconds": RECONCILE_INTERVAL_SEC}
    ),
    JobSpec(id="tg_proxy.scan", func=scan_job, trigger="interval", schedule=_scan_schedule, lock_ttl_sec=1800),
]
