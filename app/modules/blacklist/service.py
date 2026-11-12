"""Application glue of the blacklist: scheduled refresh, enforcement on incoming updates, admin notices."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.services import blacklist
from app.services.notifications import AdminEvent, notify

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

MAX_LISTED_IN_NOTICE = 20


def _kick_reconcile(ctx: AppContext) -> None:
    from app.modules.admin_api.deps import kick_reconcile

    try:
        kick_reconcile(ctx)
    except Exception:
        log.exception("could not start the panel reconcile")


def _ids(ctx: AppContext, ids: list[int]) -> str:
    settings = ctx.settings
    lines = []
    for tg_id in ids[:MAX_LISTED_IN_NOTICE]:
        reason = blacklist.CACHE.reason(tg_id, settings)
        lines.append(f"<code>{tg_id}</code>" + (f" — {ctx.i18n.esc(reason)}" if reason else ""))
    if len(ids) > MAX_LISTED_IN_NOTICE:
        lines.append(ctx.i18n.t("blacklist.more", settings.default_language, n=len(ids) - MAX_LISTED_IN_NOTICE))
    return "\n".join(lines)


def report_to_admins(ctx: AppContext, result: blacklist.ApplyReport) -> None:
    """Tell administrators about new bans, lifted bans and newly flagged users (nothing to say - no message)."""
    lang = ctx.settings.default_language
    t = ctx.i18n.t
    parts = []
    if result.banned:
        parts.append(t("blacklist.notice.banned", lang, n=len(result.banned)) + "\n" + _ids(ctx, result.banned))
    if result.new_flagged:
        flagged = result.new_flagged
        parts.append(t("blacklist.notice.flagged", lang, n=len(flagged)) + "\n" + _ids(ctx, flagged))
    if result.unbanned:
        parts.append(t("blacklist.notice.unbanned", lang, n=len(result.unbanned)) + "\n" + _ids(ctx, result.unbanned))
    if not parts:
        return
    try:
        notify(ctx, AdminEvent.USERS, "\n\n".join(parts))
    except Exception:
        log.exception("could not notify administrators about the blacklist")


async def refresh_and_apply(ctx: AppContext) -> tuple[blacklist.RefreshReport, blacklist.ApplyReport]:
    """Download the lists, enforce the policy and report changes (the job and the "refresh now" button)."""
    refreshed = await blacklist.refresh(ctx.session_factory, ctx.http, ctx.settings)
    applied = await blacklist.apply(ctx.session_factory, ctx.settings)
    if applied.banned or applied.unbanned:
        _kick_reconcile(ctx)
    report_to_admins(ctx, applied)
    return refreshed, applied


async def on_listed_user(ctx: AppContext, tg_id: int) -> bool:
    """A listed user sent an update: ban or flag them now. True if the update must be dropped."""
    try:
        blocked, result = await blacklist.check_user(ctx.session_factory, ctx.settings, tg_id)
    except Exception:
        log.exception("blacklist check of user %s failed", tg_id)
        return False
    if result.banned:
        _kick_reconcile(ctx)
    report_to_admins(ctx, result)
    return blocked
