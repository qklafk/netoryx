"""Background registration of "Мой налог" receipts, buyer notices and admin alerts."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.modules import JobSpec
from app.db.models import User
from app.db.session import read_session
from app.services import kv, tax_receipts
from app.services.notifications import AdminEvent, notify
from app.services.notify import safe_send
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.config import Settings
    from app.core.context import AppContext

log = logging.getLogger(__name__)

ALERT_KEY = "npd:auth_alert"
ALERT_EVERY = timedelta(hours=6)


async def _alert_due(ctx: AppContext) -> bool:
    """At most one "log in again" alert per ALERT_EVERY."""
    now = utcnow()
    async with ctx.session_factory.begin() as session:
        last = await kv.get(session, ALERT_KEY)
        try:
            if last and now - datetime.fromisoformat(last) < ALERT_EVERY:
                return False
        except ValueError:
            pass
        await kv.set(session, ALERT_KEY, now.isoformat())
    return True


async def _clear_alert(ctx: AppContext) -> None:
    async with ctx.session_factory.begin() as session:
        await kv.delete(session, ALERT_KEY)


async def announce(ctx: AppContext, receipt_ids: list[int]) -> int:
    """Send the receipt link to buyers who have not got it yet; returns how many were sent."""
    sent = 0
    for row in await tax_receipts.receipts_to_announce(ctx.session_factory, receipt_ids):
        async with read_session(ctx.session_factory) as session, session.begin():
            lang = await session.scalar(select(User.language).where(User.tg_id == row.user_id))
        if lang is None:
            await tax_receipts.mark_notified(ctx.session_factory, row.id)
            continue
        _ = ctx.i18n.translator(lang, ctx.settings.zone)
        text = _("npd_receipts.receipt", amount=_.money(row.amount_kop), url=_.esc(row.receipt_url or ""))
        if await safe_send(ctx.bot, row.user_id, text, session_factory=ctx.session_factory):
            sent += 1
        await tax_receipts.mark_notified(ctx.session_factory, row.id)
    return sent


async def process_job(ctx: AppContext) -> None:
    settings = ctx.settings
    result = await tax_receipts.process_due(ctx.session_factory, settings, ctx.secret_box)
    lang = ctx.i18n.default_lang
    if result.auth_error is not None:
        log.warning("Мой налог: %s", result.auth_error)
        if await _alert_due(ctx):
            notify(
                ctx,
                AdminEvent.ALERTS,
                ctx.i18n.t("npd_receipts.auth_failed", lang, error=ctx.i18n.esc(result.auth_error[:200])),
            )
    elif result.registered or result.cancelled:
        await _clear_alert(ctx)
    if result.failed:
        notify(ctx, AdminEvent.PAYMENTS, ctx.i18n.t("npd_receipts.gave_up", lang, count=len(result.failed)))
    if settings.npd_send_to_user and result.registered:
        await announce(ctx, result.registered)


def _enabled(settings: Settings) -> bool:
    return bool(settings.npd_enabled)


JOBS = [
    JobSpec(
        id="npd_receipts.process",
        func=process_job,
        trigger="interval",
        kwargs={"minutes": 2},
        enabled=_enabled,
        lock_ttl_sec=900,
    ),
]
