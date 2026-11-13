"""System: audit log, background jobs, version and updates, log tail, maintenance, feature modules, webhook."""

from __future__ import annotations

import json
import platform
from datetime import datetime
from typing import Annotated, Any

from aiogram.exceptions import TelegramAPIError
from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select

from app.core.modules import SHOP_ONLY_MODULES, module_enabled
from app.core.permissions import Permission
from app.core.settings_provider import SettingValidationError
from app.core.settings_registry import get_spec
from app.db.models import AdminAuditLog
from app.modules.admin_api.deps import Actor, bad_request, conflict, not_found, require
from app.modules.admin_api.schemas.common import Ok
from app.modules.admin_api.schemas.system import (
    AllowlistIn,
    AuditOut,
    AuditPage,
    JobOut,
    JobsOut,
    LogsOut,
    MaintenanceEnableIn,
    MaintenanceOut,
    ModuleOut,
    ModulesIn,
    ModuleToggleIn,
    RunJobOut,
    TlsHostOut,
    TlsOut,
    VersionOut,
    WebhookOut,
    WindowIn,
    WindowOut,
)
from app.modules.admin_api.version import current_version, is_newer, latest_release
from app.services import tls_watch
from app.utils.time import utcnow
from app.web.deps import Ctx, ReadDb

router = APIRouter(prefix="/system", tags=["admin: system"])

Sysadmin = Annotated[Actor, Depends(require(Permission.SYSTEM))]
Settings_ = Annotated[Actor, Depends(require(Permission.SETTINGS))]
JobId = Annotated[str, Path(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9_.:\-]+$")]

MAX_LOG_LINES = 2000


@router.get("/plugin-catalog")
async def plugin_catalog(actor: Sysadmin, ctx: Ctx) -> dict[str, Any]:
    """Truthful read-only view: legacy FeatureModules are not represented as managed plugins."""
    plugins: list[dict[str, Any]] = []
    # The Web Admin is an optional consumer of Core. A partial/older runtime
    # must still render its legacy catalogue without exposing an internal error.
    core = getattr(ctx, "plugin_core", None)
    states = {state.module_id: state for state in core.lifecycle.states()} if core is not None else {}
    if core is not None:
        from app.core.plugins.scheduler import SCHEDULER_CONTRIBUTIONS

        scheduler_contributions = core.lifecycle.services.service(SCHEDULER_CONTRIBUTIONS)
        for manifest in core.registry.available():
            state = states.get(manifest.identifier)
            partial = manifest.identifier == "infrastructure.backups"
            scheduler = (
                await scheduler_contributions.status(manifest.identifier)
                if scheduler_contributions is not None
                else {"bound": False, "owner": manifest.identifier, "jobs": []}
            )
            runtime_status = "not-probed"
            restart_required = False
            if partial:
                if state is None:
                    runtime_status = "legacy-owned"
                elif state.status.value == "enabled":
                    runtime_status = "running" if any(job["scheduled"] for job in scheduler["jobs"]) else "pending"
                    restart_required = runtime_status != "running"
                elif state is not None and state.status.value in {"disabled", "installed"}:
                    stopped = "housekeeping.backup" in scheduler.get("suppressed_legacy_jobs", [])
                    runtime_status = "stopped" if stopped else "restart-required"
                    restart_required = not stopped
            plugins.append(
                {
                    "id": manifest.identifier,
                    "title": manifest.title,
                    "description": manifest.description,
                    "version": manifest.version,
                    "category": manifest.identifier.split(".", 1)[0],
                    "kind": "plugin",
                    "declared": True,
                    "installed": state is not None and state.status.value not in {"available", "uninstalled"},
                    "lifecycle_status": state.status.value if state is not None else "available",
                    "runtime_status": runtime_status,
                    "partially_migrated": partial,
                    "migration_status": "partial" if partial else "native",
                    "scheduler": scheduler,
                    "restart_required": restart_required,
                    "dependencies": list(manifest.depends_on),
                    "conflicts": list(manifest.conflicts),
                    "roles": sorted(role.value for role in manifest.roles),
                    "resources": {"ram_mb": manifest.resources.ram_mb, "disk_mb": manifest.resources.disk_mb},
                    "permissions": list(manifest.permissions),
                    "compatibility": {"core": manifest.core_compatibility, "sdk": manifest.sdk_compatibility},
                    "last_error": state.last_error if state is not None else None,
                    "actions_available": [],
                }
            )
    legacy = [
        {
            "id": f"legacy.{module.name}",
            "title": module.title(ctx.settings.default_language),
            "description": "Existing application feature module managed by the legacy runtime.",
            "version": "legacy",
            "category": "legacy",
            "kind": "legacy",
            "declared": True,
            "installed": True,
            "lifecycle_status": "legacy",
            # Enablement is configuration, not an independent process health check.
            "runtime_status": (
                "legacy-enabled" if ctx.modules.is_enabled(module.name, ctx.settings) else "legacy-disabled"
            ),
            "partially_migrated": False,
            "migration_status": "legacy",
            "restart_required": False,
            "dependencies": [],
            "conflicts": [],
            "roles": [],
            "resources": {},
            "permissions": [],
            "compatibility": {"core": "not-applicable", "sdk": "not-applicable"},
            "last_error": None,
            "actions_available": [],
        }
        for module in ctx.modules
    ]
    return {"core_available": core is not None, "plugins": plugins, "legacy": legacy}


# ---------------------------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------------------------
def _details(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return {"raw": raw[:500]}
    return value if isinstance(value, dict) else {"value": value}


@router.get("/audit", response_model=AuditPage)
async def audit_log(
    actor: Sysadmin,
    session: ReadDb,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    actor_id: int | None = None,
    action: Annotated[str | None, Query(max_length=64, description="Exact action or a prefix ending with '.'")] = None,
    target_type: Annotated[str | None, Query(max_length=32)] = None,
    target_id: Annotated[str | None, Query(max_length=64)] = None,
    source: Annotated[str | None, Query(pattern=r"^(bot|web)$")] = None,
    since: datetime | None = None,
) -> AuditPage:
    """Who did what, newest first. Page with ``before_id`` = the previous page's ``next_before_id``."""
    stmt = select(AdminAuditLog).order_by(AdminAuditLog.id.desc()).limit(limit)
    if before_id is not None:
        stmt = stmt.where(AdminAuditLog.id < before_id)
    if actor_id is not None:
        stmt = stmt.where(AdminAuditLog.actor_id == actor_id)
    if action:
        stmt = stmt.where(
            AdminAuditLog.action.startswith(action, autoescape=True)
            if action.endswith(".")
            else AdminAuditLog.action == action
        )
    if target_type:
        stmt = stmt.where(AdminAuditLog.target_type == target_type)
    if target_id:
        stmt = stmt.where(AdminAuditLog.target_id == target_id)
    if source:
        stmt = stmt.where(AdminAuditLog.source == source)
    if since is not None:
        stmt = stmt.where(AdminAuditLog.created_at >= since)
    rows = list((await session.scalars(stmt)).all())
    items = [
        AuditOut(
            id=r.id,
            actor_id=r.actor_id,
            action=r.action,
            target_type=r.target_type,
            target_id=r.target_id,
            details=_details(r.details),
            source=r.source,
            ip=r.ip,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return AuditPage(items=items, next_before_id=rows[-1].id if len(rows) == limit else None)


# ---------------------------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------------------------
def _scheduler(actor: Actor) -> Any:
    return getattr(actor.ctx, "scheduler", None)


@router.get("/jobs", response_model=JobsOut)
async def jobs(actor: Sysadmin) -> JobsOut:
    """Background jobs: schedule, last run, duration and last error."""
    scheduler = _scheduler(actor)
    if scheduler is None:
        return JobsOut(available=False, jobs=[])
    items = [
        JobOut(
            id=s.id,
            module=s.module,
            enabled=s.enabled,
            running=s.running,
            next_run_at=s.next_run_at,
            last_started_at=s.last_started_at,
            last_finished_at=s.last_finished_at,
            last_duration_ms=s.last_duration_ms,
            last_ok=s.last_ok,
            last_error=s.last_error,
            last_error_at=s.last_error_at,
        )
        for s in await scheduler.status()
    ]
    return JobsOut(available=True, jobs=items)


@router.post("/jobs/{job_id}/run", response_model=RunJobOut)
async def run_job(job_id: JobId, actor: Sysadmin) -> RunJobOut:
    """Run a job now (in the background); ``started`` is false if it is already running."""
    scheduler = _scheduler(actor)
    if scheduler is None:
        raise conflict("unavailable", "Jobs run in another process")
    try:
        started = bool(scheduler.run_now(job_id))
    except KeyError:
        raise not_found("Unknown job") from None
    async with actor.ctx.session_factory.begin() as session:
        await actor.audit(session, "job.run", "job", job_id, started=started)
    return RunJobOut(started=started)


# ---------------------------------------------------------------------------------------------
# Version, logs, webhook
# ---------------------------------------------------------------------------------------------
async def _migration(ctx: Any) -> str | None:
    engine = getattr(ctx, "engine", None)
    if engine is None:
        return None
    try:
        from app.runtime import migration_status

        current, _head = await migration_status(engine)
    except Exception:
        return None
    return current


@router.get("/version", response_model=VersionOut)
async def version(actor: Sysadmin, check: bool = True, force: bool = False) -> VersionOut:
    """This installation's version and whether a newer release is published."""
    ctx = actor.ctx
    settings = ctx.settings
    started = getattr(ctx, "started_at", None)
    engine = getattr(ctx, "engine", None)
    current = current_version()
    out = VersionOut(
        version=current,
        python=platform.python_version(),
        started_at=started,
        uptime_sec=int((utcnow() - started).total_seconds()) if started else None,
        role=getattr(ctx, "role", None),
        mode=settings.app_mode,
        database=engine.dialect.name if engine is not None else None,
        migration=await _migration(ctx),
    )
    if check:
        release = await latest_release(getattr(ctx, "http", None), settings.update_check_repo, force=force)
        if release is not None:
            out.latest_version = release.version
            out.release_url = release.url
            out.checked_at = release.checked_at
            out.check_error = release.error
            out.update_available = is_newer(release.version, current) if release.version else None
    return out


@router.get("/logs", response_model=LogsOut)
async def logs(actor: Sysadmin, lines: Annotated[int, Query(ge=1, le=MAX_LOG_LINES)] = 200) -> LogsOut:
    """The last log lines of this process (secrets are masked)."""
    buffer = getattr(actor.ctx, "log_buffer", None)
    if buffer is None:
        return LogsOut(available=False, lines=[])
    return LogsOut(available=True, lines=list(buffer.lines(lines)))


@router.get("/tls", response_model=TlsOut)
async def tls_status(actor: Sysadmin) -> TlsOut:
    """Expiry of the watched public certificates as of the last check."""
    settings = actor.ctx.settings
    names = tls_watch.watched_hosts(settings)
    states = await tls_watch.load_states(actor.ctx.session_factory)
    hosts = [
        TlsHostOut(**states[h].public())
        if h in states
        else TlsHostOut(host=h, checked_at=None, expires_at=None, days_left=None, ok=True, problem=None, error=None)
        for h in names
    ]
    return TlsOut(enabled=bool(names), warn_days=settings.tls_warn_days, hosts=hosts)


@router.get("/webhook", response_model=WebhookOut)
async def webhook_info(actor: Sysadmin) -> WebhookOut:
    """Telegram's view of the bot webhook (pending updates, last delivery error)."""
    ctx = actor.ctx
    mode = ctx.settings.bot_mode
    try:
        info = await ctx.bot.get_webhook_info()
    except TelegramAPIError as exc:
        return WebhookOut(mode=mode, error=str(exc)[:300])
    return WebhookOut(
        mode=mode,
        url=info.url or None,
        pending_update_count=info.pending_update_count,
        last_error_date=info.last_error_date,
        last_error_message=info.last_error_message,
        max_connections=info.max_connections,
        ip_address=info.ip_address,
    )


# ---------------------------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------------------------
async def _maintenance(actor: Actor) -> MaintenanceOut:
    service = actor.ctx.maintenance
    state = await service.state(fresh=True)
    return MaintenanceOut(
        active=state.is_active(utcnow()),
        enabled=state.enabled,
        message_ru=state.message_ru,
        message_en=state.message_en,
        eta=state.eta,
        allowlist=list(state.allowlist),
        disabled_modules=list(state.disabled_modules),
        started_at=state.started_at,
        started_by=state.started_by,
        windows=[
            WindowOut(
                id=w.id,
                starts_at=w.starts_at,
                ends_at=w.ends_at,
                announce_minutes=w.announce_minutes,
                message_ru=w.message_ru,
                message_en=w.message_en,
                announced=w.announced,
                started=w.started,
            )
            for w in state.windows
        ],
        pending_notices=await service.pending_hits(),
    )


@router.get("/maintenance", response_model=MaintenanceOut)
async def maintenance_state(actor: Sysadmin) -> MaintenanceOut:
    return await _maintenance(actor)


@router.post("/maintenance/enable", response_model=MaintenanceOut)
async def maintenance_enable(body: MaintenanceEnableIn, actor: Sysadmin) -> MaintenanceOut:
    """Close the bot for users (admins and the allowlist keep access) with an optional message and ETA."""
    try:
        await actor.ctx.maintenance.enable(
            actor.tg_id, message_ru=body.message_ru, message_en=body.message_en, eta=body.eta, source="web", ip=actor.ip
        )
    except ValueError as exc:
        raise bad_request("invalid", str(exc)) from None
    return await _maintenance(actor)


@router.post("/maintenance/disable", response_model=MaintenanceOut)
async def maintenance_disable(actor: Sysadmin) -> MaintenanceOut:
    """Open the bot again (users who hit the maintenance screen are told it works again)."""
    await actor.ctx.maintenance.disable(actor.tg_id, source="web", ip=actor.ip)
    return await _maintenance(actor)


@router.put("/maintenance/message", response_model=MaintenanceOut)
async def maintenance_message(body: MaintenanceEnableIn, actor: Sysadmin) -> MaintenanceOut:
    try:
        await actor.ctx.maintenance.set_message(
            actor.tg_id, message_ru=body.message_ru, message_en=body.message_en, eta=body.eta, source="web", ip=actor.ip
        )
    except ValueError as exc:
        raise bad_request("invalid", str(exc)) from None
    return await _maintenance(actor)


@router.put("/maintenance/allowlist", response_model=MaintenanceOut)
async def maintenance_allowlist(body: AllowlistIn, actor: Sysadmin) -> MaintenanceOut:
    """Testers who keep full access during maintenance."""
    await actor.ctx.maintenance.set_allowlist(actor.tg_id, body.user_ids, source="web", ip=actor.ip)
    return await _maintenance(actor)


@router.put("/maintenance/modules", response_model=MaintenanceOut)
async def maintenance_modules(body: ModulesIn, actor: Sysadmin) -> MaintenanceOut:
    """Partial maintenance: pause only these features (e.g. payments) for users."""
    known = {m.name for m in actor.ctx.modules}
    unknown = [m for m in body.modules if m not in known]
    if unknown:
        raise bad_request("unknown_module", f"Unknown modules: {', '.join(unknown)}")
    await actor.ctx.maintenance.set_disabled_modules(actor.tg_id, body.modules, source="web", ip=actor.ip)
    return await _maintenance(actor)


@router.post("/maintenance/windows", response_model=MaintenanceOut, status_code=201)
async def maintenance_schedule(body: WindowIn, actor: Sysadmin) -> MaintenanceOut:
    """Plan a maintenance window (users are warned ``announce_minutes`` before it starts)."""
    try:
        await actor.ctx.maintenance.schedule(
            actor.tg_id,
            body.starts_at,
            body.ends_at,
            announce_minutes=body.announce_minutes,
            message_ru=body.message_ru,
            message_en=body.message_en,
            source="web",
            ip=actor.ip,
        )
    except ValueError as exc:
        raise bad_request("invalid_window", str(exc)) from None
    return await _maintenance(actor)


@router.delete("/maintenance/windows/{window_id}", response_model=MaintenanceOut)
async def maintenance_cancel_window(
    window_id: Annotated[str, Path(min_length=1, max_length=32, pattern=r"^[0-9a-f]+$")], actor: Sysadmin
) -> MaintenanceOut:
    if not await actor.ctx.maintenance.cancel_window(actor.tg_id, window_id, source="web", ip=actor.ip):
        raise not_found("No such window")
    return await _maintenance(actor)


# ---------------------------------------------------------------------------------------------
# Feature modules
# ---------------------------------------------------------------------------------------------
async def _modules(actor: Actor) -> list[ModuleOut]:
    ctx = actor.ctx
    settings = ctx.settings
    try:
        state = await ctx.maintenance.state()
        paused = set(state.disabled_modules)
    except Exception:
        paused = set()
    lang = "en" if settings.default_language == "en" else "ru"
    return [
        ModuleOut(
            name=m.name,
            title=m.title(lang),
            enabled=module_enabled(m, settings),
            required=m.required,
            shop_only=m.shop_only or m.name in SHOP_ONLY_MODULES,
            switched_off=m.name in settings.disabled_modules,
            maintenance=m.name in paused,
            admin_menu=[
                {
                    "key": i.key,
                    "title_key": i.title_key,
                    "icon": i.icon,
                    "permission": str(i.permission),
                    "web_path": i.web_path,
                    "allowed": actor.can(i.permission),
                    "order": i.order,
                }
                for i in m.admin_menu
            ],
        )
        for m in ctx.modules
    ]


@router.get("/modules", response_model=list[ModuleOut])
async def modules(actor: Settings_) -> list[ModuleOut]:
    """Feature modules: which are on, which the product mode hides, which are paused by maintenance."""
    return await _modules(actor)


@router.put("/modules/{name}", response_model=list[ModuleOut])
async def toggle_module(
    name: Annotated[str, Path(min_length=1, max_length=48, pattern=r"^[a-z0-9_]+$")],
    body: ModuleToggleIn,
    actor: Settings_,
) -> list[ModuleOut]:
    """Switch a feature module on or off (takes effect immediately)."""
    ctx = actor.ctx
    module = ctx.modules.get(name)
    if module is None:
        raise not_found("Unknown module")
    if module.required and not body.enabled:
        raise conflict("required", "This module cannot be switched off")
    actor.require(get_spec("disabled_modules").permission)
    current = list(ctx.settings.disabled_modules)
    new = [m for m in current if m != name] if body.enabled else [*current, name] if name not in current else current
    if new != current:
        try:
            await ctx.settings_provider.set("disabled_modules", new, actor.tg_id, source="web", ip=actor.ip)
        except SettingValidationError as exc:
            raise bad_request("invalid_value", exc.message) from None
    return await _modules(actor)


__all__ = ["Ok", "router"]
