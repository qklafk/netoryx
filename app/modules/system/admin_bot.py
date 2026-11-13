"""Admin quick action: /status (version, uptime, modules, maintenance, background jobs)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core import __version__
from app.core.context import AppContext
from app.core.filters import IsPrivate
from app.core.permissions import HasPermission, Permission
from app.modules.system.texts import t

router = Router(name="system_admin")
router.message.filter(IsPrivate())


def format_uptime(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return f"{days}d {hours:02d}:{minutes:02d}" if days else f"{hours:02d}:{minutes:02d}"


async def build_status(ctx: AppContext, lang: str) -> str:
    settings = ctx.settings
    now = datetime.now(UTC)
    enabled = [m.name for m in ctx.modules.enabled(settings)]
    lines = [
        t(ctx, "system.status.title", lang),
        t(ctx, "system.status.version", lang, version=__version__),
        t(ctx, "system.status.uptime", lang, uptime=format_uptime(now - ctx.started_at)),
        t(ctx, "system.status.role", lang, role=ctx.role, mode=settings.app_mode),
        t(ctx, "system.status.modules", lang, modules=", ".join(enabled) or "-"),
    ]
    state = await ctx.maintenance.state()
    key = "system.status.maintenance_on" if state.is_active(now) else "system.status.maintenance_off"
    lines.append(t(ctx, key, lang))
    scheduler = getattr(ctx, "scheduler", None)
    if scheduler is not None and scheduler.job_ids and ctx.role in ("all", "worker"):
        jobs = await scheduler.status()
        failed = sum(1 for job in jobs if job.last_ok is False)
        lines.append(t(ctx, "system.status.jobs", lang, total=len(jobs), failed=failed))
    else:
        lines.append(t(ctx, "system.status.no_jobs", lang))
    return "\n".join(lines)


@router.message(Command("status"), HasPermission(Permission.SYSTEM))
async def status_command(message: Message, ctx: AppContext, lang: str | None = None) -> None:
    await message.answer(await build_status(ctx, lang or ctx.settings.default_language))
