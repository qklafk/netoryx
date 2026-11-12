"""Housekeeping jobs: the daily backup (file to the backup chat) and the daily cleanup of expired technical data."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from app.core.modules import JobSpec
from app.db.models import KeyValue
from app.services import backups, hwid, imports
from app.services.notifications import AdminEvent, notify
from app.utils.time import utcnow
from app.web import auth

if TYPE_CHECKING:
    from app.config import Settings
    from app.core.context import AppContext
    from app.db.session import SessionFactory

log = logging.getLogger(__name__)

SESSIONS_KEEP = timedelta(days=30)
CLEANUP_HOUR, CLEANUP_MINUTE = 3, 30
# kv prefixes whose values are ISO timestamps and that may go once older than the given age.
KV_TTL: dict[str, timedelta] = {
    "rwhook:": timedelta(days=7),  # Remnawave webhook deliveries already processed
    "devices:reset:": timedelta(days=30),  # last device reset (cooldowns are minutes to hours)
}
KV_BATCH = 1000


# --- backups ---------------------------------------------------------------------------------------------------


def _size(ctx: AppContext, lang: str, size: int) -> str:
    value, unit = float(size), 0
    units = ("b", "kb", "mb", "gb")
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    number = f"{value:.1f}".removesuffix(".0") if value < 10 else f"{value:.0f}"
    if lang == "ru":
        number = number.replace(".", ",")
    return str(ctx.i18n.t(f"housekeeping.unit.{units[unit]}", lang, n=number))


def backup_caption(ctx: AppContext, info: backups.BackupInfo) -> str:
    settings = ctx.settings
    lang = settings.default_language
    i18n = ctx.i18n
    when = i18n.datetime(info.created_at, lang, settings.zone)
    key = "housekeeping.backup.encrypted" if info.encrypted else "housekeeping.backup.plain"
    return str(
        i18n.t(
            "housekeeping.backup.caption",
            lang,
            brand=i18n.esc(settings.brand_name),
            when=when,
            size=_size(ctx, lang, info.size),
        )
        + "\n"
        + i18n.t(key, lang)
    )


async def backup_job(ctx: AppContext) -> None:
    """Create a backup, keep the newest ``BACKUP_KEEP`` files and send the new one to the backup chat."""
    settings = ctx.settings
    lang = settings.default_language
    folder = backups.directory_from(settings)
    try:
        info = await backups.create(ctx.engine, folder, password=backups.password_from(settings))
    except Exception as exc:
        notify(
            ctx, AdminEvent.BACKUPS, ctx.i18n.t("housekeeping.backup.failed", lang, error=ctx.i18n.esc(str(exc)[:300]))
        )
        raise
    removed = await asyncio.to_thread(backups.prune, folder, backups.keep_from(settings))
    if removed:
        log.info("backups: removed %d old files", len(removed))
    chat_id = settings.backup_target_chat
    if chat_id is None:
        log.warning("backup %s created but there is no chat to send it to (BACKUP_CHAT_ID / ADMIN_IDS)", info.path.name)
        return
    try:
        await backups.send_to_telegram(ctx.bot, chat_id, info.path, caption=backup_caption(ctx, info))
    except Exception as exc:
        log.exception("could not send backup %s to chat %s", info.path.name, chat_id)
        notify(
            ctx,
            AdminEvent.BACKUPS,
            ctx.i18n.t(
                "housekeeping.backup.not_sent",
                lang,
                name=ctx.i18n.esc(info.path.name),
                error=ctx.i18n.esc(str(exc)[:200]),
            ),
        )


def _backup_schedule(settings: Settings) -> dict[str, Any]:
    return {"hour": settings.backup_time.hour, "minute": settings.backup_time.minute}


def _backup_enabled(settings: Settings) -> bool:
    return settings.backup_enabled


# --- cleanup ---------------------------------------------------------------------------------------------------


def _expired(value: str, cutoff: datetime) -> bool:
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return True  # not ours to keep: an unreadable timestamp under a timestamp prefix
    if stamp.tzinfo is None:
        return False
    return stamp < cutoff


async def purge_kv(session_factory: SessionFactory, now: datetime | None = None) -> int:
    """Delete timestamped ``kv`` rows older than their prefix's lifetime (:data:`KV_TTL`)."""
    now = now or utcnow()
    removed = 0
    for prefix, ttl in KV_TTL.items():
        cutoff = now - ttl
        async with session_factory.begin() as session:
            rows = (
                await session.execute(
                    select(KeyValue.key, KeyValue.value)
                    .where(KeyValue.key.startswith(prefix, autoescape=True))
                    .limit(KV_BATCH * 10)
                )
            ).all()
            stale = [key for key, value in rows if _expired(value, cutoff)]
            for start in range(0, len(stale), KV_BATCH):
                result = await session.execute(
                    delete(KeyValue).where(KeyValue.key.in_(stale[start : start + KV_BATCH]))
                )
                removed += int(getattr(result, "rowcount", 0) or 0)
    return removed


async def cleanup_job(ctx: AppContext) -> None:
    """Expired web login tokens and sessions, processed webhook markers, old reset stamps, stale devices."""
    tokens = await auth.purge_expired_login_tokens(ctx.session_factory)
    sessions = await auth.purge_sessions(ctx.session_factory, keep=SESSIONS_KEEP)
    rows = await purge_kv(ctx.session_factory)
    secret_box = getattr(ctx, "secret_box", None)
    if secret_box is not None:
        await imports.purge_expired_claims(ctx.session_factory, secret_box)
    devices_removed = await hwid.purge_stale(ctx.session_factory, ctx.settings.hwid_device_ttl_days)
    if tokens or sessions or rows or devices_removed:
        log.info(
            "cleanup: %d login tokens, %d admin sessions, %d kv rows, %d stale devices removed",
            tokens,
            sessions,
            rows,
            devices_removed,
        )


JOBS = [
    JobSpec(
        id="housekeeping.backup",
        func=backup_job,
        trigger="cron",
        schedule=_backup_schedule,
        enabled=_backup_enabled,
        lock_ttl_sec=3600,
    ),
    JobSpec(
        id="housekeeping.cleanup",
        func=cleanup_job,
        trigger="cron",
        kwargs={"hour": CLEANUP_HOUR, "minute": CLEANUP_MINUTE},
        lock_ttl_sec=1800,
    ),
]
