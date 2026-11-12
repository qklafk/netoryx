"""Background jobs: APScheduler with single-instance runs, cross-process locks, status tracking and alerts."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config import Settings
from app.core.modules import JobSpec, ModuleRegistry, module_enabled
from app.db.models import KeyValue
from app.db.session import read_session
from app.services import kv
from app.services.notify import notify_admins

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

STATUS_PREFIX = "job:"
LOCK_PREFIX = "job:"
MAX_JOB_ID_LENGTH = 48
CORE_MODULE = "core"
DEFAULT_MISFIRE_GRACE_SEC = 60
DEFAULT_ALERT_INTERVAL_SEC = 600.0


@dataclass(frozen=True, slots=True)
class JobStatus:
    id: str
    module: str
    enabled: bool
    next_run_at: datetime | None
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_duration_ms: int | None = None
    last_ok: bool | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    running: bool = False


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_dt(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


class Scheduler:
    """Owns the APScheduler instance and every registered :class:`JobSpec`."""

    def __init__(
        self,
        ctx: AppContext,
        *,
        use_locks: bool | None = None,
        alert_interval_sec: float = DEFAULT_ALERT_INTERVAL_SEC,
        misfire_grace_sec: int = DEFAULT_MISFIRE_GRACE_SEC,
    ) -> None:
        self._ctx = ctx
        settings = ctx.settings
        self._use_locks = use_locks if use_locks is not None else (settings.role != "all" or ctx.redis is not None)
        self._alert_interval = alert_interval_sec
        self._misfire_grace = misfire_grace_sec
        self._aps = AsyncIOScheduler(timezone=settings.zone)
        self._specs: dict[str, tuple[str, JobSpec]] = {}
        self._legacy_specs: dict[str, tuple[str, JobSpec]] = {}
        self._contribution_owners: dict[str, str] = {}
        self._suppressed_legacy: dict[str, str] = {}
        self._applied: dict[str, tuple[str, dict[str, Any]]] = {}
        self._running: dict[str, asyncio.Task[Any]] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._last_alert: dict[str, float] = {}
        self._started = False
        self._sync_lock = asyncio.Lock()
        self._unsubscribe: Any = None

    # --- registration ----------------------------------------------------------------------------
    def register(self, spec: JobSpec, module: str = CORE_MODULE) -> None:
        if not spec.id or len(spec.id) > MAX_JOB_ID_LENGTH:
            raise ValueError(f"job id must be 1..{MAX_JOB_ID_LENGTH} characters")
        if spec.id in self._specs:
            raise ValueError(f"duplicate job id {spec.id!r}")
        if spec.trigger not in ("interval", "cron"):
            raise ValueError(f"job {spec.id}: unknown trigger {spec.trigger!r}")
        self._specs[spec.id] = (module, spec)
        self._legacy_specs[spec.id] = (module, spec)

    def register_modules(self, modules: ModuleRegistry) -> None:
        for module in modules:
            for spec in module.jobs:
                self.register(spec, module.name)

    @property
    def job_ids(self) -> list[str]:
        return list(self._specs)

    def is_scheduled(self, job_id: str) -> bool:
        return job_id in self._applied

    async def activate_contribution(self, owner: str, specs: tuple[JobSpec, ...]) -> None:
        """Atomically replace legacy jobs with a plugin's declared scheduler contribution."""
        if not owner:
            raise ValueError("scheduler contribution owner is required")
        ids = {spec.id for spec in specs}
        if len(ids) != len(specs):
            raise ValueError(f"duplicate contribution job for {owner}")
        for spec in specs:
            self._validate_spec(spec)
            existing = self._contribution_owners.get(spec.id)
            if existing is not None and existing != owner:
                raise ValueError(f"job {spec.id!r} is owned by {existing!r}")
        previous = [job_id for job_id, current_owner in self._contribution_owners.items() if current_owner == owner]
        for job_id in previous:
            if job_id not in ids:
                self._restore_legacy(job_id, owner)
        for spec in specs:
            self._specs[spec.id] = (owner, spec)
            self._contribution_owners[spec.id] = owner
            self._suppressed_legacy.pop(spec.id, None)
        await self.sync()

    async def deactivate_contribution(self, owner: str, job_ids: tuple[str, ...] = ()) -> None:
        """Stop an owner's jobs while retaining legacy compatibility jobs as disabled records.

        ``job_ids`` names the jobs the owner would contribute, so that the legacy job stays suppressed even in a
        fresh process where the owner never activated anything.
        """
        for job_id, current_owner in list(self._contribution_owners.items()):
            if current_owner == owner:
                self._restore_legacy(job_id, owner)
        for job_id in job_ids:
            if job_id in self._legacy_specs and job_id not in self._contribution_owners:
                self._suppressed_legacy[job_id] = owner
        await self.sync()

    async def contribution_status(self, owner: str) -> dict[str, Any]:
        jobs = await self.status()
        owned = [job for job in jobs if self._contribution_owners.get(job.id) == owner]
        suppressed = [job_id for job_id, current_owner in self._suppressed_legacy.items() if current_owner == owner]
        return {
            "bound": True,
            "owner": owner,
            "jobs": [
                {"id": job.id, "scheduled": job.enabled, "running": job.running, "last_error": job.last_error}
                for job in owned
            ],
            "suppressed_legacy_jobs": suppressed,
        }

    # --- lifecycle -------------------------------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        self._aps.start()
        self._started = True
        await self.sync()
        self._unsubscribe = self._ctx.settings_provider.subscribe(self.sync)
        log.info("scheduler started with %d jobs", len(self._applied))

    async def shutdown(self, wait_sec: float = 10.0) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._started:
            self._aps.shutdown(wait=False)
            self._started = False
        self._applied.clear()
        tasks = [*self._running.values(), *self._background]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=wait_sec)
            for task in pending:
                task.cancel()
            if pending:
                log.warning("cancelled %d jobs still running at shutdown", len(pending))
                await asyncio.gather(*pending, return_exceptions=True)

    async def sync(self, settings: Settings | None = None) -> None:
        """Add, remove and reschedule jobs to match the settings (module switches, intervals)."""
        if not self._started:
            return
        settings = settings or self._ctx.settings
        async with self._sync_lock:
            for job_id, (module, spec) in self._specs.items():
                wanted = self._wanted(module, spec, settings)
                applied = self._applied.get(job_id)
                if wanted is None:
                    if applied is not None:
                        self._remove(job_id)
                    continue
                if applied == wanted:
                    continue
                try:
                    trigger = self._trigger(wanted, settings)
                except (TypeError, ValueError):
                    log.exception("job %s: invalid schedule %s", job_id, wanted)
                    continue
                if applied is None:
                    first_run: Any = datetime.now(UTC) if spec.run_on_start else None
                    kwargs: dict[str, Any] = {"next_run_time": first_run} if first_run else {}
                    self._aps.add_job(
                        self._run,
                        trigger=trigger,
                        args=[job_id],
                        id=job_id,
                        name=job_id,
                        replace_existing=True,
                        max_instances=1,
                        coalesce=True,
                        misfire_grace_time=self._misfire_grace,
                        **kwargs,
                    )
                    log.debug("job %s scheduled: %s", job_id, wanted)
                else:
                    self._aps.reschedule_job(job_id, trigger=trigger)
                    log.info("job %s rescheduled: %s", job_id, wanted)
                self._applied[job_id] = wanted

    def _wanted(self, module: str, spec: JobSpec, settings: Settings) -> tuple[str, dict[str, Any]] | None:
        if spec.id in self._suppressed_legacy:
            return None
        if module != CORE_MODULE and spec.id not in self._contribution_owners:
            registry_module = self._ctx.modules.get(module)
            if registry_module is None or not module_enabled(registry_module, settings):
                return None
        try:
            if not spec.is_enabled(settings):
                return None
            return spec.trigger, spec.trigger_kwargs(settings)
        except Exception:
            log.exception("job %s: schedule evaluation failed", spec.id)
            return None

    @staticmethod
    def _trigger(wanted: tuple[str, dict[str, Any]], settings: Settings) -> BaseTrigger:
        kind, kwargs = wanted
        if kind == "cron":
            return CronTrigger(timezone=settings.zone, **kwargs)
        return IntervalTrigger(timezone=settings.zone, **kwargs)

    @staticmethod
    def _validate_spec(spec: JobSpec) -> None:
        if not spec.id or len(spec.id) > MAX_JOB_ID_LENGTH:
            raise ValueError(f"job id must be 1..{MAX_JOB_ID_LENGTH} characters")
        if spec.trigger not in ("interval", "cron"):
            raise ValueError(f"job {spec.id}: unknown trigger {spec.trigger!r}")

    def _restore_legacy(self, job_id: str, owner: str) -> None:
        if self._contribution_owners.get(job_id) != owner:
            return
        self._contribution_owners.pop(job_id, None)
        legacy = self._legacy_specs.get(job_id)
        if legacy is None:
            self._specs.pop(job_id, None)
            self._remove(job_id)
            return
        self._specs[job_id] = legacy
        # An explicit lifecycle disable must not silently reactivate the legacy
        # job on this process or after the next process restart.
        self._suppressed_legacy[job_id] = owner

    def _remove(self, job_id: str) -> None:
        self._applied.pop(job_id, None)
        try:
            self._aps.remove_job(job_id)
        except Exception:  # already gone
            log.debug("job %s was not scheduled", job_id)
        log.info("job %s unscheduled", job_id)

    # --- running ---------------------------------------------------------------------------------
    def run_now(self, job_id: str) -> bool:
        """Start a job immediately in the background (unless it is already running)."""
        if job_id not in self._specs:
            raise KeyError(f"unknown job {job_id!r}")
        if job_id in self._running:
            return False
        task = asyncio.create_task(self._run(job_id))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return True

    async def _run(self, job_id: str) -> None:
        module, spec = self._specs[job_id]
        if job_id in self._running:
            log.debug("job %s is already running", job_id)
            return
        task = asyncio.current_task()
        if task is not None:
            self._running[job_id] = task
        started_at = datetime.now(UTC)
        t0 = time.monotonic()
        error: str | None = None
        ran = True
        try:
            if self._use_locks:
                async with self._ctx.locks.job_lock(LOCK_PREFIX + job_id, spec.lock_ttl_sec) as acquired:
                    if not acquired:
                        ran = False
                        log.debug("job %s is running elsewhere; skipped", job_id)
                        return
                    await spec.func(self._ctx)
            else:
                await spec.func(self._ctx)
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            log.exception("job %s (%s) failed", job_id, module)
            await self._alert(job_id, error)
        finally:
            try:
                if ran:
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    await self._record(job_id, started_at, duration_ms, error)
            finally:
                # Tracked until the status is stored, so shutdown waits for the database write as well.
                self._running.pop(job_id, None)

    async def _record(self, job_id: str, started_at: datetime, duration_ms: int, error: str | None) -> None:
        key = STATUS_PREFIX + job_id
        now = datetime.now(UTC)
        try:
            async with self._ctx.session_factory.begin() as session:
                previous = _load(await kv.get(session, key))
                data = {
                    "last_started_at": _iso(started_at),
                    "last_finished_at": _iso(now),
                    "last_duration_ms": duration_ms,
                    "last_ok": error is None,
                    "last_error": error if error else previous.get("last_error"),
                    "last_error_at": _iso(now) if error else previous.get("last_error_at"),
                }
                await kv.set(session, key, json.dumps(data))
        except Exception:
            log.exception("job %s: failed to store status", job_id)

    async def _alert(self, job_id: str, error: str) -> None:
        now = time.monotonic()
        last = self._last_alert.get(job_id)
        if last is not None and now - last < self._alert_interval:
            return
        self._last_alert[job_id] = now
        settings = self._ctx.settings
        text = _job_failed_text(self._ctx, settings, job_id, error)
        try:
            await notify_admins(self._ctx.bot, settings, text)
        except Exception:
            log.exception("job %s: failed to alert admins", job_id)

    # --- status ----------------------------------------------------------------------------------
    async def status(self) -> list[JobStatus]:
        keys = [STATUS_PREFIX + job_id for job_id in self._specs]
        stored: dict[str, dict[str, Any]] = {}
        if keys:
            async with read_session(self._ctx.session_factory) as session:
                rows = await session.execute(select(KeyValue.key, KeyValue.value).where(KeyValue.key.in_(keys)))
                stored = {key[len(STATUS_PREFIX) :]: _load(value) for key, value in rows}
        result: list[JobStatus] = []
        for job_id, (module, _spec) in self._specs.items():
            data = stored.get(job_id, {})
            job = self._aps.get_job(job_id) if self._started else None
            result.append(
                JobStatus(
                    id=job_id,
                    module=module,
                    enabled=job_id in self._applied,
                    next_run_at=getattr(job, "next_run_time", None),
                    last_started_at=_parse_dt(data.get("last_started_at")),
                    last_finished_at=_parse_dt(data.get("last_finished_at")),
                    last_duration_ms=data.get("last_duration_ms"),
                    last_ok=data.get("last_ok"),
                    last_error=data.get("last_error"),
                    last_error_at=_parse_dt(data.get("last_error_at")),
                    running=job_id in self._running,
                )
            )
        return result


def _load(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _job_failed_text(ctx: AppContext, settings: Settings, job_id: str, error: str) -> str:
    key = "system.job_failed"
    params = {"job": html.escape(job_id), "error": html.escape(error)}
    try:
        text = ctx.i18n.t(key, settings.default_language, **params)
    except Exception:
        text = key
    if text == key:
        text = "⚠️ Background job <code>{job}</code> failed:\n<code>{error}</code>".format(**params)
    return text
